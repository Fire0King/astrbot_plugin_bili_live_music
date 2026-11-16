# -*- coding: utf-8 -*-
"""队列 / 持久化 单测（不联网, 不依赖 AstrBot 运行时）。"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

# ---- astrbot.api.logger stub ----
import types

fake = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        pass

    def error(self, m):
        pass


api.logger = _Logger()
fake.api = api
sys.modules.update({"astrbot": fake, "astrbot.api": api})

from core.models import HistoryEntry, QueueItem, Song          # noqa: E402
from core.queue_manager import (                             # noqa: E402
    QueueManager,
    QueueSettings,
    REASON_COOLDOWN,
    REASON_DUPLICATE,
    REASON_QUEUE_FULL,
    REASON_TOO_LONG,
    REASON_USER_LIMIT,
)
from core.store import QueueStore                            # noqa: E402

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


def song(name, seconds=200, sid=None, source="netease"):
    return Song(
        source=source,
        song_id=sid or name,
        title=name,
        artist="测试歌手",
        duration_ms=seconds * 1000,
    )


settings = QueueSettings(
    max_queue_size=5, per_user_limit=2, cooldown_secs=25,
    max_duration_ms=600_000, history_limit=3, dedupe=True,
)
tmp = Path(tempfile.mkdtemp())
store = QueueStore(tmp / "state.json")
q = QueueManager(settings, store)
changes = []
q.on_change = lambda: changes.append(1)

# 1) 正常入队
r = q.enqueue(song("晴天"), "小明", "uid:1", now=1000)
check("正常入队成功", r.ok and r.position == 1 and len(q.waiting) == 1)
check("入队触发变更回调", len(changes) == 1)

# 2) 冷却
r = q.enqueue(song("七里香"), "小明", "uid:1", now=1010)
check("冷却拦下第二首", not r.ok and r.reason == REASON_COOLDOWN)
check("冷却剩余时间正确", "15" in r.detail)  # 25 - 10 = 15

# 3) 冷却期内但换了人
r = q.enqueue(song("七里香"), "小红", "uid:2", now=1010)
check("换个人可以点", r.ok and r.position == 2)

# 4) 去重
r = q.enqueue(song("晴天"), "小红", "uid:2", now=2000)
check("重复歌曲被拦下", not r.ok and r.reason == REASON_DUPLICATE)

# 5) 管理员绕过限制
r = q.enqueue(song("夜曲"), "管理员", "admin", bypass_limits=True, now=1011)
check("绕过限制可以入队", r.ok and r.position == 3)
r = q.enqueue(song("稻香"), "管理员", "admin", bypass_limits=True, now=1012)
check("绕过限制可以继续入队", r.ok and r.position == 4)

# 6) 单人上限(不同歌曲, 冷却过后)
q._last_request_at.clear()
r = q.enqueue(song("发如雪", sid="s5"), "小红", "uid:2", now=3000)
check("第二首成功(未超单人上限)", r.ok)
q._last_request_at.clear()
r = q.enqueue(song("菊花台", sid="s6"), "小红", "uid:2", now=3100)
check("单人上限拦下第三首", not r.ok and r.reason == REASON_USER_LIMIT)

# 7) 队列上限
q2 = QueueManager(QueueSettings(max_queue_size=1, per_user_limit=10, cooldown_secs=0))
q2.enqueue(song("甲", sid="a"), "u", "k1")
r = q2.enqueue(song("乙", sid="b"), "u", "k2")
check("队列满被拦下", not r.ok and r.reason == REASON_QUEUE_FULL)

# 8) 单曲时长保护
q3 = QueueManager(QueueSettings(max_duration_ms=60_000, cooldown_secs=0))
r = q3.enqueue(song("超长", seconds=120, sid="long"), "u", "k")
check("超长歌曲被拦下", not r.ok and r.reason == REASON_TOO_LONG)
check("超长提示含分钟数", "1" in r.detail)

# 9) 空歌
r = q3.enqueue(None, "u", "k")
check("空歌返回失败", not r.ok)

# 10) 播放推进 + 历史
first = q.start_next()
check("start_next 返回队首", first is not None and first.song.title == "晴天")
check("current 已设置", q.current is first and len(q.waiting) == 4)
check("重复 start_next 不换歌", q.start_next() is first)
entry = q.finish_current("done")
check("finish 记入历史", entry is not None and entry.result == "done" and q.current is None)
check("历史长度受限制", len(q.history) <= 3)

# 11) 跳过
q.start_next()
skipped = q.skip_current()
check("跳过标记为 skipped", skipped is not None and skipped.result == "skipped")

# 12) 失败
q.start_next()
failed = q.fail_current("链接失效")
check("失败标记为 failed", failed is not None and failed.result == "failed" and "链接" in failed.item.note)

# 13) 清空不影响当前
q.start_next()
current = q.current
cleared = q.clear_waiting()
check("清空返回条数", cleared == len(q.waiting) + cleared and q.waiting == [])
check("清空不动当前播放", q.current is current)

# 14) 移动 / 删除
q4 = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0))
for i in range(3):
    q4.enqueue(song(f"歌{i}", sid=f"m{i}"), "u", f"k{i}")
ok = q4.move(0, 2)
check("移动到队尾", ok and [i.song.title for i in q4.waiting] == ["歌1", "歌2", "歌0"])
check("上移一位", q4.move_up(2) and [i.song.title for i in q4.waiting] == ["歌1", "歌0", "歌2"])
check("越界移动返回 False", not q4.move(0, 9))
removed = q4.remove_at(1)
check("删除指定项", removed is not None and removed.song.title == "歌0" and len(q4.waiting) == 2)
check("越界删除返回 None", q4.remove_at(9) is None)

# 15) 音量
check("音量夹取上限", q4.set_volume(500) == 100)
check("音量夹取下限", q4.set_volume(-5) == 0)

# 16) 快照
snap = q4.snapshot(preview=1)
check("快照含 current/queue/volume", set(["current", "queue", "queue_total", "volume"]) <= set(snap))
check("快照预览数量受控", len(snap["queue"]) == 1)

# 17) 持久化 + 恢复
q5 = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0),
                  QueueStore(tmp / "state2.json"))
q5.enqueue(song("持久1", sid="p1"), "u", "k1")
q5.enqueue(song("持久2", sid="p2"), "u", "k2")
q5.set_volume(66)
q5.start_next()
q5.skip_current()
q5.persist()

q6 = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0),
                  QueueStore(tmp / "state2.json"))
check("恢复等待队列", [i.song.title for i in q6.waiting] == ["持久2"])
check("恢复音量", q6.volume == 66)
check("恢复历史", len(q6.history) == 1)

# 18) 重启时未播完的歌放回队首
q7 = QueueManager(QueueSettings(max_queue_size=10), QueueStore(tmp / "state3.json"))
q7.enqueue(song("重启1", sid="r1"), "u", "k1")
q7.start_next()
q7.persist(q7.current)
q8 = QueueManager(QueueSettings(max_queue_size=10), QueueStore(tmp / "state3.json"))
check("未播完的歌回到队首", q8.waiting and q8.waiting[0].song.title == "重启1")

# 19) 坏文件不崩
bad = tmp / "bad.json"
bad.write_text("{not json", encoding="utf-8")
q9 = QueueManager(QueueSettings(), QueueStore(bad))
check("坏状态文件不抛异常", q9.waiting == [] and q9.volume == 100)

print()
print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
sys.exit(0 if all(results) else 1)
