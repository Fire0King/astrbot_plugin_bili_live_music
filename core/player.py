"""
播放器状态机(OBS 浏览器源后端)。

职责边界:
  · 决定"什么时候把哪首歌交给浏览器播"(队列推进);
  · 跟踪 loading / playing / paused / ended 状态与播放位置;
  · 加载超时或播放报错时自动跳过, 不让一首坏歌卡住整个点歌台;
  · 状态变化统一广播给 Overlay(播放器页与展示页都靠它渲染)。

"谁在播"由 Overlay 页面自己声明(role=player), 没有播放器页连着时
状态停在 waiting_client —— 不会白白跳过歌曲, OBS 一开页就立刻接上。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from astrbot.api import logger

from .models import PlaybackState

STATUS_IDLE = "idle"
STATUS_WAITING_CLIENT = "waiting_client"
STATUS_LOADING = "loading"
STATUS_PLAYING = "playing"
STATUS_PAUSED = "paused"
STATUS_ERROR = "error"


@dataclass
class PlayerSettings:
    load_timeout_secs: float = 25.0
    state_broadcast_secs: float = 2.0


class PlayerService:
    def __init__(self, queue, hub, settings: Optional[PlayerSettings] = None, resolver=None):
        self.queue = queue
        self.hub = hub
        self.settings = settings or PlayerSettings()
        # resolver(song) -> 换一条可用直链; 用于"直链临时失效时换链重试"
        self.resolver = resolver
        self.status = STATUS_IDLE
        self.position_secs = 0.0
        self.error = ""
        self._load_started_at = 0.0
        self._last_broadcast = 0.0
        self._play_seq = 0
        self._tick_task: Optional[asyncio.Task] = None
        self._tasks: set = set()

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        if self._tick_task and not self._tick_task.done():
            return
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        task = self._tick_task
        self._tick_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"播放器 tick 异常: {e}")

    # ---------------- 状态 ----------------

    def state(self) -> PlaybackState:
        return PlaybackState(
            current=self.queue.current,
            status=self.status,
            position_secs=self.position_secs,
            duration_secs=((self.queue.current.song.duration_ms / 1000) if self.queue.current else 0),
            volume=self.queue.volume,
            error=self.error,
            queue_preview=[item.to_dict() for item in self.queue.waiting[:5]],
        )

    def snapshot(self) -> Dict[str, Any]:
        return self.state().to_dict()

    def _broadcast_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_broadcast) < self.settings.state_broadcast_secs:
            return
        self._last_broadcast = now
        self.hub.broadcast({"type": "state", "state": self.snapshot()})

    def _broadcast_volume(self) -> None:
        self.hub.broadcast({"type": "volume", "volume": int(self.queue.volume)})

    def notify_change(self) -> None:
        """队列被外部修改(面板操作)后立即推一次状态"""
        self._broadcast_state(force=True)

    def _emit_play(self, item) -> None:
        """下发播放指令, 并带上自增序号。

        序号的作用: 换链重试/切歌时, 旧音源被中断会再发一次 error/ended,
        这些"过期事件"带着旧序号回来时直接丢弃, 不会把新的一次播放判死。
        """
        self._play_seq += 1
        self.hub.broadcast({"type": "play", "item": item.to_dict(), "seq": self._play_seq})

    # ---------------- 队列推进 ----------------

    async def start_next(self) -> bool:
        """取下一首并让播放器页开始播; 返回是否真的开始了"""
        if self.queue.current is not None:
            return False
        item = self.queue.start_next()
        if item is None:
            self.status = STATUS_IDLE
            self._broadcast_state(force=True)
            return False
        self.status = STATUS_LOADING
        self.position_secs = 0.0
        self.error = ""
        self._load_started_at = time.time()
        logger.info(f"开始播放: {item.song.display}（{item.requester} 点）")
        self._emit_play(item)
        self._broadcast_state(force=True)
        return True

    async def play_now(self, item) -> None:
        """面板手动播放某一项(已入队)"""
        self.queue.current = item
        item.status = "playing"
        self.status = STATUS_LOADING
        self.position_secs = 0.0
        self._load_started_at = time.time()
        self._emit_play(item)
        self._broadcast_state(force=True)

    async def skip(self, reason: str = "跳过") -> bool:
        if self.queue.current is None:
            return False
        self.queue.skip_current(note=reason)
        self.status = STATUS_IDLE
        self.position_secs = 0.0
        logger.info(f"已跳过当前歌曲: {reason}")
        await self.start_next()
        return True

    async def pause(self) -> bool:
        if self.status not in (STATUS_PLAYING, STATUS_LOADING):
            return False
        self.hub.broadcast({"type": "pause"})
        self.status = STATUS_PAUSED
        self._broadcast_state(force=True)
        return True

    async def resume(self) -> bool:
        if self.status != STATUS_PAUSED or self.queue.current is None:
            return False
        self.hub.broadcast({"type": "resume", "item": self.queue.current.to_dict()})
        self.status = STATUS_PLAYING
        self._broadcast_state(force=True)
        return True

    async def stop_playback(self) -> None:
        self.hub.broadcast({"type": "stop"})
        if self.queue.current is not None:
            self.queue.finish_current(result="skipped", note="手动停止")
        self.status = STATUS_IDLE
        self.position_secs = 0.0
        self._broadcast_state(force=True)

    def set_volume(self, volume: int) -> int:
        value = self.queue.set_volume(volume)
        self._broadcast_volume()
        self._broadcast_state(force=True)
        return value

    # ---------------- 客户端消息 ----------------

    def on_client_message(self, role: str, message: Dict[str, Any]) -> None:
        """处理播放器页回传的事件(loading/playing/ended/progress/error)。"""
        if not isinstance(message, dict):
            return
        kind = str(message.get("type") or "")
        # 过期事件(来自已经被替换掉的音源)直接丢弃
        seq = message.get("seq")
        if seq is not None:
            try:
                if int(seq) != self._play_seq:
                    return
            except (TypeError, ValueError):
                pass
        if kind == "loading":
            self.status = STATUS_LOADING
            self._load_started_at = time.time()
        elif kind == "playing":
            self.status = STATUS_PLAYING
            self.position_secs = 0.0
            if self.queue.current:
                self.queue.current.status = "playing"
            self._broadcast_state(force=True)
        elif kind == "progress":
            try:
                self.position_secs = max(0.0, float(message.get("position") or 0))
            except (TypeError, ValueError):
                pass
        elif kind == "ended":
            self._finish_current("done")
        elif kind == "error":
            # 直链偶尔会临时失效(第三方节点/CDN), 先换一条链重试一次再放弃
            self._spawn(self._handle_play_error(str(message.get("message") or "播放失败")))
        self._broadcast_state()

    async def _handle_play_error(self, note: str) -> None:
        item = self.queue.current
        if item is None:
            return
        if self.resolver is not None and not item.retried:
            item.retried = True
            logger.warning(f"播放失败({note}), 尝试换一条直链重试: {item.song.display}")
            try:
                resolved = await self.resolver(item.song)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"换链重试失败: {e}")
                resolved = None
            if resolved is not None and resolved.url and resolved.url != item.song.url:
                item.song = resolved
                self.status = STATUS_LOADING
                self.position_secs = 0.0
                self._load_started_at = time.time()
                self._emit_play(item)
                self._broadcast_state(force=True)
                return
        self._finish_current("failed", note)

    def on_client_connected(self, role: str) -> None:
        """播放器页连上(或重连): 把当前状态和正在播的歌再推一遍, 便于页面自愈。"""
        self.hub.broadcast({"type": "role", "role": role, "backend": "browser"})
        if self.queue.current is not None:
            if self.status in (STATUS_LOADING, STATUS_PLAYING, STATUS_PAUSED):
                self._emit_play(self.queue.current)
        self._broadcast_state(force=True)
        self.hub.broadcast({"type": "volume", "volume": int(self.queue.volume)})

    def _finish_current(self, result: str, note: str = "") -> None:
        if self.queue.current is None:
            return
        title = self.queue.current.song.display
        if result == "done":
            self.queue.finish_current("done")
            logger.info(f"播放结束: {title}")
        else:
            self.queue.fail_current(note or "播放失败")
            logger.warning(f"播放失败({note or '未知原因'}): {title}, 自动跳过")
        self.status = STATUS_IDLE
        self.position_secs = 0.0
        self.error = "" if result == "done" else (note or "播放失败")
        # 让页面立刻停掉旧音频(否则换链重试/跳过之后, 页面还在放上一首)
        self.hub.broadcast({"type": "stop"})

    # ---------------- 周期检查 ----------------

    async def tick(self) -> None:
        # 没有播放器页时不急着换歌, 等 OBS 打开
        if self.queue.current is None:
            if self.hub.has_player:
                await self.start_next()
            else:
                if self.status != STATUS_WAITING_CLIENT and self.queue.waiting:
                    self.status = STATUS_WAITING_CLIENT
                    self._broadcast_state(force=True)
                return
        if self.status == STATUS_LOADING:
            if self._load_started_at and (time.time() - self._load_started_at) > self.settings.load_timeout_secs:
                logger.warning(f"加载超时({self.settings.load_timeout_secs:.0f}s), 跳过当前歌曲")
                if self.queue.current is not None:
                    self.queue.fail_current("加载超时")
                self.status = STATUS_IDLE
                await self.start_next()
                return
        self._broadcast_state()
