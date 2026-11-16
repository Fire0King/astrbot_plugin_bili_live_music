"""
点歌台的核心数据模型。

把这些结构单独放一层, 是为了让"队列 / 音乐源 / 播放后端 / Overlay"四块
互相只通过数据结构通信 —— 后续换音源或换播放方式时不用改动其他模块。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 点歌来源
SOURCE_DANMAKU = "danmaku"   # B 站直播间弹幕
SOURCE_BOT = "bot"           # QQ 群 / 私聊命令
SOURCE_PANEL = "panel"       # 控制面板手动添加


@dataclass
class Song:
    """一首歌(搜索结果的统一形态)"""

    source: str                      # netease / tencent / kugou
    song_id: str                     # 各音源自己的 ID
    title: str
    artist: str = "未知歌手"
    album: str = ""
    duration_ms: int = 0
    cover: str = ""                  # 封面 URL(可为空)
    url: str = ""                    # 播放直链(解析后才有)
    extra: Dict[str, Any] = field(default_factory=dict)   # mid / hash / albumId 等音源私有字段

    @property
    def key(self) -> str:
        """用于去重的唯一标识"""
        return f"{self.source}:{self.song_id}"

    @property
    def display(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "song_id": str(self.song_id),
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": int(self.duration_ms or 0),
            "cover": self.cover or "",
            "url": self.url or "",
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Song":
        return cls(
            source=str(data.get("source") or ""),
            song_id=str(data.get("song_id") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or "未知歌手"),
            album=str(data.get("album") or ""),
            duration_ms=int(data.get("duration_ms") or 0),
            cover=str(data.get("cover") or ""),
            url=str(data.get("url") or ""),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class QueueItem:
    """队列里的一首歌"""

    song: Song
    requester: str                   # 展示用昵称
    requester_key: str               # 去重/限流用(uid 或 name)
    source: str = SOURCE_DANMAKU
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    requested_at: float = field(default_factory=time.time)
    status: str = "waiting"          # waiting / playing / done / skipped / failed
    note: str = ""                   # 失败原因等
    retried: bool = False            # 直链失败后是否已经换链重试过一次

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "song": self.song.to_dict(),
            "requester": self.requester,
            "requester_key": self.requester_key,
            "source": self.source,
            "requested_at": self.requested_at,
            "status": self.status,
            "note": self.note,
            "retried": self.retried,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        item = cls(
            song=Song.from_dict(data.get("song") or {}),
            requester=str(data.get("requester") or ""),
            requester_key=str(data.get("requester_key") or ""),
            source=str(data.get("source") or SOURCE_DANMAKU),
            item_id=str(data.get("item_id") or uuid.uuid4().hex[:12]),
            requested_at=float(data.get("requested_at") or time.time()),
            status=str(data.get("status") or "waiting"),
            note=str(data.get("note") or ""),
            retried=bool(data.get("retried", False)),
        )
        return item


@dataclass
class HistoryEntry:
    """历史上播过 / 被跳过的一首"""

    item: QueueItem
    finished_at: float = field(default_factory=time.time)
    result: str = "done"             # done / skipped / failed

    def to_dict(self) -> Dict[str, Any]:
        return {"item": self.item.to_dict(), "finished_at": self.finished_at, "result": self.result}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HistoryEntry":
        return cls(
            item=QueueItem.from_dict(data.get("item") or {}),
            finished_at=float(data.get("finished_at") or time.time()),
            result=str(data.get("result") or "done"),
        )


@dataclass
class PlaybackState:
    """当前播放状态(推给 Overlay 的 state 消息)"""

    current: Optional[QueueItem] = None
    status: str = "idle"             # idle / loading / playing / paused / ended / error
    position_secs: float = 0.0
    duration_secs: float = 0.0
    volume: int = 100
    error: str = ""
    queue_preview: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current": self.current.to_dict() if self.current else None,
            "status": self.status,
            "position": round(float(self.position_secs or 0), 2),
            "duration": round(float(self.duration_secs or 0), 2),
            "volume": int(self.volume),
            "error": self.error,
            "queue": list(self.queue_preview),
        }
