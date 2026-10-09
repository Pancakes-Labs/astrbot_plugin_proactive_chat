"""插件入口与装配容器。

主类只负责：装配各服务、注册 AstrBot 事件入口、承载容器级运行时状态。
具体业务逻辑分布在 services / pipeline / features 中，调用方直接使用对应服务。
"""

from __future__ import annotations

import asyncio
import re
import time

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.adapters.event_handlers import EventHandlers
from .core.console.runtime_server import WebConsoleServer
from .core.domain.domain_enums import TriggerSource
from .core.pipeline.flow_orchestrator import FlowOrchestrator
from .core.remote.notification_center import NotificationCenter
from .core.remote.telemetry_manager import TelemetryManager
from .core.services.config_service import ConfigService
from .core.services.context_service import ContextService
from .core.services.lifecycle_service import LifecycleService
from .core.services.llm_service import LlmService
from .core.services.scheduler_service import SchedulerService
from .core.services.sender_service import SenderService
from .core.services.session_service import SessionService
from .core.services.storage_service import StorageService
from .core.store.override_store import OverrideStore
from .utils.version_utils import get_plugin_version


class ProactiveChatPlugin(star.Star):
    """插件的主类：装配容器 + 事件入口 + 容器级状态。"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        # 注入的配置对象（由 AstrBot 框架提供）
        self.config: AstrBotConfig = config
        # 调度器与时区会在 initialize 中初始化
        self.scheduler = None
        self.timezone = None

        # 使用 StarTools 获取插件专属数据目录（Path 对象）
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"

        # 共享锁与持久化数据容器（data_lock 在 initialize 中初始化）
        self.data_lock = None
        self.session_data: dict = {}
        # 记录当前正在执行“立即触发”的会话，防止重复点击导致并发主动消息。
        self.manual_trigger_sessions: set[str] = set()

        self.version = get_plugin_version()

        # ------------------------------------------------------------------
        # 装配各服务（组合替代继承）
        # ------------------------------------------------------------------
        self.session_service = SessionService(self)
        self.storage_service = StorageService(self)
        self.config_service = ConfigService(self)
        self.scheduler_service = SchedulerService(self)
        self.context_service = ContextService(self)
        self.llm_service = LlmService(self)
        self.sender_service = SenderService(self)
        self.lifecycle_service = LifecycleService(self)
        self.event_handlers = EventHandlers(self)
        self.orchestrator = FlowOrchestrator(self)

        # 会话差异配置管理器
        self.session_override_manager = OverrideStore(self.data_dir)

        # 通知中心与 Web 控制台
        self.notification_center = NotificationCenter(self)
        try:
            self.web_admin_server = WebConsoleServer(self)
        except Exception as e:
            self.web_admin_server = None
            logger.error(f"[主动消息] Web 管理端组件创建失败喵，已自动禁用: {e}")

        # 遥测管理器在插件实例创建阶段即初始化。
        self.telemetry = TelemetryManager(
            config=dict(self.config),
            plugin_version=self.version,
        )
        self._telemetry_tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._start_time: float = 0.0
        self._original_exception_handler = None
        self._exception_handler_installed = False
        self._terminating = False

        # 平台就绪后的启动流程状态（延迟初始化，先占位）
        self._startup_finalized: bool = False
        self._startup_lock: asyncio.Lock | None = None
        self._startup_task: asyncio.Task[None] | None = None

        # 群聊沉默倒计时与自动触发计时器
        self.group_timers: dict[str, asyncio.TimerHandle] = {}
        self.last_bot_message_time = 0
        self.session_temp_state: dict[str, dict] = {}
        self.last_message_times: dict[str, float] = {}
        self.auto_trigger_timers: dict[str, asyncio.TimerHandle] = {}
        self.plugin_start_time = time.time()
        self.first_message_logged: set[str] = set()
        self._cleanup_counter = 0

        logger.info("[主动消息] 插件实例已创建喵。")

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def initialize(self) -> None:
        """插件的异步初始化函数。"""
        await self.lifecycle_service.initialize()

    async def terminate(self) -> None:
        """插件终止入口。"""
        await self.lifecycle_service.terminate()

    # ==================================================================
    # 遥测基础设施（供各服务通过容器复用）
    # ==================================================================
    def _track_task(self, task: asyncio.Task[None] | None) -> asyncio.Task[None] | None:
        """登记遥测任务引用，避免任务过早释放。"""
        if task is None:
            return None
        self._telemetry_tasks.add(task)
        task.add_done_callback(self._telemetry_tasks.discard)
        return task

    async def _cleanup_telemetry_tasks(self) -> None:
        """清理所有未完成的遥测任务。"""
        if not self._telemetry_tasks:
            return

        pending_tasks = list(self._telemetry_tasks)
        for task in pending_tasks:
            if not task.done():
                task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._telemetry_tasks.clear()

    async def _deferred_startup_telemetry(self) -> None:
        """错开上报 startup 与 config 事件，避免并发请求触发服务端限流。"""
        try:
            await self.telemetry.track_startup()
            await asyncio.sleep(2)
            await self.telemetry.track_config(dict(self.config))
        except Exception as e:
            logger.debug(f"[主动消息] 启动遥测上报失败喵: {e}")

    async def _heartbeat_loop(self) -> None:
        """遥测心跳循环（默认每 12 小时上报一次）。"""
        heartbeat_interval = 43200
        try:
            while True:
                if not self.telemetry or not self.telemetry.enabled:
                    await asyncio.sleep(heartbeat_interval)
                    continue

                uptime = time.monotonic() - self._start_time
                try:
                    await self.telemetry.track_heartbeat(uptime_seconds=uptime)
                except Exception as e:
                    logger.debug(f"[主动消息] 遥测心跳发送失败喵: {e}")

                await asyncio.sleep(heartbeat_interval)
        except asyncio.CancelledError:
            logger.debug("[主动消息] 遥测心跳任务已取消喵。")
            raise
        except Exception as e:
            logger.error(f"[主动消息] 遥测心跳循环异常喵: {e}")

    def _handle_asyncio_exception(self, loop, context) -> None:
        """全局 asyncio 异常处理器，仅处理当前插件相关异常。

        通过遍历异常调用栈的文件名判断归属；非本插件的异常交还原处理器。
        """
        exception = context.get("exception")
        message = context.get("message", "未知异常")

        # 判断异常调用栈中是否出现本插件目录名。
        is_plugin_exception = False
        if exception:
            tb = exception.__traceback__
            while tb is not None:
                filename = tb.tb_frame.f_code.co_filename
                if "astrbot_plugin_proactive_chat" in filename:
                    is_plugin_exception = True
                    break
                tb = tb.tb_next

        if not is_plugin_exception:
            if self._original_exception_handler:
                self._original_exception_handler(loop, context)
            else:
                loop.default_exception_handler(context)
            return

        if exception:
            logger.error(f"[主动消息] 捕获未处理的异步异常喵: {exception}")
            logger.error(f"[主动消息] 异常上下文喵: {message}")
        else:
            logger.error(f"[主动消息] 捕获未处理的异步错误喵: {message}")

        # 遥测启用时上报该未处理异常，并附带尽量精确的任务名。
        if self.telemetry and self.telemetry.enabled:
            task_name = "unknown"
            future = context.get("future")
            if future:
                task_name = getattr(future, "get_name", lambda: str(future))()
                if not task_name or task_name == str(future):
                    future_repr = repr(future)
                    match = re.search(r"name='([^']+)'", future_repr)
                    if match:
                        task_name = match.group(1)

            error = exception or RuntimeError(message)
            self._track_task(
                asyncio.create_task(
                    self.telemetry.track_error(
                        error,
                        module=f"main.unhandled_async.{task_name}",
                    )
                )
            )

    # ==================================================================
    # 事件入口
    # ==================================================================
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot 加载完成钩子：平台已加载，恢复持久化定时任务。"""
        await self.lifecycle_service.on_astrbot_loaded()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_friend_message(self, event: AstrMessageEvent) -> None:
        """私聊消息入口。"""
        await self.event_handlers.on_friend_message(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊消息入口。"""
        await self.event_handlers.on_group_message(event)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后入口。"""
        await self.event_handlers.on_after_message_sent(event)

    # ==================================================================
    # 主动消息主流程
    # ==================================================================
    async def check_and_chat(self, session_id: str) -> None:
        """由定时任务触发的核心函数，完成一次完整的主动消息流程。"""
        await self.orchestrator.run(session_id, TriggerSource.SCHEDULE)
