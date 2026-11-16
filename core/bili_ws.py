"""
B 站直播间弹幕 WebSocket 客户端（aiohttp 实现）。

为什么不用 `bilibili_api.live.LiveDanmaku`:
  该库的 WebSocket 走 **curl_cffi**，在部分容器环境里 `ws_recv` 会直接触发
  `Fatal Python error: Aborted`（C 层崩溃，整个进程挂掉）—— 实测本机 100% 复现。
  弹幕一多进程就没了，这对"直播点歌"是致命的。

本模块只替换传输层:
  · 房间号 / 弹幕服务器 token 仍用 bilibili_api 的 HTTP 接口（它自带 wbi 签名，能过风控）；
  · WebSocket 用 aiohttp；协议解析（zlib/brotli + JSON）自己实现；
  · 对外暴露与 LiveDanmaku 相同的最小接口（connect/add_event_listener/disconnect/get_status），
    这样上层 DanmakuService 的重连/状态机完全不用改。

协议要点（B 站直播弹幕）:
  包 = 16 字节头 + body，头为 >IHHII = (总长, 头长, 协议版本, 操作码, 序号)
  操作码: 2=心跳, 3=心跳回应(人气值), 5=消息, 7=认证, 8=认证回应
  协议版本: 0/1=明文 JSON, 2=zlib 压缩 JSON, 3=brotli 压缩 JSON
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_PLAIN = 0
PROTO_ZLIB = 2
PROTO_BROTLI = 3

HEADER = struct.Struct(">IHHII")
HEARTBEAT_BODY = b"[object Object]"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
WS_HEADERS = {
    "User-Agent": UA,
    "Origin": "https://live.bilibili.com",
    "Referer": "https://live.bilibili.com/",
}

STATUS_INIT = "init"
STATUS_CONNECTING = "connecting"
STATUS_ESTABLISHED = "established"
STATUS_CLOSING = "closing"
STATUS_CLOSED = "closed"
STATUS_ERROR = "error"


def build_packet(op: int, body: bytes, proto: int = 0) -> bytes:
    """组装一个 B 站弹幕包"""
    return HEADER.pack(HEADER.size + len(body), HEADER.size, proto, op, 1) + body


def parse_packet(raw: bytes) -> Tuple[int, int, bytes]:
    """解析单个包 -> (op, proto, body)。头不完整时抛 ValueError"""
    if len(raw) < HEADER.size:
        raise ValueError("包长度不足")
    total, hlen, proto, op, _seq = HEADER.unpack(raw[: HEADER.size])
    total = min(total, len(raw))
    return op, proto, raw[hlen:total]


def _json_values(text: str) -> List[dict]:
    """从一个字符串里解析出所有 JSON 对象（兼容拼接的多个 JSON）"""
    decoder = json.JSONDecoder()
    values: List[dict] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \r\n\t\x00":
            index += 1
        if index >= length:
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            break
        index = end
        if isinstance(value, dict):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, dict))
    return values


def _split_packets(payload: bytes) -> List[bytes]:
    """把解压后的负载按 pbbp2 包头切成若干个 body。

    B 站压缩帧里装的**不是裸 JSON**, 而是"包头(16 字节) + JSON body"的重复序列,
    所以解压之后还要再切一次包 —— 这正是之前"连上了却一条弹幕都收不到"的原因。
    """
    bodies: List[bytes] = []
    offset = 0
    total_len = len(payload)
    while offset + HEADER.size <= total_len:
        total, hlen, _ver, _op, _seq = HEADER.unpack(payload[offset: offset + HEADER.size])
        if total < HEADER.size or hlen < HEADER.size or offset + total > total_len:
            break
        bodies.append(payload[offset + hlen: offset + total])
        offset += total
    return bodies


def decode_messages(proto: int, body: bytes) -> List[dict]:
    """按协议版本解压并解析出 JSON 消息列表"""
    payload = body
    try:
        if proto == PROTO_ZLIB:
            payload = zlib.decompress(body)
        elif proto == PROTO_BROTLI:
            try:
                import brotli  # type: ignore
            except Exception as e:  # noqa: BLE001
                logger.warning(f"缺少 brotli 解压库, 跳过一帧弹幕: {e}")
                return []
            payload = brotli.decompress(body)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"弹幕解压失败(proto={proto}): {e}")
        return []

    # 解压后可能还是"包头 + JSON"的序列; 也可能是裸 JSON
    chunks = _split_packets(payload)
    if not chunks:
        chunks = [payload]
    messages: List[dict] = []
    for chunk in chunks:
        text = chunk.decode("utf-8", "ignore").strip()
        if not text:
            continue
        messages.extend(_json_values(text))
    return messages


def normalize_cmd(cmd: Any) -> str:
    """B 站的 cmd 可能带后缀(DANMU_MSG:4:0:2:2:2:0), 这里归一成事件名"""
    text = str(cmd or "")
    if "DANMU_MSG" in text:
        return "DANMU_MSG"
    if "RECALL_DANMU_MSG" in text:
        return "RECALL_DANMU_MSG"
    return text


class AiohttpLiveDanmaku:
    """直播弹幕客户端（接口对齐 bilibili_api.live.LiveDanmaku）"""

    def __init__(self, room_display_id: int, credential: Any = None,
                 connect_timeout: float = 10.0, heartbeat: float = 30.0,
                 recv_timeout: float = 90.0):
        self.room_display_id = int(room_display_id or 0)
        self.credential = credential
        self.connect_timeout = max(3.0, float(connect_timeout))
        self.heartbeat_interval = max(5.0, float(heartbeat))
        self.recv_timeout = max(30.0, float(recv_timeout))
        self._handlers: Dict[str, List[Any]] = {}
        self._tasks: set = set()
        self._session: Any = None
        self._ws: Any = None
        self._status = STATUS_INIT
        self._hb_task: Optional[asyncio.Task] = None

    # ---------------- 事件接口(对齐 AsyncEvent) ----------------

    def add_event_listener(self, name: str, handler: Any) -> None:
        self._handlers.setdefault(name.upper(), []).append(handler)

    def on(self, event_name: str):
        def decorator(func):
            self.add_event_listener(event_name, func)
            return func
        return decorator

    def dispatch(self, name: str, *args, **kwargs) -> None:
        for handler in list(self._handlers.get(name.upper(), [])):
            try:
                result = handler(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.error(f"弹幕事件处理异常({name}): {e}")
                continue
            if asyncio.iscoroutine(result):
                task = asyncio.create_task(result)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def get_status(self) -> str:
        return self._status

    # ---------------- 连接 ----------------

    async def connect(self) -> None:
        """连接并持续收弹幕；连接结束(正常/异常)才返回, 由上层决定是否重连。"""
        import aiohttp

        self._status = STATUS_CONNECTING
        real_room, token, hosts, _cookie = await self._fetch_endpoint()
        if not hosts:
            raise RuntimeError("没有可用的弹幕服务器")

        # 先按登录身份连(有 DedeUserID 才用), 失败再退回匿名 —— 只有 SESSDATA
        # 而没有 uid 的"半套凭据"会被服务端无视, 表现为认证包(不回 op=8)
        attempts: List[Tuple[int, str, Dict[str, str], str]] = []
        uid, buvid, jar = self._identity()
        if uid > 0:
            attempts.append((uid, buvid, jar, "登录"))
        attempts.append((0, "", {}, "匿名"))

        last_error = ""
        for ident_uid, ident_buvid, cookies, label in attempts:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                self._session = session
                for host in hosts[:4]:
                    uri = f"wss://{host['host']}:{host.get('wss_port') or 443}/sub"
                    try:
                        async with session.ws_connect(uri, headers=WS_HEADERS,
                                                      heartbeat=None, max_msg_size=0) as ws:
                            self._ws = ws
                            await self._authenticate(ws, real_room, token, ident_uid, ident_buvid)
                            self._status = STATUS_ESTABLISHED
                            self.dispatch("VERIFICATION_SUCCESSFUL", {"type": "VERIFICATION_SUCCESSFUL"})
                            logger.info(
                                f"弹幕已连接({label}): {uri}（房间 {self.room_display_id}）"
                            )
                            self._hb_task = asyncio.create_task(self._heartbeat_loop(ws))
                            await self._recv_loop(ws)
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        last_error = f"{type(e).__name__}: {e}"
                        logger.warning(
                            f"弹幕服务器 {host.get('host')} 连接失败({label}): {last_error}"
                        )
                        await self._stop_heartbeat()
                        self._ws = None
                        continue
        self._status = STATUS_ERROR
        raise RuntimeError(last_error or "所有弹幕服务器都连接失败")

    def _identity(self) -> Tuple[int, str, Dict[str, str]]:
        """返回 (uid, buvid, cookie)；没有有效 uid 时按匿名处理。"""
        cred = self.credential
        if cred is None:
            return 0, "", {}
        try:
            uid = int(getattr(cred, "dedeuserid", 0) or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid <= 0:
            return 0, "", {}
        buvid = str(getattr(cred, "buvid3", "") or "")
        cookie: Dict[str, str] = {}
        for attr, key in (("sessdata", "SESSDATA"), ("bili_jct", "bili_jct"),
                          ("dedeuserid", "DedeUserID"), ("buvid3", "buvid3")):
            value = getattr(cred, attr, None)
            if value:
                cookie[key] = str(value)
        return uid, buvid, cookie

    async def disconnect(self) -> None:
        self._status = STATUS_CLOSING
        await self._stop_heartbeat()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._status = STATUS_CLOSED

    # ---------------- 内部 ----------------

    async def _fetch_endpoint(self) -> Tuple[int, str, List[dict], Dict[str, str]]:
        """用 bilibili_api 的 HTTP 接口拿 房间号 + token + 服务器列表（带 wbi 签名, 不会被 -352）"""
        from bilibili_api import live

        room = live.LiveRoom(room_display_id=self.room_display_id, credential=self.credential)
        real_room = int(await room.get_room_id())
        info = await room.get_danmu_info()
        # bilibili_api 的 Api.result 会直接返回 data 内容(不再是 {"data": ...}),
        # 两种形态都兼容, 否则会拿不到 token/host_list
        data = info.get("data") if isinstance(info.get("data"), dict) else info
        token = str(data.get("token") or "")
        hosts = [h for h in (data.get("host_list") or []) if isinstance(h, dict) and h.get("host")]
        cookie: Dict[str, str] = {}
        cred = self.credential
        if cred is not None:
            for attr, key in (("sessdata", "SESSDATA"), ("bili_jct", "bili_jct"),
                              ("dedeuserid", "DedeUserID"), ("buvid3", "buvid3")):
                value = getattr(cred, attr, None)
                if value:
                    cookie[key] = str(value)
        return real_room, token, hosts, cookie

    async def _authenticate(self, ws: Any, real_room: int, token: str,
                            uid: int = 0, buvid: str = "") -> None:
        auth = {
            "uid": int(uid or 0),
            "roomid": real_room,
            "protover": PROTO_BROTLI if _has_brotli() else PROTO_ZLIB,
            "platform": "web",
            "type": 2,
            "key": token,
            "buvid": buvid or "",
        }
        await ws.send_bytes(build_packet(OP_AUTH, json.dumps(auth).encode("utf-8")))
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(ws.receive(), timeout=max(1.0, deadline - time.monotonic()))
            if msg.type.name != "BINARY":
                continue
            op, _proto, _body = parse_packet(msg.data)
            if op == OP_AUTH_REPLY:
                return
        raise TimeoutError("认证超时（未收到 op=8）")

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                if ws.closed:
                    return
                await ws.send_bytes(build_packet(OP_HEARTBEAT, HEARTBEAT_BODY))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug(f"弹幕心跳发送失败(忽略): {e}")

    async def _recv_loop(self, ws: Any) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=self.recv_timeout)
            except asyncio.TimeoutError:
                logger.warning(f"弹幕 {self.recv_timeout:.0f} 秒没收到任何数据, 断开重连")
                self._status = STATUS_ERROR
                return
            kind = getattr(msg.type, "name", str(msg.type))
            if kind == "BINARY":
                try:
                    op, proto, body = parse_packet(msg.data)
                except ValueError:
                    continue
                if op != OP_MESSAGE:
                    continue
                for payload in decode_messages(proto, body):
                    cmd = normalize_cmd(payload.get("cmd"))
                    if cmd:
                        self.dispatch(cmd, {"type": cmd, "data": payload})
            elif kind in ("CLOSE", "CLOSING", "CLOSED"):
                self._status = STATUS_CLOSED
                return
            elif kind == "ERROR":
                self._status = STATUS_ERROR
                return

    async def _stop_heartbeat(self) -> None:
        task = self._hb_task
        self._hb_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass


def _has_brotli() -> bool:
    try:
        import brotli  # type: ignore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
