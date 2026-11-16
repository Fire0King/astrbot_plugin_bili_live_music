"""
B 站凭据的来源解析。

公开直播间匿名就能连弹幕, 所以凭据是**可选项**; 但受限直播间(需要登录才收弹幕、
或者要显示完整用户名)就得带 Cookie。为了少让用户填一遍, 这里优先借用
`astrbot_plugin_bilibili` 已经登录保存的凭据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from astrbot.api import logger

# 需要的字段(SESSDATA 必填, 其余用于过风控)
_BILI_COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid", "buvid4", "sid")
# 拼回 Cookie 时要用 B 站认的名字(大小写敏感), 否则下游解析不到 DedeUserID
_BILI_COOKIE_NAMES = {
    "sessdata": "SESSDATA",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "dedeuserid": "DedeUserID",
    "buvid4": "buvid4",
    "sid": "sid",
}


def extract_bili_cookie_from_file(path: Optional[Path]) -> str:
    """从 bilibili 插件的数据文件里提取凭据, 拼成 `k=v; k=v`。

    只取已知字段, 不打印任何值; 文件不存在/结构不符时返回空串。
    """
    if not path or not Path(path).exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:      # 兼容 BOM
            data: Any = json.load(fp)
    except Exception:  # noqa: BLE001
        return ""

    found: List[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            lower = {str(k).lower(): v for k, v in obj.items()}
            sessdata = lower.get("sessdata")
            if isinstance(sessdata, str) and sessdata.strip():
                pairs = []
                for key in _BILI_COOKIE_KEYS:
                    val = lower.get(key)
                    if isinstance(val, str) and val.strip():
                        name = _BILI_COOKIE_NAMES.get(key, key)
                        pairs.append(f"{name}={val.strip()}")
                if pairs:
                    found.append("; ".join(pairs))
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)

    walk(data)
    return found[0] if found else ""


def resolve_bilibili_cookie(
    explicit: str,
    data_dir: Optional[Path] = None,
    allow_borrow: bool = True,
) -> str:
    """取 B 站凭据: 配置里填了就用配置; 否则尝试借用 bilibili 插件的数据。"""
    text = (explicit or "").strip()
    if text:
        return text
    if not allow_borrow or data_dir is None:
        return ""

    data_dir = Path(data_dir)
    candidates = [
        data_dir.parent / "astrbot_plugin_bilibili" / "astrbot_plugin_bilibili.json",
        data_dir.parent.parent / "config" / "astrbot_plugin_bilibili_config.json",
        data_dir.parent.parent / "plugin_data" / "astrbot_plugin_bilibili" / "astrbot_plugin_bilibili.json",
    ]
    for path in candidates:
        cookie = extract_bili_cookie_from_file(path)
        if cookie:
            logger.info(
                f"B 站凭据: 已从 bilibili 插件数据借用 ({path.name}), "
                f"含 {len(cookie.split('; '))} 个字段 (不回显)"
            )
            return cookie
    return ""
