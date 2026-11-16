# -*- coding: utf-8 -*-
"""音乐源单测: 网易云直链 / 落月 API / 多源编排（全部用假 HTTP, 不联网）。"""

import asyncio
import json
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

from core.models import Song                                              # noqa: E402
from core.music.base import (                                             # noqa: E402
    MusicError,
    MusicService,
    MusicSettings,
    HttpClient,
    parse_duration,
)
from core.music.luoyue import LuoyueProvider                              # noqa: E402
from core.music.netease import NeteaseProvider                            # noqa: E402

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


class FakeResp:
    def __init__(self, status=200, body="{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "audio/mpeg"}

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """routes: [(matcher, resp)]; matcher(url, params, headers) -> bool"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for matcher, resp in self.routes:
            matched = matcher(url, params or {}, headers or {})
            if matched is True:
                return resp
            if matched is not False and matched is not None:
                return matched          # matcher 直接返回响应时优先用它
        return FakeResp(500, "not matched")

    def urls(self):
        return [c[0] for c in self.calls]


def cfg(**kw):
    base = {
        "music_backend": "netease",
        "search_limit": 3,
        "netease_direct_api": "api.qijieya.cn,api.injahow.cn",
        "verify_music_url": True,
        "luoyue_api_base": "https://api.vkeys.cn",
        "luoyue_sources": "netease,tencent",
    }
    base.update(kw)
    return base


async def main():
    # ---------------- 1) 工具函数 ----------------
    check("裸毫秒", parse_duration(225000) == 225000)
    check("秒 -> 毫秒", parse_duration(225) == 225000)
    check("mm:ss", parse_duration("03:45") == 225000)
    check("坏值落 0", parse_duration("abc") == 0)
    check("None 落 0", parse_duration(None) == 0)

    settings = MusicSettings.from_config(cfg())
    check("默认后端 netease", settings.backend == "netease")
    check("节点按逗号切分", settings.netease_nodes == ["api.qijieya.cn", "api.injahow.cn"])
    bad = MusicSettings.from_config({"music_backend": "spotify"})
    check("非法后端回落 netease", bad.backend == "netease")
    check("非法音源被过滤", MusicSettings.from_config({"luoyue_sources": "xx,tencent"}).luoyue_sources == ["tencent"])

    # ---------------- 2) 网易云搜索 ----------------
    search_body = json.dumps({
        "code": 200,
        "result": {"songs": [
            {"id": 111, "name": "晴天", "duration": 269000,
             "artists": [{"name": "周杰伦"}], "album": {"name": "叶惠美", "picUrl": "http://p1.music.126.net/a.jpg"}},
            {"id": 222, "name": "无歌手字段", "duration": 1000, "artists": [], "album": {}},
        ]},
    }, ensure_ascii=False)
    detail_body = json.dumps({"code": 200, "songs": [{"id": 222, "album": {"picUrl": "https://p2.music.126.net/b.jpg"}}]})

    def is_search(url, params, headers):
        return "search/get/web" in url

    def is_detail(url, params, headers):
        return "song/detail" in url

    session = FakeSession([(is_search, FakeResp(200, search_body)), (is_detail, FakeResp(200, detail_body))])
    provider = NeteaseProvider(HttpClient(session), settings)
    songs = await provider.search("晴天", 3)
    check("搜索解析出 2 首", len(songs) == 2)
    check("歌名/歌手正确", songs[0].title == "晴天" and songs[0].artist == "周杰伦")
    check("时长解析正确", songs[0].duration_ms == 269000)
    check("封面补全 http->https + param", songs[0].cover.startswith("https://") and "param=300y300" in songs[0].cover)
    check("缺歌手的落到未知歌手", songs[1].artist == "未知歌手")
    check("封面缺失时走详情接口补全", "p2.music.126.net" in songs[1].cover)
    check("搜索请求带 Referer", any("music.163.com" in (h.get("Referer") or "") for _, _, h in session.calls))

    # ---------------- 3) 网易云直链: 节点回退 ----------------
    good_urls = []

    def node_ok(url, params, headers):
        good_urls.append(url)
        return "injahow" in url

    session_ok = FakeSession([(node_ok, FakeResp(200, b"", {"Content-Type": "audio/mpeg"}))])
    p2 = NeteaseProvider(HttpClient(session_ok), settings)
    resolved = await p2.resolve(songs[0])
    check("第一个节点失败后换备用节点成功", resolved is not None and "injahow" in resolved.url)
    check("尝试了 2 个节点", len(good_urls) == 2)

    session_bad = FakeSession([(lambda u, p, h: True, FakeResp(404, ""))])
    p3 = NeteaseProvider(HttpClient(session_bad), settings)
    check("所有节点都挂时返回 None", await p3.resolve(songs[0]) is None)

    # 关闭校验时不发请求, 直接给第一节点
    settings_noverify = MusicSettings.from_config(cfg(verify_music_url=False))
    session_none = FakeSession([])
    p4 = NeteaseProvider(HttpClient(session_none), settings_noverify)
    r4 = await p4.resolve(songs[0])
    check("关闭校验后不探测", r4 is not None and session_none.calls == [])

    # ---------------- 4) 落月: 多源交替合并 ----------------
    luoyue_session = FakeSession([
        (lambda u, p, h: "/v2/music/netease" in u and p.get("word") == "晴天",
         FakeResp(200, json.dumps({"code": 200, "data": [
             {"id": 1, "song": "晴天", "singer": "周杰伦", "interval": "04:29"},
             {"id": 3, "song": "晴天2", "singer": "某人", "interval": "03:00"},
         ]}, ensure_ascii=False))),
        (lambda u, p, h: "/v2/music/tencent" in u and p.get("word") == "晴天",
         FakeResp(200, json.dumps({"code": 200, "data": {
             "id": 2, "mid": "m-2", "song": "晴天", "singer": "周杰伦", "duration": 269000,
         }}, ensure_ascii=False))),
    ])
    luo = LuoyueProvider(HttpClient(luoyue_session), MusicSettings.from_config(cfg(music_backend="luoyue")))
    merged = await luo.search("晴天", 3)
    check("交替合并顺序 netease/tencent/netease", [s.song_id for s in merged] == ["1", "2", "3"])
    check("落月解析 mid", merged[1].extra.get("mid") == "m-2")
    check("落月时长 mm:ss 解析", merged[0].duration_ms == 269000)

    # ---------------- 5) 落月: tencent 详情用 mid ----------------
    detail_calls = []

    def tencent_detail(url, params, headers):
        if "/v2/music/tencent" in url and params.get("mid"):
            detail_calls.append(params)
            return True
        return False

    luoyue_session2 = FakeSession([
        (tencent_detail, FakeResp(200, json.dumps({"code": 200, "data": {
            "url": "https://audio.example.com/a.mp3", "song": "晴天", "singer": "周杰伦",
        }}, ensure_ascii=False))),
    ])
    luo2 = LuoyueProvider(HttpClient(luoyue_session2), MusicSettings.from_config(cfg(music_backend="luoyue")))
    song_tencent = Song(source="tencent", song_id="2", title="晴天", artist="周杰伦", extra={"mid": "m-2"})
    res = await luo2.resolve(song_tencent)
    check("腾讯详情用 mid 请求", detail_calls and detail_calls[0].get("mid") == "m-2")
    check("取到落月播放地址", res is not None and res.url.endswith(".mp3"))

    # ---------------- 6) 落月: 酷狗 hash 降级 ----------------
    kugou_attempts = []

    def kugou(url, params, headers):
        if "/v2/music/kugou" in url:
            kugou_attempts.append(dict(params))
            # 第一种(仅 hash)返回空 url, 第二种(带 album 参数)成功
            if params.get("album_id"):
                return FakeResp(200, json.dumps({"code": 200, "data": {
                    "url": "https://kg.example.com/1.mp3", "song": "七里香", "singer": "周杰伦",
                }}, ensure_ascii=False))
            return FakeResp(200, json.dumps({"code": 200, "data": {"song": "七里香", "singer": "周杰伦"}}, ensure_ascii=False))
        return False

    luo3 = LuoyueProvider(HttpClient(FakeSession([(kugou, FakeResp(200, "{}"))])),
                          MusicSettings.from_config(cfg(music_backend="luoyue", luoyue_sources="kugou")))
    song_kugou = Song(source="kugou", song_id="9", title="七里香", artist="周杰伦",
                      extra={"hash": "h1", "album_id": "al1", "album_audio_id": "aa1"})
    res_kugou = await luo3.resolve(song_kugou)
    check("酷狗取链降级成功", res_kugou is not None and res_kugou.url.endswith(".mp3"))
    check("酷狗先试 hash 再试 album 参数", len(kugou_attempts) >= 2 and kugou_attempts[1].get("album_id") == "al1")

    # ---------------- 7) MusicService.pick: 跳过取不到链的候选 ----------------
    class FakeProvider:
        name = "fake"

        def __init__(self):
            self.calls = []

        async def search(self, keyword, limit):
            return [
                Song(source="netease", song_id="bad", title="坏", artist="x"),
                Song(source="netease", song_id="good", title="好", artist="y"),
            ]

        async def resolve(self, song):
            self.calls.append(song.song_id)
            if song.song_id == "bad":
                raise MusicError("节点全挂")
            return Song(source="netease", song_id="good", title="好", artist="y", url="https://ok/1.mp3")

        def describe(self):
            return "fake"

    fp = FakeProvider()
    svc = MusicService(MusicSettings(), [fp])
    picked = await svc.pick("随便")
    check("pick 跳过失败候选", picked.song_id == "good" and picked.url.endswith(".mp3"))
    check("pick 依次尝试", fp.calls == ["bad", "good"])

    # ---------------- 8) MusicService 多源异常隔离 ----------------
    class Boom:
        name = "boom"

        async def search(self, keyword, limit):
            raise MusicError("炸了")

        async def resolve(self, song):
            return None

        def describe(self):
            return "boom"

    class Fine:
        name = "fine"

        async def search(self, keyword, limit):
            return [Song(source="netease", song_id="1", title="好", artist="y")]

        async def resolve(self, song):
            return song

        def describe(self):
            return "fine"

    svc2 = MusicService(MusicSettings(), [Boom(), Fine()])
    got = await svc2.search("x", 3)
    check("一个源炸了不影响另一个", len(got) == 1 and got[0].song_id == "1")

    try:
        await MusicService(MusicSettings(), []).search("x")
        check("没有音源时报错", False)
    except MusicError:
        check("没有音源时报错", True)

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
