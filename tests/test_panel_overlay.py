# -*- coding: utf-8 -*-
"""控制面板逻辑 + Overlay 服务(真实 aiohttp 服务器, 本机回环) 单测。"""

import asyncio
import json
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
from core.overlay import OverlayHub, OverlayServer, OverlaySettings  # noqa: E402
from core.panel import PanelService                                 # noqa: E402
from core.player import PlayerService, PlayerSettings                # noqa: E402
from core.queue_manager import QueueManager, QueueSettings           # noqa: E402

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


class FakeHub:
    def __init__(self, has_player=True):
        self.messages = []
        self.has_player = has_player

    def broadcast(self, message):
        self.messages.append(message)

    def of(self, kind):
        return [m for m in self.messages if m.get("type") == kind]


class FakeMusic:
    class settings:
        backend = "netease"
        luoyue_sources = ["netease"]
        panel_search_limit = 5

    def describe(self):
        return "fake 音源"

    async def search(self, keyword, limit=5):
        return [Song(source="netease", song_id=keyword, title=keyword, artist="歌手")]

    async def resolve(self, song):
        from dataclasses import replace
        return replace(song, url="https://audio.example/1.mp3")

    async def pick(self, keyword, limit=3):
        song = (await self.search(keyword, limit))[0]
        return await self.resolve(song)


class FakeDanmaku:
    def __init__(self):
        self.restarted = 0

    def snapshot(self):
        return {"state": "connected", "danmaku": 7, "attempts": 1}

    async def restart(self):
        self.restarted += 1
        return True


def build(has_player=True):
    queue = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0))
    hub = FakeHub(has_player=has_player)
    player = PlayerService(queue, hub, PlayerSettings(state_broadcast_secs=0))
    danmaku = FakeDanmaku()
    panel = PanelService(queue, player, FakeMusic(), danmaku,
                         urls_provider=lambda: {"player": "http://x/overlay?token=t", "panel": "http://x/"})
    return queue, hub, player, panel, danmaku


async def test_panel():
    queue, hub, player, panel, danmaku = build()

    # snapshot
    snap = panel.snapshot()
    check("快照含 player/queue/history/danmaku/music/urls",
          {"player", "queue", "history", "danmaku", "music", "urls"} <= set(snap))
    check("快照带音源描述", snap["music"]["describe"] == "fake 音源")
    check("快照带 Overlay 地址", snap["urls"]["player"].startswith("http://x/"))

    # 搜索
    res = await panel.action("search", {"keyword": "晴天"})
    check("面板搜索返回结果", res.get("items") and res["items"][0]["title"] == "晴天")

    # 添加(自动解析直链) + 空闲时开播
    added = await panel.action("add", {"song": res["items"][0]})
    check("面板添加成功", added.get("item") is not None and added.get("position") == 1)
    check("添加后开始播放", queue.current is not None)
    check("广播了 play", len(hub.of("play")) == 1)
    check("播放地址已解析", queue.current.song.url.endswith(".mp3"))

    # 再加两首
    await panel.action("add", {"keyword": "七里香"})
    await panel.action("add", {"keyword": "稻香"})
    check("队列长度 2(一首在播)", len(queue.waiting) == 2)
    check("重复添加同一首被去重拒绝",
          (await panel.action("add", {"song": {"source": "netease", "song_id": "晴天", "title": "晴天", "artist": "歌手"}})).get("ok") is False)

    # 排序
    first_title = queue.waiting[0].song.title
    moved = await panel.action("down", {"index": 0})
    check("下移生效", moved.get("moved") is True and queue.waiting[1].song.title == first_title)
    moved = await panel.action("up", {"index": 1})
    check("上移生效", moved.get("moved") is True and queue.waiting[0].song.title == first_title)

    # 删除
    removed = await panel.action("remove", {"index": 0})
    check("删除返回歌名", isinstance(removed.get("removed"), str) and len(queue.waiting) == 1)

    # 暂停 / 继续 / 下一首 / 停止
    player.on_client_message("player", {"type": "playing"})
    check("暂停", (await panel.action("pause", {})).get("changed") is True)
    check("继续", (await panel.action("resume", {})).get("changed") is True)
    await panel.action("next", {})
    check("下一首记录 skipped", queue.history[-1].result == "skipped")
    await panel.action("stop", {})
    check("停止后无当前歌", queue.current is None)

    # 音量 / 清空 / 重连 / 未知操作
    check("音量设置", (await panel.action("volume", {"value": 37})).get("volume") == 37)
    queue.enqueue(Song(source="netease", song_id="9", title="清空用", artist="x"), "u", "k")
    check("清空返回条数", (await panel.action("clear", {})).get("removed") == 1)
    check("重连弹幕调用 restart", (await panel.action("danmaku_restart", {})).get("ok") is True and danmaku.restarted == 1)
    unknown = await panel.action("nope", {})
    check("未知操作返回错误", unknown.get("ok") is False and "未知操作" in unknown.get("error", ""))

    # 客户端事件透传
    queue.enqueue(Song(source="netease", song_id="10", title="透传", artist="x"), "u", "k2")
    await player.tick()
    player.on_client_message("player", {"type": "ended"})
    check("ended 透传到播放器", queue.history and queue.history[-1].result == "done")
    check("快照里带播放历史", len(panel.snapshot()["history"]) >= 1)

    # 模拟弹幕
    simulated = []

    async def fake_sim(text, username):
        simulated.append((text, username))
        return {"ok": True, "detail": "已加入", "queued": 1, "current": "晴天"}

    _, _, _, panel5, _ = build()
    panel5.simulate = fake_sim
    r = await panel5.action("simulate_danmaku", {"text": "点歌 晴天"})
    check("模拟弹幕调用回调", simulated == [("点歌 晴天", "测试观众")] and r.get("ok") is True)
    r2 = await panel5.action("simulate_danmaku", {"text": "   "})
    check("模拟弹幕空文本报错", r2.get("ok") is False)
    panel5.simulate = None
    r3 = await panel5.action("simulate_danmaku", {"text": "点歌 x"})
    check("未提供模拟回调时报错", r3.get("ok") is False)

    # 没有播放器页时不自动开播
    queue2, hub2, player2, panel2, _ = build(has_player=False)
    await panel2.action("add", {"keyword": "等待"})
    check("无播放器页时等待", queue2.current is None and player2.status == "waiting_client")


class DummyPanel:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"player": {"status": "idle"}, "queue": [], "ok": True}

    async def action(self, name, payload):
        self.calls.append((name, payload))
        if name == "boom":
            raise RuntimeError("炸了")
        return {"echo": name, "payload": payload}

    def on_client_message(self, role, message):
        self.calls.append(("msg", (role, message)))

    def on_client_connected(self, role):
        self.calls.append(("connected", role))


async def test_overlay_server():
    import aiohttp

    # 页面静态检查: 占位符与关键逻辑都在, 且没有误写入的字面转义
    overlay_html = (ROOT / "pages" / "overlay.html").read_text(encoding="utf-8")
    panel_html = (ROOT / "pages" / "panel.html").read_text(encoding="utf-8")
    check("overlay 有字体占位符", "__FONT_URL__" in overlay_html)
    check("overlay 有 WS 路径占位符", "__WS_PATH__" in overlay_html)
    check("overlay 换源时忽略旧 error", "expectedSrc" in overlay_html and "currentSrc" in overlay_html)
    check("overlay 事件带序号", "currentSeq" in overlay_html and "message.seq = currentSeq" in overlay_html)
    check("overlay 没有字面转义残留", "`n" not in overlay_html)
    check("panel 有路由前缀占位符", "__BASE_PATH__" in panel_html)
    check("panel 有模拟弹幕入口", "simulate_danmaku" in panel_html)
    check("panel 有播放历史区域", "historyBody" in panel_html)

    hub = OverlayHub()
    panel = DummyPanel()
    settings = OverlaySettings(host="127.0.0.1", port=0, public_host="127.0.0.1", token="tok123")
    server = OverlayServer(hub, settings, panel, page_dir=ROOT / "pages")
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    check("服务绑定到随机端口", port > 0)

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/bili-live-music/api/health") as resp:
            body = await resp.json()
        check("健康检查免 token", resp.status == 200 and body["ok"] is True)

        async with session.get(f"{base}/bili-live-music/overlay?mode=player&layout=mini") as resp:
            text = await resp.text()
        check("无 token 访问 Overlay 返回 401", resp.status == 401)

        async with session.get(
            f"{base}/bili-live-music/overlay?token=tok123&mode=player&layout=sidebar"
        ) as resp:
            html = await resp.text()
        check("带 token 访问 Overlay 返回 200", resp.status == 200)
        check("Overlay 注入了布局", 'data-layout="sidebar"' in html)
        check("Overlay 注入了 token", "tok123" in html)
        check("Overlay 字体地址已替换", "__FONT_URL__" not in html and "/font.ttf" in html)
        check("Overlay 注入了 WS 路径", "/bili-live-music/ws" in html)

        async with session.get(f"{base}/bili-live-music/?token=tok123") as resp:
            page = await resp.text()
        check("面板页可访问", resp.status == 200 and "B站直播点歌台" in page)
        check("面板页注入了路由前缀", "bili-live-music/api/action" in page and "__BASE_PATH__" not in page)

        async with session.get(f"{base}/bili-live-music/api/state?token=tok123") as resp:
            state = await resp.json()
        check("状态接口返回快照", state.get("ok") is True and "player" in state)

        async with session.post(
            f"{base}/bili-live-music/api/action/next?token=tok123", json={"a": 1}
        ) as resp:
            data = await resp.json()
        check("操作接口转发到面板", data.get("ok") is True and data["result"]["echo"] == "next")
        check("操作参数被透传", panel.calls[-1] == ("next", {"a": 1}))

        async with session.post(
            f"{base}/bili-live-music/api/action/boom?token=tok123", json={}
        ) as resp:
            data = await resp.json()
        check("面板异常返回 400 且不崩", resp.status == 400 and data.get("ok") is False)

        # WebSocket: claim -> player 角色; 广播能收到
        async with session.ws_connect(f"{base}/bili-live-music/ws?token=tok123") as ws:
            await ws.send_str(json.dumps({"type": "claim"}))
            await asyncio.sleep(0.15)
            check("claim 后 hub 认为是播放器", hub.has_player is True)
            hub.broadcast({"type": "state", "state": {"status": "playing"}})
            msg = await ws.receive(timeout=2)
            payload = json.loads(msg.data)
            check("WS 收到广播", payload["type"] == "state")
            await ws.send_str(json.dumps({"type": "ended", "itemId": "x"}))
            await asyncio.sleep(0.1)
            check("WS 消息转发给面板", ("msg", ("player", {"type": "ended", "itemId": "x"})) in panel.calls)

    await server.stop()
    check("服务停止后端口释放", True)

    # 自定义路由前缀
    hub2 = OverlayHub()
    server2 = OverlayServer(hub2, OverlaySettings(host="127.0.0.1", port=0, public_host="127.0.0.1",
                                                  token="tok2", base_path="/music"),
                            DummyPanel(), page_dir=ROOT / "pages")
    port2 = await server2.start()
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port2}/music/?token=tok2") as resp:
            page2 = await resp.text()
        check("自定义前缀: 面板可访问", resp.status == 200)
        check("自定义前缀: 面板里路径也跟着换", "/music/api/state" in page2 and "__BASE_PATH__" not in page2)
    await server2.stop()


async def main():
    await test_panel()
    await test_overlay_server()
    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
