"""
控制面板后端逻辑。

把"队列 / 播放器 / 音源 / 弹幕"四块能力收拢成:
  · snapshot() —— 面板与 WebSocket 用的一份完整快照;
  · action()   —— 面板按钮对应的操作(下一首/暂停/清空/搜索/点歌/重连…)。

放在独立模块而不是塞进 main.py, 是因为这块逻辑最多变(后续加"置顶/收藏/点歌历史"
只需要加 action), 单独一个文件更容易测、也不会把插件入口撑爆。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from astrbot.api import logger

from .models import SOURCE_PANEL, Song
from .player import PlayerService
from .queue_manager import QueueManager


class PanelService:
    def __init__(
        self,
        queue: QueueManager,
        player: PlayerService,
        music: Any,
        danmaku: Any = None,
        urls_provider: Optional[Callable[[], Dict[str, str]]] = None,
    ):
        self.queue = queue
        self.player = player
        self.music = music
        self.danmaku = danmaku
        self.urls_provider = urls_provider

    # ---------------- 快照 ----------------

    def snapshot(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "player": self.player.snapshot(),
            "queue": [item.to_dict() for item in self.queue.waiting],
            "queue_total": len(self.queue.waiting),
            "history": [entry.to_dict() for entry in self.queue.history[-10:]],
            "volume": self.queue.volume,
            "danmaku": self.danmaku.snapshot() if self.danmaku is not None else {},
            "music": {
                "backend": getattr(getattr(self.music, "settings", None), "backend", "?"),
                "sources": getattr(getattr(self.music, "settings", None), "luoyue_sources", []),
                "describe": self.music.describe() if hasattr(self.music, "describe") else "",
            },
            "now": time.time(),
        }
        if self.urls_provider is not None:
            try:
                snapshot["urls"] = self.urls_provider()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"生成 Overlay 地址失败: {e}")
                snapshot["urls"] = {}
        return snapshot

    # ---------------- 操作 ----------------

    async def action(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = payload or {}
        if name in ("refresh", "state"):
            return {"snapshot": self.snapshot()}
        if name in ("next", "skip"):
            await self.player.skip("面板跳过")
            return self._ok()
        if name == "pause":
            return {"changed": await self.player.pause()}
        if name == "resume":
            return {"changed": await self.player.resume()}
        if name == "stop":
            await self.player.stop_playback()
            return self._ok()
        if name == "volume":
            value = self.player.set_volume(int(payload.get("value") or 0))
            return {"volume": value}
        if name == "clear":
            removed = self.queue.clear_waiting()
            self.player.notify_change()
            return {"removed": removed}
        if name == "remove":
            index = self._index(payload)
            item = self.queue.remove_at(index) if index is not None else None
            self.player.notify_change()
            return {"removed": item.song.display if item else None}
        if name == "move":
            try:
                src = int(payload.get("from"))
                dst = int(payload.get("to"))
            except (TypeError, ValueError):
                return self._err("缺少移动目标")
            moved = self.queue.move(src, dst)
            self.player.notify_change()
            return {"moved": moved}
        if name == "up":
            index = self._index(payload)
            moved = self.queue.move_up(index) if index is not None else False
            self.player.notify_change()
            return {"moved": moved}
        if name == "down":
            index = self._index(payload)
            moved = self.queue.move_down(index) if index is not None else False
            self.player.notify_change()
            return {"moved": moved}
        if name == "search":
            keyword = str(payload.get("keyword") or "").strip()
            if not keyword:
                return self._err("请输入歌名")
            limit = int(payload.get("limit") or getattr(self.music.settings, "panel_search_limit", 20))
            songs = await self.music.search(keyword, limit)
            return {"items": [song.to_dict() for song in songs], "keyword": keyword}
        if name == "add":
            return await self._add(payload)
        if name == "danmaku_restart":
            if self.danmaku is None:
                return self._err("弹幕监听未启用")
            ok = await self.danmaku.restart()
            return {"ok": ok, "danmaku": self.danmaku.snapshot()}
        return self._err(f"未知操作: {name}")

    async def _add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_song = payload.get("song")
        if isinstance(raw_song, dict):
            song = Song.from_dict(raw_song)
        else:
            keyword = str(payload.get("keyword") or "").strip()
            if not keyword:
                return self._err("缺少歌曲信息")
            song = await self.music.pick(keyword)
        if not song.url:
            resolved = await self.music.resolve(song)
            if resolved is None or not resolved.url:
                return self._err(f"拿不到播放地址: {song.display}")
            song = resolved
        result = self.queue.enqueue(song, "面板", "panel:admin", source=SOURCE_PANEL, bypass_limits=True)
        if not result.ok:
            return self._err(result.detail or "加入队列失败")
        self.player.notify_change()
        if self.queue.current is None:
            await self.player.tick()
        return {"item": result.item.to_dict() if result.item else None, "position": result.position}

    # ---------------- 客户端事件 ----------------

    def on_client_message(self, role: str, message: Dict[str, Any]) -> None:
        self.player.on_client_message(role, message)

    def on_client_connected(self, role: str) -> None:
        self.player.on_client_connected(role)

    # ---------------- 工具 ----------------

    @staticmethod
    def _index(payload: Dict[str, Any]) -> Optional[int]:
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            return None
        return index if index >= 0 else None

    @staticmethod
    def _ok() -> Dict[str, Any]:
        return {"ok": True}

    @staticmethod
    def _err(message: str) -> Dict[str, Any]:
        logger.warning(f"面板操作失败: {message}")
        return {"ok": False, "error": message}
