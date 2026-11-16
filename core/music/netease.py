"""
网易云直链音源。

搜索走 music.163.com 的公开搜索接口; 播放地址不直接解析, 而是交给第三方
"meting" 直链节点生成(与原插件一致) —— 这类节点经常挂, 所以:
  · 节点列表可配置;
  · 默认会先验证直链真的能下载, 再返回给队列;
  · 第一个节点失败自动换下一个。
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

from astrbot.api import logger

from ..models import Song
from .base import HttpClient, MusicProvider, MusicSettings, MusicError, normalize_cover

_SEARCH_URL = "https://music.163.com/api/search/get/web"
_DETAIL_URL = "https://music.163.com/api/song/detail/"
_REFERER = {"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0"}

# 节点 -> 直链模板(未知节点按 meting 通用格式兜底)
_NODE_TEMPLATES: Dict[str, str] = {
    "api.injahow.cn": "https://api.injahow.cn/meting/?type=url&id={id}",
    "api.qijieya.cn": "https://api.qijieya.cn/meting/?type=url&id={id}",
    "meting.jmstrand.cn": "https://meting.jmstrand.cn/?type=url&id={id}",
    "metingapi.nanorocky.top": "https://metingapi.nanorocky.top/?server=netease&type=url&id={id}",
}


class NeteaseProvider(MusicProvider):
    name = "netease"

    def __init__(self, http: HttpClient, settings: MusicSettings):
        self.http = http
        self.settings = settings

    def describe(self) -> str:
        return f"网易云直链 · {self.settings.netease_nodes[0] if self.settings.netease_nodes else '无节点'}"

    def handles(self, source: str) -> bool:
        return source == "netease"

    async def search(self, keyword: str, limit: int) -> List[Song]:
        data = await self.http.get_json(
            _SEARCH_URL,
            params={
                "csrf_token": "",
                "hlpretag": "",
                "hlposttag": "",
                "s": keyword,
                "type": 1,
                "offset": 0,
                "total": "true",
                "limit": max(1, limit),
            },
            headers=_REFERER,
            timeout=self.settings.timeout_secs,
        )
        if not isinstance(data, dict) or data.get("code") != 200:
            raise MusicError(f"网易云搜索返回异常: {data.get('code') if isinstance(data, dict) else '?'}")

        raw_songs = ((data.get("result") or {}).get("songs") or [])
        songs: List[Song] = []
        for item in raw_songs:
            if not isinstance(item, dict):
                continue
            artists = [a.get("name") for a in (item.get("artists") or []) if isinstance(a, dict) and a.get("name")]
            album = item.get("album") or {}
            songs.append(Song(
                source="netease",
                song_id=str(item.get("id") or ""),
                title=str(item.get("name") or ""),
                artist="/".join(artists) or "未知歌手",
                album=str(album.get("name") or ""),
                duration_ms=int(item.get("duration") or 0),
                cover=normalize_cover(album.get("picUrl")),
            ))
        await self._enrich_covers(songs)
        return [s for s in songs if s.title]

    async def _enrich_covers(self, songs: List[Song]) -> None:
        missing = [s for s in songs if not s.cover and s.song_id]
        if not missing:
            return
        try:
            import json
            data = await self.http.get_json(
                _DETAIL_URL,
                params={"ids": json.dumps([int(s.song_id) for s in missing if s.song_id.isdigit()])},
                headers=_REFERER,
                timeout=self.settings.timeout_secs,
            )
        except Exception as e:  # noqa: BLE001 — 封面补全失败不影响搜索
            logger.debug(f"网易云封面补全失败: {e}")
            return
        if not isinstance(data, dict) or data.get("code") != 200:
            return
        covers: Dict[str, str] = {}
        for item in (data.get("songs") or []):
            if not isinstance(item, dict):
                continue
            album = item.get("album") or {}
            covers[str(item.get("id"))] = normalize_cover(album.get("picUrl") or (item.get("al") or {}).get("picUrl"))
        for song in missing:
            song.cover = covers.get(song.song_id, "") or song.cover

    def build_url(self, song_id: str) -> List[str]:
        """按节点顺序给出候选直链"""
        urls: List[str] = []
        for node in self.settings.netease_nodes:
            template = _NODE_TEMPLATES.get(node) or f"https://{node}/meting/?type=url&id={{id}}"
            urls.append(template.format(id=song_id))
        return urls

    async def resolve(self, song: Song) -> Optional[Song]:
        if not song.song_id:
            return None
        candidates = self.build_url(song.song_id)
        for index, url in enumerate(candidates):
            if self.settings.verify_url:
                ok = await self.http.url_playable(url, timeout=min(6.0, self.settings.timeout_secs))
                if not ok:
                    logger.info(f"直链节点不可用, 换下一个: {url.split('/')[2]} ({song.title})")
                    continue
            if index > 0:
                logger.info(f"直链使用备用节点: {url.split('/')[2]} ({song.title})")
            return dataclasses.replace(song, url=url)
        logger.warning(f"所有网易云直链节点都不可用: {song.title}")
        return None
