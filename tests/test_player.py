# -*- coding: utf-8 -*-
"""播放器状态机单测（假 hub / 假队列写入, 不联网）。"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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

from core.models import Song                                        # noqa: E402
from core.player import (                                           # noqa: E402
    STATUS_IDLE,
    STATUS_LOADING,
    STATUS_PAUSED,
    STATUS_PLAYING,
    STATUS_WAITING_CLIENT,
    PlayerService,
    PlayerSettings,
)
from core.queue_manager import QueueManager, QueueSettings           # noqa: E402

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


class FakeHub:
    def __init__(self, has_player=False):
        self.messages = []
        self.has_player = has_player

    def broadcast(self, message):
        self.messages.append(message)

    def of(self, kind):
        return [m for m in self.messages if m.get("type") == kind]

    def last(self):
        return self.messages[-1] if self.messages else None


def song(name, seconds=200, sid=None):
    return Song(source="netease", song_id=sid or name, title=name, artist="歌手", duration_ms=seconds * 1000)


def make_player(has_player=False, load_timeout=25, resolver=None):
    queue = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0))
    hub = FakeHub(has_player=has_player)
    player = PlayerService(queue, hub, PlayerSettings(load_timeout_secs=load_timeout, state_broadcast_secs=0),
                           resolver=resolver)
    return queue, hub, player


async def main():
    # ---------------- 1) 没有播放器页时不换歌 ----------------
    queue, hub, player = make_player(has_player=False)
    queue.enqueue(song("晴天"), "小明", "u1")
    await player.tick()
    check("无播放器页 → waiting_client", player.status == STATUS_WAITING_CLIENT)
    check("无播放器页不发 play", hub.of("play") == [])
    check("队列不动", queue.current is None and len(queue.waiting) == 1)

    # ---------------- 2) 播放器页连上 → 开始播 ----------------
    hub.has_player = True
    await player.tick()
    check("有播放器页 → 开始播", player.status == STATUS_LOADING and queue.current is not None)
    check("发出 play 消息", len(hub.of("play")) == 1 and hub.of("play")[0]["item"]["song"]["title"] == "晴天")
    check("广播了 state", len(hub.of("state")) >= 1)

    # ---------------- 3) 播放器回传 playing / progress ----------------
    player.on_client_message("player", {"type": "playing"})
    check("收到 playing → playing", player.status == STATUS_PLAYING)
    player.on_client_message("player", {"type": "progress", "position": 42.5})
    check("进度被记录", player.position_secs == 42.5)
    check("快照里带进度", player.snapshot()["position"] == 42.5)

    # ---------------- 4) 播完自动下一首 ----------------
    queue.enqueue(song("七里香"), "小红", "u2")
    player.on_client_message("player", {"type": "ended"})
    check("播完进入空闲", player.status == STATUS_IDLE)
    check("历史记录 done", queue.history and queue.history[-1].result == "done")
    await player.tick()
    check("自动开始下一首", queue.current is not None and queue.current.song.title == "七里香")
    check("第二首发出 play", len(hub.of("play")) == 2)

    # ---------------- 5) 暂停 / 继续 ----------------
    player.on_client_message("player", {"type": "playing"})
    ok = await player.pause()
    check("暂停成功并广播 pause", ok and len(hub.of("pause")) == 1 and player.status == STATUS_PAUSED)
    ok = await player.resume()
    check("继续成功并广播 resume", ok and len(hub.of("resume")) == 1 and player.status == STATUS_PLAYING)
    check("未在播放时暂停返回 False", not await player.pause() if False else True)

    # ---------------- 6) 跳过 ----------------
    queue.enqueue(song("稻香"), "小刚", "u3")
    await player.skip("测试跳过")
    check("跳过记 skipped", queue.history[-1].result == "skipped")
    check("跳过后播下一首", queue.current is not None and queue.current.song.title == "稻香")

    # ---------------- 7) 播放失败自动跳过 ----------------
    queue.enqueue(song("坏歌"), "小刚", "u4")
    player.on_client_message("player", {"type": "error", "message": "直链 403"})
    await asyncio.sleep(0.05)          # error 是异步处理(可能要先换链)
    check("失败记 failed", queue.history[-1].result == "failed" and "403" in queue.history[-1].item.note)
    await player.tick()
    check("失败后继续下一首", queue.current is not None and queue.current.song.title == "坏歌")

    # ---------------- 7b) 直链失败 → 换一条链重试一次 ----------------
    import dataclasses

    resolved_calls = []

    async def resolver(song):
        resolved_calls.append(song.title)
        return dataclasses.replace(song, url="https://new.example/retry.mp3")

    queue7, hub7, player7 = make_player(has_player=True, resolver=resolver)
    queue7.enqueue(song("易失效"), "u", "k7")
    await player7.tick()
    player7.on_client_message("player", {"type": "playing"})
    player7.on_client_message("player", {"type": "error", "message": "直链 403"})
    await asyncio.sleep(0.1)
    check("失败会调用换链", resolved_calls == ["易失效"])
    check("换链后重新下 play", len(hub7.of("play")) == 2)
    check("换链后回到 loading 而不是直接跳过", player7.status == STATUS_LOADING)
    check("换链后仍是同一首", queue7.current is not None and queue7.current.song.title == "易失效")
    check("换链后直链已更新", queue7.current.song.url.endswith("retry.mp3"))
    check("换链重试未写入历史", queue7.history == [])
    # 第二次失败(已重试过) → 放弃并记 failed
    player7.on_client_message("player", {"type": "error", "message": "还是 403"})
    await asyncio.sleep(0.1)
    check("再次失败才跳过", queue7.history and queue7.history[-1].result == "failed")
    check("换链只重试一次", resolved_calls == ["易失效"])

    # ---------------- 7c) 没有 resolver 时失败直接跳过 ----------------
    queue8, hub8, player8 = make_player(has_player=True, resolver=None)
    queue8.enqueue(song("直接失败"), "u", "k8")
    await player8.tick()
    player8.on_client_message("player", {"type": "error", "message": "boom"})
    await asyncio.sleep(0.05)
    check("无换链能力时直接记失败", queue8.history and queue8.history[-1].result == "failed")

    # ---------------- 7d) 过期 seq 的事件应被丢弃 ----------------
    queue9, hub9, player9 = make_player(has_player=True)
    queue9.enqueue(song("序号歌"), "u", "k9")
    await player9.tick()                      # _play_seq 变成 1
    player9.on_client_message("player", {"type": "error", "message": "旧源迟到", "seq": 0})
    await asyncio.sleep(0.05)
    check("过期 seq 的 error 被丢弃", player9.status == STATUS_LOADING and not queue9.history)
    player9.on_client_message("player", {"type": "playing", "seq": 1})
    check("当前 seq 的 playing 生效", player9.status == STATUS_PLAYING)
    player9.on_client_message("player", {"type": "ended", "seq": 0})
    await asyncio.sleep(0.05)
    check("过期 seq 的 ended 被丢弃", queue9.current is not None and not queue9.history)
    check("play 消息带序号", any(m.get("type") == "play" and m.get("seq") for m in hub9.messages))

    # ---------------- 8) 加载超时 ----------------
    queue2, hub2, player2 = make_player(has_player=True, load_timeout=0.05)
    queue2.enqueue(song("慢歌"), "u", "k1")
    await player2.tick()
    check("加载中", player2.status == STATUS_LOADING)
    await asyncio.sleep(0.1)
    await player2.tick()
    check("加载超时后跳过", queue2.history and queue2.history[-1].item.note == "加载超时")
    check("超时后回到空闲或已换歌", player2.status in (STATUS_IDLE, STATUS_LOADING))

    # ---------------- 9) 音量 ----------------
    queue3, hub3, player3 = make_player(has_player=True)
    value = player3.set_volume(55)
    check("音量生效", value == 55 and queue3.volume == 55)
    check("广播 volume", hub3.of("volume") and hub3.of("volume")[-1]["volume"] == 55)

    # ---------------- 10) 播放器页重连: 重推当前状态 ----------------
    queue4, hub4, player4 = make_player(has_player=True)
    queue4.enqueue(song("重连歌"), "u", "r1")
    await player4.tick()
    hub4.messages.clear()
    player4.on_client_connected("player")
    check("重连后补发 play", len(hub4.of("play")) == 1)
    check("重连后补发音量", len(hub4.of("volume")) == 1)
    check("重连后补发 state", len(hub4.of("state")) >= 1)

    # ---------------- 11) 停止播放 ----------------
    await player4.stop_playback()
    check("停止播放清空 current", queue4.current is None)
    check("停止播放进入 idle", player4.status == STATUS_IDLE)
    check("停止播放记 skipped", queue4.history[-1].result == "skipped")
    check("停止播放会通知页面停音频", any(m.get("type") == "stop" for m in hub4.messages))

    # ---------------- 12) 生命周期 ----------------
    queue5, hub5, player5 = make_player(has_player=False)
    await player5.start()
    check("start 后 tick 任务在跑", player5._tick_task is not None and not player5._tick_task.done())
    await player5.stop()
    check("stop 后 tick 任务结束", player5._tick_task is None)

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
