"""Web 控制台服务器外壳。

由旧 core/web_admin_server.py 的服务器启动/停止/端口逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ...utils.version_utils import get_plugin_version
from .api_routes import ApiRoutes
from .realtime_channel import RealtimeChannel
from .state_builder import StateBuilder
from .token_auth import TokenAuth

# 注意：这些名称必须位于模块全局命名空间。
# 由于启用了 from __future__ import annotations，FastAPI 会以模块全局命名空间
# 解析处理函数的类型注解；若把 WebSocket / Request 等放在函数内部 import，
# 注解将无法解析，导致 /ws 被误当作普通 query 参数校验并返回 1008 拒绝。
try:
    import uvicorn
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    Request = None  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    logger.warning(
        "[主动消息] FastAPI 未安装喵，Web 管理端不可用喵。请安装: pip install fastapi uvicorn"
    )


_PORT_FALLBACK_MAX_ATTEMPTS = 20


def is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker / 容器环境中。"""
    if os.path.exists("/.dockerenv"):
        return True

    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
            if "/docker/" in content or "/kubepods/" in content:
                return True
    except Exception:
        pass

    return os.environ.get("DOCKER_CONTAINER") == "true"


class WebConsoleServer:
    """主动消息插件 Web 控制台服务器。

    负责 FastAPI 应用的创建、路由/静态资源挂载、鉴权中间件，
    以及 uvicorn 服务的启动、端口探测与停止。
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.config = plugin.config
        self.app: FastAPI | None = None
        self.server = None
        self.server_task: asyncio.Task | None = None
        # 实时推送通道与鉴权组件。
        self.realtime = RealtimeChannel(plugin)
        # 令牌鉴权：以回调动态读取当前密码配置，改密码后无需重启即生效。
        self.token_auth = TokenAuth(
            lambda: bool(self.config.get("web_admin", {}).get("password", ""))
        )
        # 状态载荷构建器，连接计数实时取自推送通道。
        self.state_builder = StateBuilder(
            plugin,
            get_plugin_version(default="未知版本"),
            connection_counter=lambda: self.realtime.connection_count,
        )
        # 记录 Web 端可用性及初始化失败原因。
        self._web_admin_available = False
        self._web_admin_init_error: str | None = None
        # 状态广播合并窗口：群聊消息连发时避免重复构建全量载荷。
        self._broadcast_task: asyncio.Task | None = None
        self._pending_broadcast_reason = ""
        # 构建/发送阶段到达的新请求置位，flush 收尾后据此补跑一轮。
        self._broadcast_dirty = False

        if FASTAPI_AVAILABLE:
            try:
                self._setup_app()
                self._web_admin_available = self.app is not None
            except Exception as e:
                self.app = None
                self._web_admin_available = False
                self._web_admin_init_error = str(e)
                logger.error(
                    "[主动消息] Web 管理端初始化失败喵，已自动禁用，不影响插件主体功能。"
                    f" 可能是 FastAPI / Pydantic 依赖版本不兼容: {e}"
                )

    def _setup_app(self) -> None:
        """创建 FastAPI 应用并装配中间件、路由与静态资源。"""
        self.app = FastAPI(
            title="主动消息管理端",
            description="主动消息插件独立 WebUI",
        )

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:4100",
                "http://127.0.0.1:4100",
                "http://localhost",
                "http://127.0.0.1",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # HTTP 鉴权中间件：启用鉴权后，除登录与鉴权信息接口外，
        # 其余 /api 前缀请求都必须携带有效 Bearer 令牌。
        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if not self.token_auth.enabled:
                return await self._with_static_cache_policy(request, call_next)

            path = request.url.path
            # 登录相关接口与静态资源无需令牌。
            if path in {"/api/login", "/api/auth-info"}:
                return await call_next(request)

            if not path.startswith("/api"):
                return await self._with_static_cache_policy(request, call_next)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse({"error": "未授权"}, status_code=401)

            token = auth_header[7:]
            if not self.token_auth.verify_token(token):
                return JSONResponse({"error": "登录已过期"}, status_code=401)

            return await call_next(request)

        routes = ApiRoutes(
            self.plugin,
            self.state_builder,
            self.realtime,
            self.token_auth,
            self.config,
            is_running_in_docker,
        )
        routes.register(self.app)
        self._mount_static_files()

    async def _with_static_cache_policy(self, request: Request, call_next):
        """给静态响应按类型附加缓存策略。

        入口 index.html 与脚本 / 样式都必须每次重新验证，否则浏览器会按启发式
        新鲜期直接复用本地副本（约文件年龄的 10%），插件升级后就可能出现
        index.html 已更新、js/css 仍是旧版的新旧混用。no-cache 只是强制验证，
        文件未变更时依旧由 ETag + Last-Modified 返回 304，性能开销可忽略。
        字体 / 图片体积大且很少变化，继续沿用启发式缓存。
        """
        response = await call_next(request)

        path = request.url.path.lower()
        if path in {"", "/"} or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"

        return response

    def _mount_static_files(self) -> None:
        """把前端构建产物目录挂载为根路径静态资源。"""
        if not self.app:
            return

        # console 位于 core 下两级，故取 parents[2] 作为插件根目录。
        admin_dir = Path(__file__).resolve().parents[2] / "admin"
        if admin_dir.exists():
            self.app.mount(
                "/", StaticFiles(directory=str(admin_dir), html=True), name="admin"
            )
        else:
            logger.warning(f"[主动消息] 未找到管理端静态目录喵: {admin_dir}")

    # ------------------------------------------------------------------
    # 广播（供通知中心、路由与消息事件复用）
    # ------------------------------------------------------------------
    async def _broadcast_ws_payload(self, payload: dict[str, Any]) -> None:
        """通过实时通道向所有连接广播任意载荷。"""
        await self.realtime.broadcast_ws_payload(payload)

    async def _broadcast_update(self, reason: str) -> None:
        """广播一份全量状态刷新载荷（无连接时直接跳过）。

        新增合并窗口：群聊消息可能在短时间内连发，逐条构建全量载荷会
        同时拖慢事件处理与前端刷新，这里把 300ms 内的多次请求合并为一次。

        注意构建阶段同样属于“任务未完成”，此期间到达的请求不能直接丢弃，
        否则计时器变化既进不了当前载荷也不会触发下一轮，前端会一直显示旧状态。
        """
        if not self.realtime.connections:
            return

        self._pending_broadcast_reason = reason
        # 已有等待中的合并任务时打脏标记：本次状态变化必须由下一轮广播补上。
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_dirty = True
            return

        self._broadcast_task = asyncio.create_task(self._flush_broadcast_update())
        # 任务结束时清理引用，避免持有已完成的 Task 造成对象无法回收。
        self._broadcast_task.add_done_callback(self._clear_broadcast_task)

    def _clear_broadcast_task(self, task: Any) -> None:
        """广播任务收尾：清理引用并吞掉异常，防止 asyncio 打未检索告警。"""
        if self._broadcast_task is task:
            self._broadcast_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[主动消息] 状态广播失败喵: {e}")

    async def _flush_broadcast_update(self) -> None:
        """等待合并窗口后构建并广播全量状态，必要时补跑一轮。"""
        while True:
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                return

            if not self.realtime.connections:
                self._broadcast_dirty = False
                return

            reason = self._pending_broadcast_reason or "update"
            # 构建开始前先清标记：构建期间的新请求会重新置位，触发下一轮。
            self._broadcast_dirty = False

            status = await self.state_builder.build_status_payload()
            jobs = await self.state_builder.collect_jobs_async()

            payload = {
                "type": "update",
                "reason": reason,
                "data": {
                    "status": status,
                    "jobs": jobs,
                    "sessions": self.state_builder.list_known_session_summaries(),
                    "notifications": await self.state_builder.build_notification_payload(),
                },
            }
            await self._broadcast_ws_payload(payload)

            # 构建或发送期间又有状态变化时补跑一轮，避免这一轮载荷落后于真实状态。
            if not self._broadcast_dirty:
                return

    async def _broadcast_notification_meta_update(self, reason: str) -> None:
        """只广播通知元信息（未读数等），用于轻量刷新。"""
        if not self.realtime.connections:
            return

        # 通知中心不可用时返回全零元信息。
        if not getattr(self.plugin, "notification_center", None):
            notification_meta = {
                "unread_count": 0,
                "last_sync_at": None,
                "total_count": 0,
            }
        else:
            notification_meta = await self.plugin.notification_center.get_meta()
        payload = {
            "type": "update",
            "reason": reason,
            "data": {
                "notificationsMeta": notification_meta,
            },
        }
        await self._broadcast_ws_payload(payload)

    # ------------------------------------------------------------------
    # 端口探测
    # ------------------------------------------------------------------
    def _is_port_available(self, host: str, port: int) -> bool:
        """尝试绑定端口以探测其是否可用（根据地址选择 IPv4/IPv6）。"""
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "posix":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _resolve_bind_port(
        self,
        host: str,
        port: int,
        *,
        allow_fallback: bool,
    ) -> int | None:
        """解析最终绑定端口；占用时按需向后探测备用端口。

        返回 None 表示无可用端口（容器环境下不自动回退）。
        """
        if self._is_port_available(host, port):
            return port

        logger.warning(f"[主动消息] Web 管理端端口 {host}:{port} 已被占用喵。")
        if not allow_fallback:
            return None

        for offset in range(1, _PORT_FALLBACK_MAX_ATTEMPTS + 1):
            candidate = port + offset
            if candidate > 65535:
                break
            if self._is_port_available(host, candidate):
                logger.warning(f"[主动消息] 已自动改用备用端口 {host}:{candidate} 喵。")
                return candidate
        return None

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 Web 控制台服务；任何失败都只影响 Web 端，不影响插件主体。"""
        if not FASTAPI_AVAILABLE:
            logger.error("[主动消息] 无法启动 Web 管理端喵: FastAPI 未安装")
            return

        if not self._web_admin_available or not self.app:
            detail = (
                f" 初始化失败原因: {self._web_admin_init_error}"
                if self._web_admin_init_error
                else ""
            )
            logger.error(
                "[主动消息] 无法启动 Web 管理端喵: 初始化未完成或依赖不兼容，已自动禁用。"
                f"{detail}"
            )
            return

        web_admin = self.config.get("web_admin", {})
        if not web_admin.get("enabled", False):
            logger.info("[主动消息] Web 管理端未启用喵。")
            return

        host = web_admin.get("host", "127.0.0.1")
        configured_port = int(web_admin.get("port", 4100))

        in_docker = is_running_in_docker()
        port = self._resolve_bind_port(
            host,
            configured_port,
            allow_fallback=not in_docker,
        )
        if port is None:
            reason = (
                "容器环境下为避免端口映射失效不会自动改用备用端口，"
                if in_docker
                else f"已向后探测 {_PORT_FALLBACK_MAX_ATTEMPTS} 个端口仍不可用，"
            )
            logger.error(
                f"[主动消息] Web 管理端启动失败喵: 端口 {host}:{configured_port} "
                f"不可用；{reason}"
                "本次仅 Web 管理端不可用，主动消息插件主体功能不受影响；"
                "请修改 web_admin.port，或释放该端口后重试。"
            )
            return

        uv_cfg = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uv_cfg)

        async def _serve():
            # 运行 uvicorn 服务；取消异常需向上抛出以便优雅停止。
            try:
                await self.server.serve()
            except asyncio.CancelledError:
                raise
            except (SystemExit, Exception) as e:
                logger.exception(f"[主动消息] Web 管理端运行异常喵: {e!r}")

        self.server_task = asyncio.create_task(_serve())

        self.token_auth.start_cleanup()

        for _ in range(20):
            if self.server.started or self.server_task.done():
                break
            await asyncio.sleep(0.05)

        # 等待最多 1 秒确认服务状态，区分“已启动/启动失败/仍在启动”。
        if self.server.started:
            logger.info(f"[主动消息] Web 管理端已启动喵: http://{host}:{port}")
        elif self.server_task.done():
            logger.error(
                f"[主动消息] Web 管理端启动失败喵: 端口 {host}:{port} 绑定未成功"
                "（可能被其他程序抢占）。本次仅 Web 管理端不可用，"
                "主动消息插件主体功能不受影响。"
            )
        else:
            logger.warning(
                f"[主动消息] Web 管理端仍在启动中喵: http://{host}:{port} "
                "（等待超时，稍后可能自行就绪）。"
            )

    async def stop(self) -> None:
        """停止 Web 控制台服务并释放连接与资源。"""
        await self.token_auth.stop_cleanup()
        if self.server:
            self.server.should_exit = True
            self.server.force_exit = True

        if self.server_task:
            task = self.server_task
            self.server_task = None
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning(
                    "[主动消息] Web 管理端未在 5 秒内停止喵，正在强制取消以释放端口。"
                )
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as e:
                logger.warning(f"[主动消息] 停止 Web 管理端时出现异常喵: {e!r}")

        self.realtime.clear()
        logger.info("[主动消息] Web 管理端已停止喵。")
