"""
音乐源抽象层。

三个概念:
  · MusicProvider —— 一个音源(网易云直链 / 落月 API), 负责"搜"和"取直链";
  · MusicSettings —— 从插件配置映射出来的参数(节点/音质/启用源);
  · MusicService  —— 对上层只暴露 search() / resolve() / pick(), 负责多源编排。

音源随时会挂(第三方直链节点尤其不靠谱), 因此:
  · 取直链时可以在多个节点间回退, 并且可以验证"这个 URL 真的能下载";
  · 上层拿到的一定是"已验证可播放"的 Song, 拿不到就明确报错。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from ..models import Song


class MusicError(Exception):
    """音乐源/直链解析失败"""


@dataclass
class MusicSettings:
    """音乐相关配置(插件配置映射而来)"""

    backend: str = "netease"                  # netease / luoyue
    search_limit: int = 3                     # 弹幕/Bot 点歌时每源候选数
    panel_search_limit: int = 20              # 面板搜索返回条数
    netease_nodes: List[str] = field(default_factory=lambda: [
        "api.qijieya.cn", "api.injahow.cn", "meting.jmstrand.cn", "metingapi.nanorocky.top",
    ])
    luoyue_base: str = "https://api.vkeys.cn"
    luoyue_sources: List[str] = field(default_factory=lambda: ["netease", "tencent"])
    quality: Dict[str, Any] = field(default_factory=lambda: {"netease": 1, "tencent": 10, "kugou": "320"})
    verify_url: bool = True
    timeout_secs: float = 10.0

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "MusicSettings":
        def _str_list(key: str, default: List[str]) -> List[str]:
            raw = cfg.get(key)
            if isinstance(raw, str):
                items = [x.strip() for x in re.split(r"[,;\s]+", raw) if x.strip()]
            elif isinstance(raw, list):
                items = [str(x).strip() for x in raw if str(x).strip()]
            else:
                items = []
            return items or list(default)

        quality = dict(cls.quality) if hasattr(cls, "quality") else {"netease": 1, "tencent": 10, "kugou": "320"}
        quality.update({
            "netease": cfg.get("netease_quality", quality.get("netease", 1)),
            "tencent": cfg.get("tencent_quality", quality.get("tencent", 10)),
            "kugou": str(cfg.get("kugou_quality", quality.get("kugou", "320"))),
        })
        backend = str(cfg.get("music_backend") or "netease").strip().lower()
        return cls(
            backend=backend if backend in ("netease", "luoyue") else "netease",
            search_limit=max(1, min(20, int(cfg.get("search_limit") or 3))),
            panel_search_limit=max(1, min(50, int(cfg.get("panel_search_limit") or 20))),
            netease_nodes=_str_list("netease_direct_api", cls().netease_nodes),
            luoyue_base=str(cfg.get("luoyue_api_base") or "https://api.vkeys.cn").rstrip("/"),
            luoyue_sources=[s for s in _str_list("luoyue_sources", ["netease", "tencent"]) if s in ("netease", "tencent", "kugou")],
            quality=quality,
            verify_url=bool(cfg.get("verify_music_url", True)),
            timeout_secs=float(cfg.get("music_timeout", 10) or 10),
        )


class HttpClient:
    """aiohttp Session 的薄包装(便于单测注入假 session)"""

    def __init__(self, session: Any):
        self.session = session

    async def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        async with self.session.get(
            url, params=params, headers=headers, timeout=timeout or 10
        ) as resp:
            status = int(getattr(resp, "status", 0))
            text = await resp.text()
        if status != 200:
            raise MusicError(f"HTTP {status} ({url.split('/')[2]})")
        try:
            import json
            return json.loads(text)
        except Exception as e:  # noqa: BLE001
            raise MusicError(f"返回内容不是 JSON: {e}") from e

    async def url_playable(self, url: str, timeout: float = 6.0) -> bool:
        """用 Range 请求快速判断直链是否真的可用(跟随重定向)"""
        try:
            async with self.session.get(
                url, headers={"Range": "bytes=0-1023", "User-Agent": _BROWSER_UA}, timeout=timeout
            ) as resp:
                status = int(getattr(resp, "status", 0))
                ctype = str(resp.headers.get("Content-Type", "")) if hasattr(resp, "headers") else ""
                return status in (200, 206) and ("audio" in ctype or "video" in ctype or ctype == "")
        except Exception:  # noqa: BLE001
            return False


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def parse_duration(text: Any) -> int:
    """把 "03:45" / 225 解析成毫秒"""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        value = float(text)
        # 落月接口的 interval 可能是秒, 也可能是毫秒
        return int(value * 1000) if value < 10000 else int(value)
    raw = str(text).strip()
    if not raw:
        return 0
    if raw.isdigit():
        return parse_duration(int(raw))
    parts = raw.split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return int(seconds * 1000)
    except ValueError:
        return 0


def normalize_cover(url: Optional[str]) -> str:
    if not url:
        return ""
    secure = re.sub(r"^http://", "https://", str(url).strip(), flags=re.I)
    if re.search(r"[?&]param=\d+y\d+", secure):
        return secure
    return f"{secure}{'&' if '?' in secure else '?'}param=300y300"


class MusicProvider:
    """音源接口(所有音源实现这两个方法)"""

    name = "base"

    async def search(self, keyword: str, limit: int) -> List[Song]:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def resolve(self, song: Song) -> Optional[Song]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class MusicService:
    """对上层暴露的统一入口"""

    def __init__(self, settings: MusicSettings, providers: List[MusicProvider]):
        self.settings = settings
        self.providers = providers

    async def search(self, keyword: str, limit: Optional[int] = None) -> List[Song]:
        """多源并行搜索, 交替合并(与原插件一致的错落顺序)"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        limit = max(1, int(limit or self.settings.search_limit))
        providers = self.providers
        if not providers:
            raise MusicError("没有可用的音乐源")

        per_source = max(1, (limit + len(providers) - 1) // len(providers))
        groups = await asyncio.gather(
            *[self._search_one(provider, keyword, per_source) for provider in providers],
            return_exceptions=True,
        )
        merged: List[Song] = []
        lists: List[List[Song]] = []
        for provider, group in zip(providers, groups):
            if isinstance(group, Exception):
                logger.warning(f"音乐源 {provider.name} 搜索失败: {group}")
                lists.append([])
            else:
                lists.append(group)
        longest = max((len(x) for x in lists), default=0)
        for index in range(longest):
            for group in lists:
                if index < len(group) and len(merged) < limit:
                    merged.append(group[index])
        return merged

    async def _search_one(self, provider: MusicProvider, keyword: str, limit: int) -> List[Song]:
        started = time.time()
        songs = await provider.search(keyword, limit)
        logger.info(f"音乐源 {provider.name} 搜索完成: {keyword!r} -> {len(songs)} 条 ({time.time()-started:.2f}s)")
        return songs

    async def pick(self, keyword: str, limit: Optional[int] = None) -> Song:
        """点歌入口: 搜到第一首**直链可用**的歌"""
        songs = await self.search(keyword, limit)
        if not songs:
            raise MusicError(f"没搜到「{keyword}」")
        last_error = ""
        for song in songs:
            try:
                resolved = await self.resolve(song)
            except MusicError as e:
                last_error = str(e)
                continue
            if resolved and resolved.url:
                return resolved
        raise MusicError(last_error or f"「{keyword}」找到了但都拿不到播放地址")

    async def resolve(self, song: Song) -> Optional[Song]:
        for provider in self.providers:
            if getattr(provider, "handles", None) and provider.handles(song.source):
                return await provider.resolve(song)
        # 没匹配到具体音源时, 按 backend 兜底
        return await self.providers[0].resolve(song) if self.providers else None

    def describe(self) -> str:
        return " + ".join(p.describe() for p in self.providers) or "无音源"
