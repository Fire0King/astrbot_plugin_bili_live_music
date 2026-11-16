# -*- coding: utf-8 -*-
"""B 站凭据借用逻辑单测（纯文件读写, 不联网）。"""

import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fake = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
LOGS = []


class _Logger:
    def debug(self, m):
        LOGS.append(("DEBUG", m))

    def info(self, m):
        LOGS.append(("INFO", m))

    def warning(self, m):
        LOGS.append(("WARN", m))

    def error(self, m):
        LOGS.append(("ERR", m))


api.logger = _Logger()
fake.api = api
sys.modules.update({"astrbot": fake, "astrbot.api": api})

from core.bili_cookie import extract_bili_cookie_from_file, resolve_bilibili_cookie  # noqa: E402

results = []


def check(label, cond):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label)


def main():
    tmp = Path(tempfile.mkdtemp())

    # 1) 嵌套结构里找 SESSDATA
    nested = tmp / "nested.json"
    nested.write_text(json.dumps({
        "accounts": [{"user": {"SESSDATA": "sess-1", "bili_jct": "jct-1", "buvid3": "bv-1", "extra": 1}}],
        "other": {"x": 1},
    }, ensure_ascii=False), encoding="utf-8")
    cookie = extract_bili_cookie_from_file(nested)
    check("嵌套结构能提取", "SESSDATA=sess-1" in cookie and "bili_jct=jct-1" in cookie)
    check("只取已知字段", "extra" not in cookie)

    # 2) BOM 兼容
    bom_file = tmp / "bom.json"
    bom_file.write_bytes(("\ufeff" + json.dumps({"SESSDATA": "sess-bom"})).encode("utf-8"))
    check("BOM 文件也能读", "SESSDATA=sess-bom" in extract_bili_cookie_from_file(bom_file))

    # 3) 没有 SESSDATA 的字典不当作凭据
    no_sess = tmp / "nosess.json"
    no_sess.write_text(json.dumps({"bili_jct": "x", "uid": 1}), encoding="utf-8")
    check("没有 SESSDATA 时返回空", extract_bili_cookie_from_file(no_sess) == "")

    # 4) 坏 JSON / 不存在的文件
    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    check("坏 JSON 返回空", extract_bili_cookie_from_file(bad) == "")
    check("文件不存在返回空", extract_bili_cookie_from_file(tmp / "nope.json") == "")
    check("None 返回空", extract_bili_cookie_from_file(None) == "")

    # 5) 顶层列表也能处理
    top_list = tmp / "list.json"
    top_list.write_text(json.dumps([{"sessdata": "sess-list", "SESSDATA": "sess-list"}]), encoding="utf-8")
    check("列表结构能提取", "SESSDATA=sess-list" in extract_bili_cookie_from_file(top_list))

    # 6) resolve: 显式配置优先
    data_dir = tmp / "plugin_data" / "astrbot_plugin_bili_live_music"
    data_dir.mkdir(parents=True)
    check("显式配置优先", resolve_bilibili_cookie("SESSDATA=explicit", data_dir) == "SESSDATA=explicit")

    # 7) resolve: 从同级 bilibili 插件数据借用
    bili_dir = tmp / "plugin_data" / "astrbot_plugin_bilibili"
    bili_dir.mkdir(parents=True)
    (bili_dir / "astrbot_plugin_bilibili.json").write_text(
        json.dumps({"credential": {"sessdata": "sess-borrow", "bili_jct": "jct-b"}}), encoding="utf-8")
    borrowed = resolve_bilibili_cookie("", data_dir)
    check("能从 bilibili 插件借用", "SESSDATA=sess-borrow" in borrowed and "bili_jct=jct-b" in borrowed)
    check("借用时日志不回显凭据值", not any("sess-borrow" in str(m) for _, m in LOGS))

    # 8) 关闭借用后不读别人的数据
    check("关闭借用后返回空", resolve_bilibili_cookie("", data_dir, allow_borrow=False) == "")

    # 9) 借用失败时返回空且不抛异常（换一个干净的目录, 避免读到上一步的凭据）
    tmp2 = Path(tempfile.mkdtemp())
    empty_dir = tmp2 / "plugin_data" / "astrbot_plugin_bili_live_music"
    empty_dir.mkdir(parents=True)
    check("没有可用凭据时返回空", resolve_bilibili_cookie("", empty_dir) == "")

    print()
    print("RESULT:", "ALL PASSED" if all(results) else f"SOME FAILED ({results.count(False)})")
    return 0 if all(results) else 1


sys.exit(main())
