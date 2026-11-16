# -*- coding: utf-8 -*-
"""弹幕解析 + 连接监管（全部用假客户端, 不联网）。"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- astrbot.api.logger stub ----
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

# ---- 假 bilibili_api（Credential） ----
bili = types.ModuleType("bilibili_api")


class FakeCredential:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


bili.Credential = FakeCredential
sys.modules["bilibili_api"] = bili

from core.danmaku import (                             # noqa: E402
    STATE_CONNECTED,
    STATE_DISABLED,
    STATE_STOPPED,
    STATE_WAITING,
    DanmakuMessage,
    DanmakuService,
    credential_from_cookie,
    parse_danmaku_callback,
)

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


def danmu_payload(text="点歌 晴天", uid=12345, name="观众A", room=1234):
    return {
        "type": "DANMU_MSG",
        "data": {
            "cmd": "DANMU_MSG",
            "room_id": room,
            "info": [
                [0, 1, 25, 16777215, 1700000000000, 0, 0, "", 0, 0, 0],
                text,
                [uid, name, 1, 0, 0, 10000, 1, ""],
                [15, "粉丝牌", "主播", 1, 99999, 0, 1, 1],
            ],
        },
    }


class FakeClient:
    STATUS_ESTABLISHED = "established"

    def __init__(self, connect_error=None, hold=0.05):
        self.handlers = {}
        self.connect_error = connect_error
        self.hold = hold
        self.disconnected = False
        self.status = "closed"

    def add_event_listener(self, name, handler):
        self.handlers.setdefault(name.upper(), []).append(handler)

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.status = "established"
        for handler in self.handlers.get("VERIFICATION_SUCCESSFUL", []):
            await handler({"type": "VERIFICATION_SUCCESSFUL"})
        await asyncio.sleep(self.hold)

    async def disconnect(self):
        self.disconnected = True
        self.status = "closed"

    def get_status(self):
        return self.status

    async def fire_danmaku(self, payload):
        for handler in self.handlers.get("DANMU_MSG", []):
            await handler(payload)


async def main():
    # ---------------- 1) 弹幕解析 ----------------
    msg = parse_danmaku_callback(danmu_payload())
    check("解析出弹幕对象", isinstance(msg, DanmakuMessage))
    check("正文正确", msg.text == "点歌 晴天")
    check("uid/昵称正确", msg.uid == 12345 and msg.username == "观众A")
    check("房号正确", msg.room_id == 1234)
    check("空正文返回 None", parse_danmaku_callback(danmu_payload(text="  ")) is None)
    check("非 dict 返回 None", parse_danmaku_callback(None) is None)
    check("缺 info 返回 None", parse_danmaku_callback({"data": {}}) is None)
    no_uid = danmu_payload(uid=0, name="")
    no_uid["data"]["info"][2] = []
    m2 = parse_danmaku_callback(no_uid)
    check("无用户信息兜底", m2 is not None and m2.uid == 0 and m2.username.startswith("uid"))

    # ---------------- 2) Cookie -> Credential ----------------
    cred = credential_from_cookie("SESSDATA=abc%2C123; bili_jct=jct; DedeUserID=98765; buvid3=bv")
    check("解析出 Credential", cred is not None)
    check("SESSDATA 映射", cred.kwargs.get("sessdata") == "abc%2C123")
    check("DedeUserID 映射", cred.kwargs.get("dedeuserid") == "98765")
    check("空 Cookie 返回 None", credential_from_cookie("") is None)

    # ---------------- 3) 正常连接 + 弹幕回调 ----------------
    received = []
    clients = []

    def factory(room_id, credential):
        client = FakeClient(hold=0.3)
        clients.append(client)
        return client

    service = DanmakuService(room_id=1234, client_factory=factory,
                             on_message=lambda m: received.append(m))
    check("start 返回 True", await service.start() is True)
    await asyncio.sleep(0.12)
    check("状态变为 connected", service.state == STATE_CONNECTED)
    check("connected_at 有值", service.connected_at > 0)
    check("只建了一个客户端", len(clients) == 1)

    await clients[0].fire_danmaku(danmu_payload("点歌 七里香"))
    check("弹幕回调被触发", len(received) == 1 and received[0].text == "点歌 七里香")
    check("统计到弹幕数", service.snapshot()["danmaku"] == 1)

    # 旧 generation 的回调应被丢弃
    service.generation += 1
    await clients[0].fire_danmaku(danmu_payload("点歌 过期消息"))
    check("旧连接回调被忽略", len(received) == 1)

    await service.stop()
    check("stop 后状态为 stopped", service.state == STATE_STOPPED)
    check("stop 时断开了客户端", clients[0].disconnected is True)
    check("stop 后 running 为 False", service.running is False)

    # ---------------- 4) 连接失败 -> 退避重试 ----------------
    attempts = []
    fail_then_ok = {"count": 0}

    def flaky_factory(room_id, credential):
        attempts.append(room_id)
        fail_then_ok["count"] += 1
        if fail_then_ok["count"] <= 2:
            return FakeClient(connect_error=RuntimeError("连接被重置"))
        return FakeClient(hold=0.3)

    flaky = DanmakuService(room_id=777, client_factory=flaky_factory,
                           retry_delays=[0.02, 0.02], connect_timeout=5)
    await flaky.start()
    await asyncio.sleep(0.35)
    check("失败后会重试", len(attempts) >= 2)
    check("重试后连上", flaky.state == STATE_CONNECTED)
    check("记录了最近错误", "连接被重置" in "".join(t["detail"] for t in flaky.transitions))
    await flaky.stop()

    # ---------------- 5) 未配置房号 ----------------
    disabled = DanmakuService(room_id=0)
    check("房号为空时 start 返回 False", await disabled.start() is False)
    check("房号为空状态为 disabled", disabled.state == STATE_DISABLED)

    # ---------------- 6) 握手成功后长连接不能被超时误杀 ----------------
    long_client = FakeClient(hold=0.4)
    long_lived = DanmakuService(room_id=999, client_factory=lambda *a: long_client,
                                connect_timeout=0.2, retry_delays=[0.05])
    await long_lived.start()
    await asyncio.sleep(0.3)
    check("长连接超过 connect_timeout 仍保持", long_lived.state == STATE_CONNECTED)
    check("长连接没有被反复重连", long_lived.attempts == 1)
    await long_lived.stop()

    # ---------------- 7) 握手一直不成功 -> 超时重试 ----------------
    class SilentClient(FakeClient):
        async def connect(self):
            await asyncio.sleep(5)          # 永远不发 VERIFICATION_SUCCESSFUL

    silent_attempts = []

    def silent_factory(room_id, credential):
        silent_attempts.append(room_id)
        return SilentClient()

    silent = DanmakuService(room_id=998, client_factory=silent_factory,
                            connect_timeout=0.2, retry_delays=[0.05])
    await silent.start()
    await asyncio.sleep(0.8)
    check("握手超时会重试", len(silent_attempts) >= 2)
    check("超时原因被记录", "超时" in (silent.last_error or "") or "超时" in "".join(
        t["detail"] for t in silent.transitions))
    await silent.stop()

    # ---------------- 8) 快照 ----------------
    snap = service.snapshot()
    check("快照字段齐全", {"room_id", "state", "running", "attempts", "transitions"} <= set(snap))
    check("快照房号正确", snap["room_id"] == 1234)

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
