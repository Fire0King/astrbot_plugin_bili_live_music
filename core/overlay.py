"""
OBS 播放层服务(aiohttp)。

为什么不用 AstrBot 的 dashboard 路由:
  Overlay 需要 **WebSocket 实时推状态**, 而且要一个只暴露给本机/OBS 的独立端口
  (原 Koishi 插件在 60716 也是这个思路)。独立端口还有个好处: 重启 AstrBot
  面板不会把正在播放的 OBS 页面带下线(页面会自动重连)。

对外提供:
  GET  /                      控制面板(需 token)
  GET  /overlay?mode=&layout= OBS 播放页(需 token)
  GET  /font.ttf              字体
  GET  /api/health            健康检查(无需 token)
  GET  /api/state             完整状态(需 token)
  POST /api/action/<name>     面板操作(需 token)
  GET  /ws                    WebSocket(需 token; 页面用 mode=player 声明自己负责放音频)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from astrbot.api import logger

ROLE_UNKNOWN = "unknown"
ROLE_PLAYER = "player"
ROLE_DISPLAY = "display"


@dataclass
class OverlaySettings:
    host: str = "0.0.0.0"
    port: int = 60716
    public_host: str = "127.0.0.1"
    token: str = "test12345"
    base_path: str = "/bili-live-music"
    font_path: str = ""
    layout_default: str = "standard"

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "OverlaySettings":
        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        base = str(cfg.get("overlay_base_path") or "/bili-live-music").strip()
        if not base.startswith("/"):
            base = "/" + base
        layout = str(cfg.get("overlay_layout") or "standard").strip().lower()
        return cls(
            host=str(cfg.get("obs_server_host") or "0.0.0.0"),
            port=_int("obs_server_port", 60716, 1024, 65535),
            public_host=str(cfg.get("obs_public_host") or "127.0.0.1"),
            token=str(cfg.get("obs_access_token") or "test12345"),
            base_path=base.rstrip("/") or "/bili-live-music",
            font_path=str(cfg.get("overlay_font_path") or ""),
            layout_default=layout if layout in ("mini", "standard", "sidebar") else "standard",
        )


class OverlayClient:
    """一个 WS 连接(播放器页或展示页)"""

    def __init__(self, ws: Any):
        self.ws = ws
        self.role = ROLE_UNKNOWN
        self.connected_at = time.time()

    async def send(self, message: Dict[str, Any]) -> None:
        try:
            await self.ws.send_str(json.dumps(message, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Overlay 推送失败: {e}")


class OverlayHub:
    """WS 连接集合 + 广播(同步接口, 内部异步发)"""

    def __init__(self) -> None:
        self.clients: Set[OverlayClient] = set()
        self._tasks: Set[asyncio.Task] = set()

    def register(self, ws: Any) -> OverlayClient:
        client = OverlayClient(ws)
        self.clients.add(client)
        logger.info(f"Overlay 页面已连接(共 {len(self.clients)} 个)")
        return client

    def unregister(self, client: OverlayClient) -> None:
        self.clients.discard(client)
        logger.info(f"Overlay 页面已断开(剩 {len(self.clients)} 个)")

    @property
    def has_player(self) -> bool:
        return any(c.role == ROLE_PLAYER for c in self.clients)

    def player_count(self) -> int:
        return sum(1 for c in self.clients if c.role == ROLE_PLAYER)

    def broadcast(self, message: Dict[str, Any]) -> None:
        if not self.clients:
            return
        for client in list(self.clients):
            self._spawn(client.send(message))

    def send_to_role(self, role: str, message: Dict[str, Any]) -> None:
        for client in list(self.clients):
            if client.role == role:
                self._spawn(client.send(message))

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:  # 没有事件循环(理论上不会)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close_all(self) -> None:
        for client in list(self.clients):
            try:
                await client.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()


class OverlayServer:
    """aiohttp 服务(Overlay 页面 + 面板 + WS)"""

    def __init__(
        self,
        hub: OverlayHub,
        settings: OverlaySettings,
        panel: Any,
        page_dir: Optional[Path] = None,
    ):
        self.hub = hub
        self.settings = settings
        self.panel = panel
        self.page_dir = Path(page_dir) if page_dir else (Path(__file__).resolve().parent.parent / "pages")
        self._runner: Any = None
        self._site: Any = None
        self._app: Any = None
        self.bound_port: int = 0

    # ---------------- 生命周期 ----------------

    async def start(self) -> int:
        from aiohttp import web

        self._app = web.Application()
        base = self.settings.base_path
        app = self._app
        app.router.add_get("/", self._handle_index)
        app.router.add_get(base, self._handle_panel)
        app.router.add_get(f"{base}/", self._handle_panel)
        app.router.add_get(f"{base}/overlay", self._handle_overlay)
        app.router.add_get(f"{base}/font.ttf", self._handle_font)
        app.router.add_get(f"{base}/api/health", self._handle_health)
        app.router.add_get(f"{base}/api/state", self._handle_state)
        app.router.add_post(f"{base}/api/action/{{name}}", self._handle_action)
        app.router.add_get(f"{base}/ws", self._handle_ws)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.host, self.settings.port)
        await self._site.start()
        # 端口可能配 0(测试用), 从 sockets 里取真实端口
        try:
            sockets = self._site._server.sockets  # type: ignore[union-attr]
            if sockets:
                self.bound_port = int(sockets[0].getsockname()[1])
        except Exception:  # noqa: BLE001
            self.bound_port = self.settings.port
        logger.info(
            f"Overlay 服务已启动: http://{self.settings.host}:{self.bound_port}{self.settings.base_path}/"
            f"overlay?token=******"
        )
        return self.bound_port

    async def stop(self) -> None:
        try:
            await self.hub.close_all()
        finally:
            if self._runner is not None:
                await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None

    # ---------------- 工具 ----------------

    def base_url(self) -> str:
        port = self.bound_port or self.settings.port
        if port == 80:
            return f"http://{self.settings.public_host}{self.settings.base_path}"
        return f"http://{self.settings.public_host}:{port}{self.settings.base_path}"

    def overlay_url(self, mode: str = "player", layout: Optional[str] = None) -> str:
        layout = layout or self.settings.layout_default
        return f"{self.base_url()}/overlay?token={self.settings.token}&mode={mode}&layout={layout}"

    def panel_url(self) -> str:
        return f"{self.base_url()}/?token={self.settings.token}"

    def font_url(self) -> str:
        return f"{self.base_url()}/font.ttf"

    def _authorized(self, request: Any) -> bool:
        token = self.settings.token
        if not token:
            return True
        supplied = request.query.get("token") or request.headers.get("X-Token") or ""
        return supplied == token

    def _read_page(self, name: str) -> str:
        path = self.page_dir / name
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"<h1>缺少页面文件: {name}</h1>"

    # ---------------- 页面 ----------------

    async def _handle_index(self, request: Any):
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=401, text="Unauthorized: invalid token")
        html = self._read_page("panel.html").replace("__BASE_PATH__", self.settings.base_path)
        return web.Response(
            text=html, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def _handle_panel(self, request: Any):
        return await self._handle_index(request)

    async def _handle_overlay(self, request: Any):
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=401, text="Unauthorized: invalid OBS access token")
        mode = request.query.get("mode", "player").lower()
        if mode not in ("player", "display"):
            mode = "player"
        layout = request.query.get("layout", self.settings.layout_default).lower()
        if layout not in ("mini", "standard", "sidebar"):
            layout = self.settings.layout_default
        html = self._read_page("overlay.html")
        html = html.replace("__FONT_URL__", self.font_url())
        html = html.replace("__MODE__", mode).replace("__LAYOUT__", layout)
        html = html.replace("__TOKEN__", json.dumps(self.settings.token))
        html = html.replace("__WS_PATH__", f"{self.settings.base_path}/ws")
        return web.Response(
            text=html, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def _handle_font(self, request: Any):
        from aiohttp import web

        path = Path(self.settings.font_path) if self.settings.font_path else None
        if path is None or not path.is_file():
            return web.Response(status=404, text="Font unavailable")
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def _handle_health(self, request: Any):
        from aiohttp import web

        return web.json_response({"ok": True, "clients": len(self.hub.clients), "players": self.hub.player_count()})

    # ---------------- API ----------------

    async def _handle_state(self, request: Any):
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=401, text="Unauthorized")
        return web.json_response(self.panel.snapshot())

    async def _handle_action(self, request: Any):
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=401, text="Unauthorized")
        name = request.match_info.get("name", "")
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            result = await self.panel.action(name, payload if isinstance(payload, dict) else {})
        except Exception as e:  # noqa: BLE001
            logger.error(f"面板操作 {name} 失败: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "result": result})

    # ---------------- WebSocket ----------------

    async def _handle_ws(self, request: Any):
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=401, text="Unauthorized")
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)
        client = self.hub.register(ws)
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data or "{}")
                except Exception:  # noqa: BLE001
                    continue
                kind = str(data.get("type") or "")
                if kind == "claim":
                    if client.role != ROLE_PLAYER and self.hub.has_player:
                        # 已经有播放器页在放音频: 后来者降级为展示页, 避免两路声音叠加
                        logger.warning("已有播放器页在播放, 新页面自动降级为展示(display)")
                        await client.send({"type": "role", "role": ROLE_DISPLAY, "backend": "browser"})
                    else:
                        client.role = ROLE_PLAYER
                        logger.info("Overlay 页面声明为播放器(player)")
                        await self._notify_role()
                elif kind == "ready":
                    if client.role == ROLE_UNKNOWN:
                        client.role = ROLE_DISPLAY
                    await self._notify_role()
                else:
                    self.panel.on_client_message(client.role, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug(f"WS 处理异常: {e}")
        finally:
            self.hub.unregister(client)
            await self._notify_role()
        return ws

    async def _notify_role(self) -> None:
        try:
            self.panel.on_client_connected("player" if self.hub.has_player else "display")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"通知页面连接状态失败: {e}")
