"""
点歌队列: 入队限制 / 冷却 / 播放推进 / 历史。

设计原则:
  · 队列只管"排什么、放到哪一首", 不负责真正播放 —— 播放由 player 驱动;
  · 所有限制(时长/单人上限/队列上限/冷却/去重)都在 enqueue 里一次判完,
    返回明确原因, 便于给观众/管理员回不同的话;
  · 每次结构变化都落盘(重启不丢等待队列)并触发 on_change 回调(用于推送 Overlay)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import (
    HistoryEntry,
    QueueItem,
    Song,
)
from .store import QueueStore

# 入队失败原因
REASON_OK = "ok"
REASON_TOO_LONG = "too_long"
REASON_DUPLICATE = "duplicate"
REASON_USER_LIMIT = "user_limit"
REASON_COOLDOWN = "cooldown"
REASON_QUEUE_FULL = "queue_full"
REASON_NO_SONG = "no_song"


@dataclass
class EnqueueResult:
    ok: bool
    reason: str = REASON_OK
    position: int = 0            # 1-based 排队位次
    detail: str = ""             # 给用户看的说明(冷却剩余时间等)
    item: Optional[QueueItem] = None


@dataclass
class QueueSettings:
    """从插件配置映射而来(便于单测用默认值构造)"""

    max_queue_size: int = 30
    per_user_limit: int = 3
    cooldown_secs: int = 25
    max_duration_ms: int = 600_000
    history_limit: int = 25
    dedupe: bool = True

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "QueueSettings":
        def _int(key: str, default: int, low: int = 0, high: int = 10 ** 9) -> int:
            try:
                value = int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return cls(
            max_queue_size=_int("max_queue_size", 30, 1, 500),
            per_user_limit=_int("per_user_limit", 3, 1, 200),
            cooldown_secs=_int("cooldown", 25, 0, 86400),
            max_duration_ms=_int("max_duration", 600_000, 10_000, 4 * 3600 * 1000),
            history_limit=_int("history_limit", 25, 1, 500),
            dedupe=bool(cfg.get("dedupe", True)),
        )


class QueueManager:
    def __init__(self, settings: QueueSettings, store: Optional[QueueStore] = None):
        self.settings = settings
        self.store = store
        self.waiting: List[QueueItem] = []
        self.current: Optional[QueueItem] = None
        self.history: List[HistoryEntry] = []
        self.volume: int = 100
        self._last_request_at: Dict[str, float] = {}
        self.on_change: Optional[Callable[[], None]] = None
        if self.store is not None:
            self.restore()

    # ---------------- 查询 ----------------

    def __len__(self) -> int:
        return len(self.waiting)

    def pending_count(self) -> int:
        return len(self.waiting)

    def user_pending(self, requester_key: str) -> int:
        return sum(1 for item in self.waiting if item.requester_key == requester_key)

    def find_duplicate(self, song: Song) -> Optional[QueueItem]:
        for item in ([self.current] if self.current else []) + self.waiting:
            if item is not None and item.song.key == song.key:
                return item
        return None

    def cooldown_remaining(self, requester_key: str, now: Optional[float] = None) -> int:
        if self.settings.cooldown_secs <= 0:
            return 0
        last = self._last_request_at.get(requester_key, 0.0)
        elapsed = (now if now is not None else time.time()) - last
        remain = self.settings.cooldown_secs - elapsed
        return max(0, int(remain + 0.999)) if remain > 0 else 0

    # ---------------- 入队 ----------------

    def enqueue(
        self,
        song: Optional[Song],
        requester: str,
        requester_key: str,
        source: str = "danmaku",
        bypass_limits: bool = False,
        now: Optional[float] = None,
    ) -> EnqueueResult:
        """把歌加入等待队列。bypass_limits=True 用于管理员/面板手动点歌(跳过冷却与单人上限)。"""
        now = now if now is not None else time.time()
        if song is None or not song.title:
            return EnqueueResult(False, REASON_NO_SONG, detail="没识别出歌名")

        if self.settings.max_duration_ms > 0 and song.duration_ms > self.settings.max_duration_ms:
            minutes = self.settings.max_duration_ms / 60000
            return EnqueueResult(
                False, REASON_TOO_LONG,
                detail=f"这首歌太长（超过 {minutes:.0f} 分钟），换一首吧",
            )

        if self.settings.dedupe:
            dup = self.find_duplicate(song)
            if dup is not None:
                where = "正在播放" if self.current is dup else "已在队列里"
                return EnqueueResult(False, REASON_DUPLICATE, detail=f"这首歌{where}了：{song.display}")

        if not bypass_limits:
            if self.user_pending(requester_key) >= self.settings.per_user_limit:
                return EnqueueResult(
                    False, REASON_USER_LIMIT,
                    detail=f"你最多同时排 {self.settings.per_user_limit} 首，等播完再点吧",
                )
            remain = self.cooldown_remaining(requester_key, now)
            if remain > 0:
                return EnqueueResult(
                    False, REASON_COOLDOWN,
                    detail=f"点歌冷却中，还有 {remain} 秒",
                )

        if len(self.waiting) >= self.settings.max_queue_size:
            return EnqueueResult(False, REASON_QUEUE_FULL, detail="队列已满，等一会儿再点")

        item = QueueItem(
            song=song,
            requester=requester or "匿名",
            requester_key=requester_key,
            source=source,
            requested_at=now,
        )
        self.waiting.append(item)
        self._last_request_at[requester_key] = now
        position = len(self.waiting)
        self._changed()
        logger.info(
            f"点歌入队: {item.song.display} 由 {item.requester} ({source}), 当前队列 {position} 首"
        )
        return EnqueueResult(True, REASON_OK, position=position, item=item)

    # ---------------- 播放推进 ----------------

    def start_next(self) -> Optional[QueueItem]:
        """把队首设为当前播放项(若已有当前项则不覆盖)"""
        if self.current is not None:
            return self.current
        if not self.waiting:
            return None
        self.current = self.waiting.pop(0)
        self.current.status = "playing"
        self._changed()
        return self.current

    def finish_current(self, result: str = "done", note: str = "") -> Optional[HistoryEntry]:
        """当前项结束(播完/失败), 记入历史并清空 current"""
        if self.current is None:
            return None
        item = self.current
        item.status = "done" if result == "done" else result
        item.note = note
        entry = HistoryEntry(item=item, result=result)
        self.history.append(entry)
        self._trim_history()
        self.current = None
        self._changed()
        return entry

    def skip_current(self, note: str = "管理员跳过") -> Optional[HistoryEntry]:
        return self.finish_current(result="skipped", note=note)

    def fail_current(self, note: str = "") -> Optional[HistoryEntry]:
        return self.finish_current(result="failed", note=note or "播放失败")

    # ---------------- 队列操作 ----------------

    def clear_waiting(self) -> int:
        count = len(self.waiting)
        self.waiting.clear()
        if count:
            self._changed()
        return count

    def remove_at(self, index: int) -> Optional[QueueItem]:
        if 0 <= index < len(self.waiting):
            item = self.waiting.pop(index)
            self._changed()
            return item
        return None

    def move(self, src: int, dst: int) -> bool:
        if src == dst:
            return True
        if not (0 <= src < len(self.waiting)) or not (0 <= dst < len(self.waiting)):
            return False
        item = self.waiting.pop(src)
        self.waiting.insert(dst, item)
        self._changed()
        return True

    def move_up(self, index: int) -> bool:
        return self.move(index, index - 1)

    def move_down(self, index: int) -> bool:
        return self.move(index, index + 1)

    def set_volume(self, volume: int) -> int:
        self.volume = max(0, min(100, int(volume)))
        self._changed()
        return self.volume

    # ---------------- 快照 / 持久化 ----------------

    def snapshot(self, preview: int = 5) -> Dict[str, Any]:
        return {
            "current": self.current.to_dict() if self.current else None,
            "queue": [item.to_dict() for item in self.waiting[: max(0, preview)]],
            "queue_total": len(self.waiting),
            "history": [entry.to_dict() for entry in self.history[-5:]],
            "volume": self.volume,
        }

    def persist(self, current: Optional[QueueItem] = None) -> None:
        if self.store is None:
            return
        self.store.save_queue(self.waiting, self.history, self.volume, current or self.current)

    def restore(self) -> None:
        """从磁盘恢复: 等待队列 + 历史 + 音量; 上次没播完的歌放回队首。"""
        if self.store is None:
            return
        data = self.store.load_queue()
        self.waiting = list(data.get("queue") or [])
        self.history = list(data.get("history") or [])
        self.volume = int(data.get("volume") or 100)
        last = data.get("current")
        if last is not None:
            last.status = "waiting"
            last.note = "上次未播完，已放回队首"
            self.waiting.insert(0, last)
        self._trim_history()
        if self.waiting or self.history:
            logger.info(
                f"点歌台状态已恢复: 等待 {len(self.waiting)} 首, 历史 {len(self.history)} 条, 音量 {self.volume}"
            )

    def _trim_history(self) -> None:
        limit = max(1, self.settings.history_limit)
        if len(self.history) > limit:
            self.history = self.history[-limit:]

    def _changed(self) -> None:
        """结构变化: 落盘 + 通知(回调里不要抛异常影响主流程)"""
        try:
            self.persist()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"点歌台状态落盘失败: {e}")
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"点歌台变更回调失败: {e}")


def format_queue_line(index: int, item: QueueItem) -> str:
    """把队列表项格式化成一行(给 QQ 命令/日志复用)"""
    return f"{index}. {item.song.title} - {item.song.artist}（{item.requester}）"
