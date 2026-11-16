"""
点歌台的持久化。

只保存"重启后还想恢复"的东西: 等待队列、播放历史、音量、最近一次播放项。
播放进度/暂停状态不落盘 —— 重启后不假装还在放, 而是把当前项放回队首重新播。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .models import HistoryEntry, QueueItem, Song


class QueueStore:
    """`<data_dir>/state.json` 的读写(原子写, 0600)"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else Path.cwd() / "state.json"

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                return {}
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取点歌台状态失败(忽略, 从空开始): {e}")
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存点歌台状态失败: {e}")

    # ---------------- 便捷读写 ----------------

    def save_queue(
        self,
        queue: List[QueueItem],
        history: List[HistoryEntry],
        volume: int,
        current: Optional[QueueItem] = None,
    ) -> None:
        self.save({
            "version": 1,
            "saved_at": time.time(),
            "queue": [item.to_dict() for item in queue],
            "history": [entry.to_dict() for entry in history],
            "volume": int(volume),
            # 只记 id 与歌曲, 重启后放回队首重播
            "current": current.to_dict() if current else None,
        })

    def load_queue(self) -> Dict[str, Any]:
        """返回 {queue, history, volume, current}"""
        data = self.load()
        queue = [QueueItem.from_dict(x) for x in (data.get("queue") or []) if isinstance(x, dict)]
        history = [HistoryEntry.from_dict(x) for x in (data.get("history") or []) if isinstance(x, dict)]
        volume = int(data.get("volume") or 100)
        current = data.get("current")
        cur_item = QueueItem.from_dict(current) if isinstance(current, dict) else None
        return {"queue": queue, "history": history, "volume": volume, "current": cur_item}
