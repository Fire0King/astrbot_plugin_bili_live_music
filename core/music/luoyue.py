"""
落月 API 音源(网易云 / QQ音乐 / 酷狗)。

接口约定(与原插件一致):
  搜索: GET {base}/v2/music/{source}?word=<kw>&num=<n>&quality=<q>
  详情: GET {base}/v2/music/{source}?id=<id>&quality=<q>      (QQ 音乐用 mid)
        GET {base}/v2/music/kugou?hash=<hash>&quality=<q>      (酷狗按 hash, 有多种降级参数)
返回统一为 {code: 200, data: {...} 或 [...]}。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from ..models import Song
from .base import HttpClient, MusicError, MusicProvider, MusicSettings, parse_duration

_VALID_SOURCES = ("netease", "tencent", "kugou")


class LuoyueProvider(MusicProvider):
    name = "luoyue"

    def __init__(self, http: HttpClient, settings: MusicSettings):
        self.http = http
        self.settings = settings

    def describe(self) -> str:
        return f"落月 API · {'/'.join(self.sources) or '未启用源'} · {self.settings.luoyue_base}"

    @property
    def sources(self) -> List[str]:
        return [s for s in self.settings.luoyue_sources if s in _VALID_SOURCES]

    def handles(self, source: str) -> bool:
        return source in self.sources

    def _quality(self, source: str) -> str:
        return str(self.settings.quality.get(source, ""))

    async def _get(self, path: str, params: Dict[str, Any], operation: str) -> Any:
        url = f"{self.settings.luoyue_base}{path}"
        data = await self.http.get_json(url, params=params, timeout=self.settings.timeout_secs)
        if not isinstance(data, dict):
            raise MusicError(f"落月 API {operation} 返回格式异常")
        if data.get("code") != 200 or data.get("data") is None:
            raise MusicError(str(data.get("message") or data.get("msg") or f"落月 API {operation} 返回异常: {data.get('code')}"))
        return data.get("data")

    async def search_source(self, source: str, keyword: str, limit: int) -> List[Song]:
        data = await self._get(
            f"/v2/music/{source}",
            {"word": keyword, "num": max(1, limit), "quality": self._quality(source)},
            "搜索",
        )
        items = data if isinstance(data, list) else [data]
        songs = [self._normalize(source, item) for item in items if isinstance(item, dict)]
        return [song for song in songs if song is not None]

    async def search(self, keyword: str, limit: int) -> List[Song]:
        """多平台并行搜索 + 交替合并(与原插件一致)"""
        sources = self.sources
        if not sources:
            return []
        per_source = max(1, (limit + len(sources) - 1) // len(sources))
        import asyncio
        groups = await asyncio.gather(
            *[self._safe_search(source, keyword, per_source) for source in sources],
            return_exceptions=False,
        )
        merged: List[Song] = []
        longest = max((len(g) for g in groups), default=0)
        for index in range(longest):
            for group in groups:
                if index < len(group) and len(merged) < limit:
                    merged.append(group[index])
        return merged

    async def _safe_search(self, source: str, keyword: str, limit: int) -> List[Song]:
        try:
            return await self.search_source(source, keyword, limit)
        except MusicError as e:
            logger.warning(f"落月音源 {source} 搜索失败: {e}")
            return []

    async def resolve(self, song: Song) -> Optional[Song]:
        source = song.source if song.source in _VALID_SOURCES else (self.sources[0] if self.sources else "")
        if not source:
            return None
        if source == "kugou":
            return await self._resolve_kugou(song)
        params: Dict[str, Any] = {"quality": self._quality(source)}
        if source == "tencent" and song.extra.get("mid"):
            params["mid"] = song.extra["mid"]
        elif song.song_id:
            params["id"] = song.song_id
        else:
            return None
        data = await self._get(f"/v2/music/{source}", params, "详情")
        resolved = self._normalize(source, data, song)
        if resolved is None or not resolved.url:
            raise MusicError(f"落月 API 未返回播放地址: {song.display}")
        return resolved

    async def _resolve_kugou(self, song: Song) -> Optional[Song]:
        quality = self._quality("kugou")
        preferred = song.extra.get("hq_hash") if quality == "320" else (
            song.extra.get("sq_hash") if quality == "flac" else None
        )
        attempts: List[Dict[str, str]] = []
        for hash_value in dict.fromkeys([preferred, song.extra.get("hash")]):
            if hash_value:
                attempts.append({"hash": hash_value})
        if song.extra.get("album_id"):
            attempts.append({
                "hash": str(song.extra.get("hash") or ""),
                "album_id": str(song.extra.get("album_id")),
                "album_audio_id": str(song.extra.get("album_audio_id") or ""),
            })
        last_error = ""
        for params in attempts:
            if not params.get("hash"):
                continue
            params = {k: v for k, v in params.items() if v}
            params["quality"] = quality
            try:
                data = await self._get("/v2/music/kugou", params, "详情")
            except MusicError as e:
                last_error = str(e)
                continue
            resolved = self._normalize("kugou", data, song)
            if resolved and resolved.url:
                return resolved
            last_error = "落月 API 未返回酷狗播放地址"
        raise MusicError(last_error or f"酷狗取链失败: {song.display}")

    @staticmethod
    def _normalize(source: str, data: Any, fallback: Optional[Song] = None) -> Optional[Song]:
        if not isinstance(data, dict):
            return None
        duration = (
            parse_duration(data.get("interval"))
            or int(data.get("duration") or 0)
            or (fallback.duration_ms if fallback else 0)
        )
        title = data.get("song") or data.get("name") or data.get("title") or (fallback.title if fallback else "")
        artist = data.get("singer") or data.get("artist") or data.get("author") or (fallback.artist if fallback else "")
        if not title or not artist:
            return None
        extra: Dict[str, str] = dict(fallback.extra) if fallback else {}
        for key, value in (
            ("mid", data.get("mid")),
            ("hash", data.get("hash")),
            ("hq_hash", data.get("hq_hash") or data.get("hqHash")),
            ("sq_hash", data.get("sq_hash") or data.get("sqHash")),
            ("album_id", data.get("album_id") or data.get("albumID")),
            ("album_audio_id", data.get("album_audio_id") or data.get("albumAudioID")),
        ):
            if value:
                extra[key] = str(value)
        return Song(
            source=source,
            song_id=str(data.get("id") or (fallback.song_id if fallback else "")),
            title=str(title),
            artist=str(artist),
            album=str(data.get("album") or (fallback.album if fallback else "")),
            duration_ms=duration,
            cover=str(data.get("cover") or data.get("pic") or (fallback.cover if fallback else "")),
            url=str(data.get("url") or data.get("link") or data.get("play_url") or ""),
            extra=extra,
        )
