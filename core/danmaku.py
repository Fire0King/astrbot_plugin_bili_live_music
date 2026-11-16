"""
B 站直播间弹幕监听。

用 bilibili-api-python 的 `LiveDanmaku` 连官方 WebSocket(匿名即可连公开直播间),
外面套一层"总监管"负责重连:
  · 连接失败/断开 → 指数退避 1,2,4,8,16,32 秒(之后固定 32 秒)无线重试;
  · 每次重连都递增 generation, 旧连接迟到的回调直接丢弃(避免状态被污染);
  · 记录最近的状态迁移, 便于面板/命令展示"为什么没连上"。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from astrbot.api import logger

RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)

STATE_DISABLED = "disabled"
STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_WAITING = "waiting"          # 等待下一次重试
STATE_RECONNECTING = "reconnecting"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"


@dataclass
class DanmakuMessage:
    """一条弹幕(只保留点歌需要的最少信息)"""

    room_id: int
    uid: int
    username: str
    text: str
    timestamp: float = field(default_factory=time.time)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


def parse_danmaku_callback(callback_info: Any) -> Optional[DanmakuMessage]:
    """把 LiveDanmaku 的 DANMU_MSG 回调解析成 DanmakuMessage。

    官方协议里弹幕正文在 `data["info"]` 中:
      info[0] 元信息 / info[1] 文本 / info[2] [uid, 昵称, ...]
    """
    if not isinstance(callback_info, dict):
        return None
    payload = callback_info.get("data") or {}
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None
    text = str(info[1] or "").strip()
    user = info[2] if isinstance(info[2], list) and info[2] else []
    uid = int(user[0]) if user and str(user[0]).isdigit() else 0
    username = str(user[1]) if len(user) > 1 and user[1] else f"uid{uid or '匿名'}"
    room = payload.get("room_id") or callback_info.get("room_id") or 0
    try:
        room_id = int(room)
    except (TypeError, ValueError):
        room_id = 0
    if not text:
        return None
    return DanmakuMessage(room_id=room_id, uid=uid, username=username, text=text, raw=payload)


def credential_from_cookie(cookie: str) -> Optional[Any]:
    """把 `SESSDATA=...; bili_jct=...` 这样的 Cookie 转成 bilibili_api 的 Credential。

    只填需要的字段; 解析失败返回 None(匿名连接)。
    """
    text = (cookie or "").strip()
    if not text:
        return None
    pairs: Dict[str, str] = {}
    for item in text.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        pairs[key.strip().lower()] = value.strip()
    try:
        from bilibili_api import Credential
    except Exception:  # noqa: BLE001
        return None
    try:
        return Credential(
            sessdata=pairs.get("sessdata") or None,
            bili_jct=pairs.get("bili_jct") or None,
            dedeuserid=pairs.get("dedeuserid") or None,
            buvid3=pairs.get("buvid3") or None,
            ac_time_value=pairs.get("ac_time_value") or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"构造 B 站凭据失败, 改用匿名连接: {e}")
        return None


class DanmakuService:
    """弹幕连接管理(重连/状态/回调)"""

    def __init__(
        self,
        room_id: int,
        credential: Any = None,
        on_message: Optional[Callable[[DanmakuMessage], Any]] = None,
        client_factory: Optional[Callable[[int, Any], Any]] = None,
        connect_timeout: float = 10.0,
        retry_delays: Optional[List[float]] = None,
        history_limit: int = 25,
    ):
        self.room_id = int(room_id or 0)
        self.credential = credential
        self.on_message = on_message
        self._client_factory = client_factory or self._default_factory
        self.connect_timeout = max(0.2, float(connect_timeout))
        self.retry_delays = list(retry_delays or RETRY_DELAYS)
        self.state = STATE_DISABLED if self.room_id <= 0 else STATE_STOPPED
        self.last_error = ""
        self.connected_at: float = 0.0
        self.attempts = 0
        self.generation = 0
        self.transitions: Deque[Dict[str, Any]] = deque(maxlen=max(5, history_limit))
        self._task: Optional[asyncio.Task] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._connected_event: Optional[asyncio.Event] = None
        self._client: Any = None
        self._stopping = False
        self._event_count = 0
        self._danmaku_count = 0

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> bool:
        if self.room_id <= 0:
            self._transition(STATE_DISABLED, "未配置直播间号")
            return False
        if self.running:
            return True
        self._stopping = False
        self._task = asyncio.create_task(self._supervise())
        return True

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        await self._stop_connect_task()
        await self._disconnect_client()
        self._transition(STATE_STOPPED, "已停止")

    async def restart(self) -> bool:
        await self.stop()
        self.attempts = 0
        self.last_error = ""
        return await self.start()

    # ---------------- 状态 ----------------

    def snapshot(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "state": self.state,
            "running": self.running,
            "attempts": self.attempts,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "generation": self.generation,
            "events": self._event_count,
            "danmaku": self._danmaku_count,
            "transitions": list(self.transitions)[-10:],
        }

    def _transition(self, state: str, detail: str = "") -> None:
        if state == self.state and not detail:
            return
        self.state = state
        entry = {"at": time.time(), "state": state, "detail": detail, "generation": self.generation}
        self.transitions.append(entry)
        level = logger.info if state in (STATE_CONNECTED, STATE_STOPPED, STATE_DISABLED) else logger.warning
        level(f"弹幕连接[{self.room_id}] -> {state}{('：' + detail) if detail else ''}")

    # ---------------- 内部 ----------------

    @staticmethod
    def _default_factory(room_id: int, credential: Any) -> Any:
        """默认用 aiohttp 版弹幕客户端。

        不用 bilibili_api 的 LiveDanmaku: 它的 WebSocket 走 curl_cffi, 在部分容器里
        收到消息会 C 层 abort(整个进程挂掉, 实测必现)。HTTP 部分仍复用 bilibili_api
        (它带 wbi 签名, 拉弹幕服务器 token 不会被 -352 风控)。
        """
        from .bili_ws import AiohttpLiveDanmaku

        return AiohttpLiveDanmaku(room_display_id=room_id, credential=credential)

    async def _supervise(self) -> None:
        delay_index = 0
        while not self._stopping:
            self.attempts += 1
            self.generation += 1
            generation = self.generation
            self._transition(
                STATE_STARTING if self.attempts == 1 else STATE_RECONNECTING,
                f"第 {self.attempts} 次连接",
            )
            client = None
            try:
                client = self._client_factory(self.room_id, self.credential)
                self._client = client
                self._register(client, generation)
                await self._run_client(client)
                # connect() 正常返回说明连接已结束(断开/被服务端关闭)
                self.last_error = self.last_error or "连接已断开"
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self.last_error = f"连接超时（>{self.connect_timeout:.0f}s 没有握手成功）"
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                await self._stop_connect_task()
                if not self._stopping:
                    if self.state != STATE_ERROR:
                        self.last_error = self.last_error or "连接结束"
                    delay = self.retry_delays[min(delay_index, len(self.retry_delays) - 1)]
                    self._transition(STATE_WAITING, f"{delay:.0f} 秒后重试（{self.last_error}）")
                    await self._disconnect_client()
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                    delay_index = min(delay_index + 1, len(self.retry_delays) - 1)
                else:
                    await self._disconnect_client()
        self._transition(STATE_STOPPED, "已停止")

    async def _run_client(self, client: Any) -> None:
        """跑一次连接。

        LiveDanmaku.connect() 是**常驻循环**: 连上之后不会返回, 只有在断开/异常时才结束。
        所以不能用 wait_for 整体超时(会在连上 10 秒后把好连接掐掉), 而是:
          · 先等"握手成功"事件(带超时) —— 只有这一步算连接失败;
          · 握手成功后原样等 connect() 自然结束。
        另外 connect() 可能在握手前就抛错(房间不存在/网络不通), 这种要立刻重试, 不能干等超时。
        """
        self._connected_event = asyncio.Event()
        self._connect_task = asyncio.create_task(client.connect())
        connected = await self._wait_connected(self._connect_task)
        if not connected:
            self.last_error = self.last_error or "连接提前结束"
            return
        await self._connect_task

    async def _wait_connected(self, connect_task: asyncio.Task) -> bool:
        """等握手成功事件; connect_task 提前结束/报错/超时都立即返回。"""
        event_task = asyncio.create_task(self._connected_event.wait())  # type: ignore[union-attr]
        try:
            done, pending = await asyncio.wait(
                {connect_task, event_task},
                timeout=self.connect_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            event_task.cancel()
        if not done:
            raise asyncio.TimeoutError
        if connect_task in done:
            # 连接任务提前结束: 有异常就抛出去(交给上层记错误并重试)
            connect_task.result()
            return False
        return True

    async def _stop_connect_task(self) -> None:
        task = self._connect_task
        self._connect_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass

    def _register(self, client: Any, generation: int) -> None:
        """注册弹幕回调; 旧 generation 的回调直接丢弃。"""

        async def on_danmaku(event: Any) -> None:
            if generation != self.generation or self._stopping:
                return
            self._event_count += 1
            message = parse_danmaku_callback(event)
            if message is None:
                return
            if not message.room_id:
                message.room_id = self.room_id
            self._danmaku_count += 1
            if self.state != STATE_CONNECTED:
                self._transition(STATE_CONNECTED, "收到弹幕")
            if self.on_message is not None:
                try:
                    result = self.on_message(message)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:  # noqa: BLE001
                    logger.error(f"处理弹幕回调失败: {e}")

        async def on_task_exception(exc: Any) -> None:
            logger.warning(f"弹幕回调内部异常: {exc}")

        try:
            client.add_event_listener("DANMU_MSG", on_danmaku)
            client.add_event_listener("__TASK_EXCEPTION__", on_task_exception)
            client.add_event_listener("VERIFICATION_SUCCESSFUL", self._on_connected)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"注册弹幕事件失败: {e}")

    async def _on_connected(self, _event: Any = None) -> None:
        self.connected_at = time.time()
        if self.state != STATE_CONNECTED:
            self._transition(STATE_CONNECTED, "连接成功")
        if self._connected_event is not None:
            self._connected_event.set()

    async def _disconnect_client(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            if hasattr(client, "get_status"):
                established = getattr(client, "STATUS_ESTABLISHED", "established")
                if client.get_status() != established:
                    return
            if hasattr(client, "disconnect"):
                result = client.disconnect()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:  # noqa: BLE001
            logger.debug(f"关闭弹幕连接失败(忽略): {e}")
