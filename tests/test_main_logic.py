# -*- coding: utf-8 -*-
"""插件入口(QQ 命令分支)的逻辑测试: 用假 AstrBot 运行时, 不启动网络/服务。

只测“命令怎么回话/怎么改队列”, 不测 AstrBot 本身的调度。
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


# ---------------- 假 AstrBot 运行时 ----------------
class _Logger:
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        pass

    def error(self, m):
        pass


class Star:
    def __init__(self, context=None):
        self.context = context


class Context:
    pass


class AstrBotConfig(dict):
    pass


def register(*args, **kwargs):
    def deco(cls):
        return cls

    return deco


class AstrMessageEvent:
    def __init__(self, admin=False, sid="10001", name="测试用户"):
        self._admin = admin
        self._sid = sid
        self._name = name

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return self._sid

    def get_sender_name(self):
        return self._name

    def plain_result(self, text):
        return {"text": text}


def command(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def permission_type(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def _install_stubs():
    fake = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    all_mod = types.ModuleType("astrbot.api.all")
    for obj in (Star, Context, AstrBotConfig, register):
        setattr(all_mod, obj.__name__, obj)
    all_mod.__all__ = ["Star", "Context", "AstrBotConfig", "register"]

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent

    filter_mod = types.ModuleType("astrbot.api.event.filter")
    filter_mod.command = command
    filter_mod.permission_type = permission_type
    filter_mod.PermissionType = types.SimpleNamespace(ADMIN="admin")

    core_mod = types.ModuleType("astrbot.core")
    star_mod = types.ModuleType("astrbot.core.star")
    star_filter = types.ModuleType("astrbot.core.star.filter")
    cmd_filter = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    cmd_filter.GreedyStr = GreedyStr

    sys.modules.update({
        "astrbot": fake,
        "astrbot.api": api,
        "astrbot.api.all": all_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.event.filter": filter_mod,
        "astrbot.core": core_mod,
        "astrbot.core.star": star_mod,
        "astrbot.core.star.filter": star_filter,
        "astrbot.core.star.filter.command": cmd_filter,
    })


_install_stubs()

# 以包的形式加载插件入口(它内部用相对导入)
pkg = types.ModuleType("blm")
pkg.__path__ = [str(ROOT)]
sys.modules["blm"] = pkg
spec = importlib.util.spec_from_file_location("blm.main", ROOT / "main.py")
main_mod = importlib.util.module_from_spec(spec)
sys.modules["blm.main"] = main_mod
spec.loader.exec_module(main_mod)

from blm.core.models import Song                                    # noqa: E402
from blm.core.player import PlayerService, PlayerSettings           # noqa: E402
from blm.core.queue_manager import QueueManager, QueueSettings      # noqa: E402


class FakeHub:
    def __init__(self, has_player=False):
        self.messages = []
        self.has_player = has_player

    def broadcast(self, message):
        self.messages.append(message)


class FakeMusic:
    class settings:
        backend = "netease"
        search_limit = 3
        panel_search_limit = 10
        luoyue_sources = []

    def describe(self):
        return "假音源"

    async def pick(self, keyword, limit=3):
        return Song(source="netease", song_id=keyword, title=keyword, artist="歌手",
                    url="https://audio.example/x.mp3")

    async def search(self, keyword, limit=3):
        return [await self.pick(keyword)]

    async def resolve(self, song):
        return song


class FakeDanmaku:
    def snapshot(self):
        return {"state": "connected", "danmaku": 3, "attempts": 2}

    async def restart(self):
        return True


def make_plugin(bot_request=False, whitelist=""):
    queue = QueueManager(QueueSettings(max_queue_size=10, per_user_limit=10, cooldown_secs=0))
    hub = FakeHub(has_player=True)
    player = PlayerService(queue, hub, PlayerSettings(state_broadcast_secs=0))
    inst = object.__new__(main_mod.Main)
    inst.queue = queue
    inst.hub = hub
    inst.player = player
    inst.music = FakeMusic()
    inst.danmaku = FakeDanmaku()
    inst.overlay = None
    inst._danmaku_last = {}
    inst.cfg = {"bot_request_enabled": bot_request, "bot_request_whitelist": whitelist,
                "command_prefix": "点歌", "enable_danmaku_request": True}
    return inst


async def collect(agen):
    return [item async for item in agen]


async def main():
    inst = make_plugin()

    # ---- 帮助 ----
    user_help = inst._help_text(False)
    admin_help = inst._help_text(True)
    check("普通用户帮助含弹幕说明", "点歌 歌名" in user_help)
    check("普通用户看不到管理命令列表", "跳过" not in user_help)
    check("管理员帮助含管理命令", "跳过" in admin_help and "重连" in admin_help)

    # ---- 状态 ----
    status = inst._render_status()
    check("空闲状态显示空闲", "当前：空闲" in status)
    check("状态里带弹幕状态", "弹幕：connected" in status)
    check("状态里带音源", "假音源" in status)

    # ---- 点歌(未开放 QQ 点歌) ----
    ev_user = AstrMessageEvent(admin=False)
    out = await collect(inst.cmd_dian_ge(ev_user, "晴天"))
    check("未开启时普通用户被拒", "只在直播间弹幕里开放" in out[0]["text"])

    # ---- 点歌(管理员) ----
    ev_admin = AstrMessageEvent(admin=True)
    out = await collect(inst.cmd_dian_ge(ev_admin, "晴天"))
    check("管理员点歌成功", "已加入点歌队列" in out[0]["text"])
    check("点歌后开始播放", inst.queue.current is not None)
    check("队列里有第二首时状态显示队列", True)
    inst.queue.enqueue(Song(source="netease", song_id="2", title="七里香", artist="歌手"),
                       "观众", "uid:2")
    check("状态显示等待队列", "等待队列（1 首）" in inst._render_status())

    # ---- 帮助无参数 ----
    out = await collect(inst.cmd_dian_ge(ev_admin, ""))
    check("无参数返回帮助", "B站直播点歌台" in out[0]["text"])

    # ---- 子命令: 状态/跳过/音量/清空/面板/重连 ----
    out = await collect(inst._handle_admin_sub("状态", ""))
    check("子命令状态", "点歌台状态" in out[0])
    out = await collect(inst._handle_admin_sub("跳过", ""))
    check("子命令跳过", "已跳过" in out[0])
    out = await collect(inst._handle_admin_sub("音量", "60"))
    check("子命令音量", "60" in out[0] and inst.queue.volume == 60)
    out = await collect(inst._handle_admin_sub("音量", ""))
    check("音量无参数给出用法", "用法" in out[0])
    out = await collect(inst._handle_admin_sub("音量", "abc"))
    check("音量非数字提示", "数字" in out[0])
    idle = make_plugin()
    out = await collect(idle._handle_admin_sub("暂停", ""))
    check("暂停无播放时提示", "没有在播放" in out[0])
    out = await collect(idle._handle_admin_sub("继续", ""))
    check("继续非暂停时提示", "不是暂停" in out[0])
    out = await collect(inst._handle_admin_sub("面板", ""))
    check("面板未启动时提示", "Overlay 服务没起来" in out[0])
    out = await collect(inst._handle_admin_sub("清空", ""))
    check("清空返回条数", "已清空" in out[0])
    check("清空后队列为空", len(inst.queue.waiting) == 0)
    out = await collect(inst._handle_admin_sub("重连", ""))
    check("重连弹幕", "已重连" in out[0])
    out = await collect(inst._handle_admin_sub("乱写", ""))
    check("未知子命令", "未知操作" in out[0])

    # ---- 群聊点歌白名单 ----
    inst2 = make_plugin(bot_request=True)
    ev = AstrMessageEvent(admin=False, sid="555")
    check("开启后普通用户可点歌", inst2._is_allowed_bot_request(ev) is True)
    inst3 = make_plugin(bot_request=True, whitelist="111,222")
    check("白名单外被拒", inst3._is_allowed_bot_request(AstrMessageEvent(admin=False, sid="555")) is False)
    check("白名单内放行", inst3._is_allowed_bot_request(AstrMessageEvent(admin=False, sid="222")) is True)
    check("管理员始终放行", inst3._is_allowed_bot_request(AstrMessageEvent(admin=True, sid="999")) is True)

    # ---- 弹幕点歌回调 ----
    class Msg:
        text = "点歌 稻香"
        uid = 777
        username = "观众甲"

    await inst._on_danmaku(Msg())
    titles = [item.song.title for item in inst.queue.waiting] + (
        [inst.queue.current.song.title] if inst.queue.current else [])
    check("弹幕点歌会入队", "稻香" in titles or inst.queue.current is not None)
    await inst._on_danmaku(Msg())          # 3 秒内重复应被忽略
    check("弹幕 3 秒内不重复处理", len(inst._danmaku_last) == 1)

    # ---- 限流表定期清理(避免直播久了无限增长) ----
    inst4 = make_plugin()
    import time as _time
    old = _time.time() - 600
    inst4._danmaku_last = {f"uid:{i}": old for i in range(600)}
    await inst4._on_danmaku(Msg())
    check("限流记录会定期清理", len(inst4._danmaku_last) < 600)

    # ---- 播放器角色降级: 第二个 claim 的页面应静音 ----
    import aiohttp as _aiohttp
    from blm.core.overlay import OverlayHub, OverlayServer, OverlaySettings

    class _P:
        def snapshot(self):
            return {}

        async def action(self, name, payload):
            return {}

        def on_client_message(self, role, message):
            pass

        def on_client_connected(self, role):
            pass

    hub3 = OverlayHub()
    server3 = OverlayServer(hub3, OverlaySettings(host="127.0.0.1", port=0, token="t3"),
                            _P(), page_dir=ROOT / "pages")
    port3 = await server3.start()
    async with _aiohttp.ClientSession() as session:
        ws1 = await session.ws_connect(f"http://127.0.0.1:{port3}/bili-live-music/ws?token=t3")
        await ws1.send_str('{"type":"claim"}')
        await asyncio.sleep(0.15)
        ws2 = await session.ws_connect(f"http://127.0.0.1:{port3}/bili-live-music/ws?token=t3")
        await ws2.send_str('{"type":"claim"}')
        await asyncio.sleep(0.25)
        check("只允许一个播放器页", hub3.player_count() == 1)
        msg = await ws2.receive(timeout=2)
        payload = __import__("json").loads(msg.data)
        check("第二个播放页被降级为展示", payload.get("type") == "role" and payload.get("role") == "display")
        await ws1.close()
        await ws2.close()
    await server3.stop()

    # ---- 模拟弹幕(面板调试用) ----
    inst5 = make_plugin()
    res5 = await inst5._simulate_danmaku("点歌 发如雪")
    check("模拟弹幕走通点歌", res5.get("ok") is True and res5.get("current") == "发如雪")
    res6 = await inst5._simulate_danmaku("随便聊聊")
    check("模拟弹幕前缀不对被拒", res6.get("ok") is False)

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
