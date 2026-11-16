# -*- coding: utf-8 -*-
"""aiohttp 版弹幕客户端单测: 包编解码/压缩/指令归一/认证与收包流程(全假对象)。"""

import asyncio
import json
import sys
import types
import zlib
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

from core.bili_ws import (                                      # noqa: E402
    OP_AUTH,
    OP_AUTH_REPLY,
    OP_MESSAGE,
    PROTO_BROTLI,
    PROTO_ZLIB,
    AiohttpLiveDanmaku,
    build_packet,
    decode_messages,
    normalize_cmd,
    parse_packet,
)

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


class FakeType:
    def __init__(self, name):
        self.name = name


class FakeMsg:
    def __init__(self, kind, data=b""):
        self.type = FakeType(kind)
        self.data = data


class FakeWS:
    closed = False

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.sent = []
        self.closed = False

    async def send_bytes(self, data):
        self.sent.append(data)

    async def receive(self):
        if not self._msgs:
            await asyncio.sleep(60)
        return self._msgs.pop(0)

    async def close(self):
        self.closed = True


def danmu_payload(text="点歌 晴天", uid=123, name="观众"):
    return {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [[0, 1, 25], text, [uid, name, 0]],
    }


async def main():
    # ---------------- 包编解码 ----------------
    packet = build_packet(OP_MESSAGE, b'{"cmd":"X"}', PROTO_ZLIB)
    op, proto, body = parse_packet(packet)
    check("包操作码正确", op == OP_MESSAGE)
    check("包协议版本正确", proto == PROTO_ZLIB)
    check("包体正确", body == b'{"cmd":"X"}')
    try:
        parse_packet(b"\x00\x01")
        check("短包抛异常", False)
    except ValueError:
        check("短包抛异常", True)

    # ---------------- 解压 / JSON ----------------
    raw = json.dumps(danmu_payload(), ensure_ascii=False).encode("utf-8")
    msgs = decode_messages(PROTO_ZLIB, zlib.compress(raw))
    check("zlib 解压出消息", len(msgs) == 1 and msgs[0]["cmd"].startswith("DANMU_MSG"))
    check("明文 JSON 也能解", decode_messages(0, raw)[0]["info"][1] == "点歌 晴天")

    two = json.dumps({"cmd": "A"}).encode() + json.dumps({"cmd": "B"}).encode()
    check("拼接的多个 JSON 都能解", [m["cmd"] for m in decode_messages(0, two)] == ["A", "B"])
    check("空负载返回空列表", decode_messages(0, b"") == [])
    check("坏负载不抛异常", decode_messages(PROTO_ZLIB, b"not-zlib") == [])

    # B 站压缩帧解压后仍是"包头 + JSON"序列, 必须再切一次包
    inner_one = build_packet(OP_MESSAGE, json.dumps(danmu_payload(), ensure_ascii=False).encode())
    nested_one = decode_messages(PROTO_ZLIB, zlib.compress(inner_one))
    check("解压后的内层包头能解析", len(nested_one) == 1 and nested_one[0]["info"][1] == "点歌 晴天")

    inner_two = (build_packet(OP_MESSAGE, json.dumps({"cmd": "A"}).encode())
                 + build_packet(OP_MESSAGE, json.dumps({"cmd": "B"}).encode()))
    nested_two = decode_messages(PROTO_ZLIB, zlib.compress(inner_two))
    check("解压后的多个内层包都能解析", [m["cmd"] for m in nested_two] == ["A", "B"])

    try:
        import brotli  # type: ignore
        brotli_ok = True
    except Exception:
        brotli_ok = False
    if brotli_ok:
        msgs_b = decode_messages(PROTO_BROTLI, brotli.compress(raw))
        check("brotli 解压出消息", len(msgs_b) == 1)
    else:
        check("brotli 未安装时返回空(不崩)", decode_messages(PROTO_BROTLI, b"xx") == [])

    # ---------------- 指令归一 ----------------
    check("DANMU_MSG 去后缀", normalize_cmd("DANMU_MSG:4:0:2:2:2:0") == "DANMU_MSG")
    check("普通指令原样", normalize_cmd("SEND_GIFT") == "SEND_GIFT")
    check("空指令返回空", normalize_cmd(None) == "")

    # ---------------- 认证流程 ----------------
    client = AiohttpLiveDanmaku(room_display_id=1)
    ws = FakeWS([FakeMsg("BINARY", build_packet(OP_AUTH_REPLY, b""))])
    await client._authenticate(ws, real_room=999, token="tok")
    check("认证发送了 op=7 包", ws.sent and parse_packet(ws.sent[0])[0] == OP_AUTH)
    auth = json.loads(parse_packet(ws.sent[0])[2])
    check("认证包含房间号与 token", auth["roomid"] == 999 and auth["key"] == "tok")
    check("认证 protover 是压缩协议", auth["protover"] in (2, 3))

    # 认证超时
    slow = FakeWS([])
    client.connect_timeout = 0.3
    try:
        await client._authenticate(slow, real_room=1, token="t")
        check("认证超时会抛异常", False)
    except (asyncio.TimeoutError, TimeoutError):
        check("认证超时会抛异常", True)

    # ---------------- 收包 -> 派发 ----------------
    got = []

    @client.on("DANMU_MSG")
    async def handler(event):
        got.append(event)

    body = zlib.compress(json.dumps(danmu_payload("点歌 稻香"), ensure_ascii=False).encode())
    ws2 = FakeWS([
        FakeMsg("BINARY", build_packet(OP_MESSAGE, body, PROTO_ZLIB)),
        FakeMsg("CLOSE"),
    ])
    await client._recv_loop(ws2)
    await asyncio.sleep(0.05)
    check("收到弹幕并派发事件", len(got) == 1 and got[0]["data"]["info"][1] == "点歌 稻香")
    check("连接关闭后状态为 closed", client.get_status() == "closed")

    # 非法帧不影响后续
    got.clear()
    ws3 = FakeWS([
        FakeMsg("TEXT", b""),
        FakeMsg("BINARY", b"\x00"),                    # 坏包
        FakeMsg("BINARY", build_packet(OP_MESSAGE, body, PROTO_ZLIB)),
        FakeMsg("CLOSE"),
    ])
    await client._recv_loop(ws3)
    await asyncio.sleep(0.05)
    check("坏帧被跳过, 正常帧仍派发", len(got) == 1)

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
