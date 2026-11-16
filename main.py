"""
B站直播点歌台 · AstrBot 插件入口。

功能一览:
  · B 站直播间弹幕监听(匿名可连), 识别 `点歌 歌名`;
  · 网易云直链 / 落月 API(网易云、QQ、酷狗) 多音源搜索与直链解析;
  · 点歌队列: 冷却 / 单人上限 / 队列上限 / 单曲时长保护 / 去重;
  · OBS 播放层: 独立 aiohttp 服务(默认 60716), 提供 mini/standard/sidebar 三种布局,
    OBS 浏览器源打开即可播放音频, 状态通过 WebSocket 实时同步;
  · 控制面板: 队列增删/排序/清空、搜索点歌、暂停/下一首/音量、弹幕重连;
  · QQ 命令: `/点歌 歌名` + 管理员子命令(状态/跳过/清空/暂停/继续/音量/面板/重连)。

设计取舍见 README「架构」一节: 音频只在 OBS 浏览器源里播放, 服务器不需要声卡。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import command
from astrbot.core.star.filter.command import GreedyStr

from .core.danmaku import DanmakuService, credential_from_cookie
from .core.models import SOURCE_BOT, SOURCE_DANMAKU
from .core.music import build_music_service
from .core.music.base import MusicError
from .core.overlay import OverlayHub, OverlayServer, OverlaySettings
from .core.panel import PanelService
from .core.player import PlayerService, PlayerSettings
from .core.queue_manager import QueueManager, QueueSettings
from .core.store import QueueStore

PLUGIN_NAME = "astrbot_plugin_bili_live_music"
VERSION = "1.0.0"
plugin_dir = Path(__file__).parent

# 管理员子命令(其余内容都按"点歌 歌名"处理)
_SUBCMDS = ("状态", "队列", "跳过", "下一首", "清空", "暂停", "继续", "音量", "面板", "重连", "帮助")


def _resolve_data_dir() -> Path:
    """优先用 AstrBot 的插件数据目录(升级插件不会被覆盖), 失败时退化为插件内 data/"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    except Exception:  # noqa: BLE001
        return plugin_dir / "data"


@register(
    PLUGIN_NAME,
    "Fire_King",
    "B站直播点歌：弹幕点歌 + 多音源 + OBS 播放层 + 控制面板",
    VERSION,
    "https://github.com/Fire0King/astrbot_plugin_bili_live_music",
)
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = config
        self.data_dir = _resolve_data_dir()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"创建数据目录失败({self.data_dir}): {e}")

        # 队列: 状态落盘, 重启后恢复(上次没播完的放回队首)
        self.queue = QueueManager(
            QueueSettings.from_config(config),
            QueueStore(self.data_dir / "state.json"),
        )

        # 播放器 + Overlay 广播
        self.hub = OverlayHub()
        self.player = PlayerService(
            self.queue,
            self.hub,
            PlayerSettings(
                load_timeout_secs=float(config.get("playback_load_timeout", 25) or 25),
                state_broadcast_secs=2.0,
            ),
        )

        self.music: Any = None
        self.danmaku: Optional[DanmakuService] = None
        self.panel: Optional[PanelService] = None
        self.overlay: Optional[OverlayServer] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._danmaku_last: Dict[str, float] = {}
        self._boot_task: Optional[asyncio.Task] = None
        self._boot_task = asyncio.create_task(self._bootstrap())

    # ==================== 启动 / 关闭 ====================

    async def _bootstrap(self) -> None:
        try:
            self._session = aiohttp.ClientSession()
            self.music = build_music_service(self.cfg, self._session)

            room_id = int(self.cfg.get("room_id") or 0)
            credential = credential_from_cookie(str(self.cfg.get("bilibili_cookie") or ""))
            self.danmaku = DanmakuService(
                room_id=room_id,
                credential=credential,
                on_message=self._on_danmaku,
                connect_timeout=float(self.cfg.get("danmaku_connect_timeout", 10) or 10),
            )
            self.panel = PanelService(
                self.queue, self.player, self.music, self.danmaku,
                urls_provider=self._overlay_urls,
            )

            self.overlay = OverlayServer(
                self.hub,
                OverlaySettings.from_config(self.cfg),
                self.panel,
                page_dir=plugin_dir / "pages",
            )
            try:
                await self.overlay.start()
            except Exception as e:  # noqa: BLE001 — 端口冲突不该拖死整个插件
                logger.error(
                    f"Overlay 服务启动失败(端口 {self.cfg.get('obs_server_port', 60716)}): {e}。"
                    f"OBS/面板不可用, 但 QQ 命令仍可用; 改端口后重载插件即可。"
                )

            # 队列变化 → 立刻推 Overlay
            self.queue.on_change = self.player.notify_change
            await self.player.start()

            if bool(self.cfg.get("enable_danmaku", True)) and room_id > 0:
                await self.danmaku.start()
            elif room_id <= 0:
                logger.warning("未配置 room_id, 弹幕点歌不会启动(可在配置里填直播间号)")

            logger.info(
                f"点歌台已就绪: 直播间 {room_id or '未配置'} | 音源 {self.music.describe()} | "
                f"面板 {self.overlay.panel_url() if self.overlay else '不可用'}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"点歌台初始化失败: {e}")

    async def terminate(self) -> None:
        logger.info("B站直播点歌台正在卸载...")
        for attr in ("danmaku", "player", "overlay"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                await obj.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"停止 {attr} 失败: {e}")
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()
        logger.info("B站直播点歌台已卸载")

    # ==================== 地址 ====================

    def _overlay_urls(self) -> Dict[str, str]:
        if self.overlay is None:
            return {}
        return {
            "player": self.overlay.overlay_url("player", "standard"),
            "display": self.overlay.overlay_url("display", "standard"),
            "mini": self.overlay.overlay_url("player", "mini"),
            "sidebar": self.overlay.overlay_url("player", "sidebar"),
            "panel": self.overlay.panel_url(),
        }

    # ==================== 点歌核心 ====================

    async def _request_song(
        self, keyword: str, requester: str, requester_key: str,
        source: str = SOURCE_DANMAKU, bypass: bool = False,
    ) -> tuple[bool, str]:
        """搜歌 → 解析直链 → 入队 → 必要时开始播放。返回 (是否成功, 给用户/日志的说明)"""
        if self.music is None:
            return False, "点歌台还在启动, 稍后再试"
        keyword = (keyword or "").strip()
        if not keyword:
            return False, "歌名是空的"
        try:
            song = await self.music.pick(keyword, self.music.settings.search_limit)
        except MusicError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            logger.error(f"搜歌异常: {e}")
            return False, f"搜歌失败: {e}"

        result = self.queue.enqueue(song, requester, requester_key, source=source, bypass_limits=bypass)
        if not result.ok:
            return False, result.detail or "没能加入队列"
        await self.player.tick()
        return True, f"{song.display}（第 {result.position} 位）"

    async def _on_danmaku(self, message: Any) -> None:
        """弹幕回调: 识别 `点歌 xxx`"""
        if not bool(self.cfg.get("enable_danmaku_request", True)):
            return
        prefix = str(self.cfg.get("command_prefix") or "点歌").strip() or "点歌"
        text = str(getattr(message, "text", "") or "").strip()
        if not text.startswith(prefix):
            return
        keyword = text[len(prefix):].strip(" \t:：,，。.")
        if not keyword:
            return
        # 同一观众 3 秒内只处理一次, 防止刷屏
        key = f"uid:{message.uid}" if getattr(message, "uid", 0) else f"name:{message.username}"
        now = time.time()
        if now - self._danmaku_last.get(key, 0) < 3:
            return
        self._danmaku_last[key] = now

        ok, detail = await self._request_song(keyword, message.username, key, SOURCE_DANMAKU)
        logger.info(
            f"弹幕点歌 {'成功' if ok else '失败'}: {message.username} -> {keyword!r} | {detail}"
        )

    def _is_allowed_bot_request(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        if not bool(self.cfg.get("bot_request_enabled", False)):
            return False
        whitelist = str(self.cfg.get("bot_request_whitelist") or "").strip()
        if not whitelist:
            return True
        allowed = {item.strip() for item in whitelist.replace("，", ",").split(",") if item.strip()}
        return event.get_sender_id() in allowed

    # ==================== QQ 命令 ====================

    @command("点歌")
    async def cmd_dian_ge(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """点歌 / 点歌台控制"""
        text = str(arg or "").strip()
        admin = event.is_admin()

        if not text or text in ("帮助", "help"):
            yield event.plain_result(self._help_text(admin))
            return

        head, _, rest = text.partition(" ")
        rest = rest.strip()

        if head in _SUBCMDS:
            if not admin:
                yield event.plain_result("🙅 只有管理员能用点歌台控制命令")
                return
            async for reply in self._handle_admin_sub(head, rest):
                yield reply
            return

        if not self._is_allowed_bot_request(event):
            yield event.plain_result(
                "🎵 点歌目前只在直播间弹幕里开放（`点歌 歌名`）。\n"
                "想在这里点歌的话, 让管理员在插件配置里打开 `bot_request_enabled`。"
            )
            return

        ok, detail = await self._request_song(
            text, event.get_sender_name() or event.get_sender_id(),
            f"umo:{event.get_sender_id()}", SOURCE_BOT,
        )
        yield event.plain_result(
            f"✅ 已加入点歌队列：{detail}" if ok else f"❌ {detail}"
        )

    async def _handle_admin_sub(self, head: str, rest: str):
        """管理员子命令(异步生成器, 统一 yield 文本结果)"""
        if head in ("状态", "队列"):
            yield self._render_status()
            return
        if head in ("跳过", "下一首"):
            if self.queue.current is None:
                yield "当前没有在播放的歌"
                return
            title = self.queue.current.song.display
            await self.player.skip("管理员跳过")
            yield f"⏭ 已跳过：{title}"
            return
        if head == "清空":
            removed = self.queue.clear_waiting()
            self.player.notify_change()
            yield f"🧹 已清空等待队列（{removed} 首），当前播放不受影响"
            return
        if head == "暂停":
            yield "⏸ 已暂停" if await self.player.pause() else "当前没有在播放的歌"
            return
        if head == "继续":
            yield "▶ 已继续" if await self.player.resume() else "当前不是暂停状态"
            return
        if head == "音量":
            if not rest:
                yield f"当前音量 {self.queue.volume}（用法：点歌 音量 60）"
                return
            try:
                value = int(rest)
            except ValueError:
                yield "音量要填 0-100 的数字"
                return
            yield f"🔊 音量已设为 {self.player.set_volume(value)}"
            return
        if head == "面板":
            if self.overlay is None:
                yield "Overlay 服务没起来, 看日志里『Overlay 服务启动失败』那一行"
                return
            urls = self._overlay_urls()
            yield (
                "🖥 控制面板（只在私聊/自己看, 含访问令牌）：\n"
                f"{urls.get('panel', '')}\n\n"
                "📺 OBS 浏览器源（播放器, 放音频）：\n"
                f"{urls.get('player', '')}\n\n"
                "纯展示（静音）：\n"
                f"{urls.get('display', '')}"
            )
            return
        if head == "重连":
            if self.danmaku is None:
                yield "弹幕监听未启用"
                return
            ok = await self.danmaku.restart()
            snap = self.danmaku.snapshot()
            yield f"{'🔄 已重连' if ok else '⚠️ 重连未启动'}: 状态 {snap['state']}, 已重试 {snap['attempts']} 次"
            return
        yield "未知操作"

    def _render_status(self) -> str:
        cur = self.queue.current
        lines = ["🎵 点歌台状态"]
        if cur is None:
            lines.append("当前：空闲")
        else:
            lines.append(f"当前：{cur.song.title} - {cur.song.artist}（{cur.requester} 点）")
            lines.append(f"状态：{self.player.status}  音量：{self.queue.volume}")
        if self.queue.waiting:
            lines.append(f"等待队列（{len(self.queue.waiting)} 首）：")
            for index, item in enumerate(self.queue.waiting[:5], start=1):
                lines.append(f"  {index}. {item.song.title} - {item.song.artist}（{item.requester}）")
            if len(self.queue.waiting) > 5:
                lines.append(f"  …另有 {len(self.queue.waiting) - 5} 首")
        else:
            lines.append("等待队列：空")
        if self.danmaku is not None:
            snap = self.danmaku.snapshot()
            lines.append(f"弹幕：{snap['state']}（已收 {snap['danmaku']} 条）")
        if self.music is not None:
            lines.append(f"音源：{self.music.describe()}")
        if self.overlay is not None:
            lines.append(f"OBS/面板：{self.overlay.base_url()}")
        return "\n".join(lines)

    def _help_text(self, admin: bool) -> str:
        base = (
            "🎵 B站直播点歌台\n"
            "· 直播间弹幕发「点歌 歌名」即可点歌\n"
            "· 队列、当前播放、封面进度 都在 OBS 画面里显示"
        )
        if not admin:
            return base
        return base + (
            "\n\n管理员命令：\n"
            "/点歌 状态 | 跳过 | 清空 | 暂停 | 继续 | 音量 60 | 面板 | 重连"
        )
