"""音乐源工厂: 按配置装配音源(对上层只暴露 MusicService)。"""

from __future__ import annotations

from typing import Any, Dict

from .base import HttpClient, MusicService, MusicSettings
from .luoyue import LuoyueProvider
from .netease import NeteaseProvider

__all__ = [
    "HttpClient",
    "LuoyueProvider",
    "MusicService",
    "MusicSettings",
    "NeteaseProvider",
    "build_music_service",
]


def build_music_service(cfg: Dict[str, Any], session: Any) -> MusicService:
    """根据配置创建音乐服务。

    backend=netease → 只启用网易云直链(轻量, 不依赖第三方聚合 API);
    backend=luoyue  → 走落月 API, 可同时启用网易云/QQ/酷狗多源。
    """
    settings = MusicSettings.from_config(cfg or {})
    http = HttpClient(session)
    if settings.backend == "luoyue":
        providers = [LuoyueProvider(http, settings)]
    else:
        providers = [NeteaseProvider(http, settings)]
    return MusicService(settings, providers)
