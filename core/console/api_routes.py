"""Web 控制台路由注册。

由旧 core/web_admin_server.py 的路由逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..domain.config_keys import (
    ENABLED_KEY,
    NOTIFICATION_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
    TELEMETRY_CONFIG_KEY,
    WEB_ADMIN_KEY,
)
from ..domain.domain_enums import TriggerSource

# 注意：WebSocket / FileResponse / JSONResponse 必须在模块全局命名空间可用。
try:  # pragma: no cover - 取决于是否安装 FastAPI
    from fastapi import BackgroundTasks, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
except ImportError:  # pragma: no cover
    BackgroundTasks = None  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]

# 写操作接口统一要求带一个跨站页面无法伪造的请求头：带自定义头的请求必然
# 触发 CORS 预检，而预检不会放行未列出的来源，因此即使未设置 WebUI 密码，
# 第三方网页也无法用表单提交改动插件状态或重启进程。
# 该校验的入口在 runtime_server 的 HTTP 中间件，这里定义常量供两处共用。
OPS_HEADER_NAME = "x-requested-with"
OPS_HEADER_VALUE = "AstrBotProactiveChatConsole"


class ApiRoutes:
    """注册 Web 控制台 HTTP 与 WebSocket 路由。

    路由按功能分为五组：鉴权、状态与任务、会话差异配置、通知系统、
    静态资源与文档。所有处理器都复用容器的服务能力，本类不持有业务状态。
    """

    def __init__(
        self,
        plugin: Any,
        state_builder: Any,
        realtime: Any,
        token_auth: Any,
        config: Any,
        is_docker_fn,
    ) -> None:
        # 装配容器，用于访问各业务服务与会话数据。
        self.plugin = plugin
        # 负责构建状态、任务、会话与文档等前端载荷。
        self.state_builder = state_builder
        # 管理 WebSocket 连接，用于推送刷新。
        self.realtime = realtime
        # 登录令牌的签发与校验。
        self.token_auth = token_auth
        # 插件配置对象，支持 save_config 落盘。
        self.config = config
        # 注入 Docker 探测函数，便于测试替换。
        self._is_running_in_docker = is_docker_fn

    def _sync_telemetry_config(self) -> None:
        """把内存中的最新配置同步给遥测管理器。

        TelemetryManager 在实例化时缓存了 enabled 开关，配置保存后不同步的话，
        Web 面板里关闭遥测会看起来成功但事件仍在继续上报。
        """
        telemetry = getattr(self.plugin, "telemetry", None)
        apply_config = getattr(telemetry, "apply_config", None)
        if not callable(apply_config):
            return
        try:
            apply_config(dict(self.config))
        except Exception as e:
            logger.debug(f"[主动消息] 同步遥测开关失败喵: {e}")

    def register(self, app) -> None:
        """把所有路由挂载到 FastAPI 应用上。

        plugin 与 state 在闭包中复用，避免每个处理器重复取属性。
        """
        plugin = self.plugin
        state = self.state_builder

        async def _broadcast_web_update(reason: str) -> None:
            """向 Web 控制台广播全量刷新（不可用时静默跳过）。"""
            web = getattr(plugin, "web_admin_server", None)
            if web:
                await web._broadcast_update(reason)

        # ------------------------------------------------------------------
        # 鉴权与登录
        # ------------------------------------------------------------------
        # 前端据此判断是否需要弹出登录页。
        @app.get("/api/auth-info")
        async def auth_info():
            return {"auth_required": self.token_auth.enabled}

        # 校验密码并签发登录令牌；未配置密码时直接放行。
        @app.post("/api/login")
        async def login(credentials: dict[str, Any]):
            password = self.config.get("web_admin", {}).get("password", "")
            if not password:
                # 未设置密码：返回固定令牌，前端不再校验。
                return {"token": "no-auth", "auth_required": False}

            input_password = str(credentials.get("password", ""))
            if not secrets.compare_digest(input_password, password):
                return JSONResponse({"error": "密码错误"}, status_code=401)

            token = self.token_auth.issue_token()
            return {"token": token, "auth_required": True}

        # 插件 logo 静态资源。
        @app.get("/logo.png")
        async def get_logo():
            # console 位于 core 下两级，故取 parents[2] 作为插件根目录。
            logo_path = Path(__file__).resolve().parents[2] / "logo.png"
            if logo_path.exists():
                return FileResponse(str(logo_path), media_type="image/png")
            return JSONResponse({"error": "logo not found"}, status_code=404)

        # 运行状态总览（含计时器卡片与连接数）。
        @app.get("/api/status")
        async def get_status():
            return await state.build_status_payload()

        # 可浏览的 Markdown 文档列表。
        @app.get("/api/markdown-files")
        async def list_markdown_files():
            return {"items": state.list_markdown_documents()}

        # 读取单个 Markdown 文档内容（仅允许白名单目录）。
        @app.get("/api/markdown-files/{file_path:path}")
        async def get_markdown_file(file_path: str):
            resolved = state.resolve_markdown_document(file_path)
            if not resolved:
                return JSONResponse(
                    {"error": "文档不存在或不允许访问"}, status_code=404
                )

            try:
                content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
            except UnicodeDecodeError:
                return JSONResponse(
                    {"error": "文档编码不受支持，仅支持 UTF-8 Markdown 文件"},
                    status_code=400,
                )
            except Exception as e:
                logger.error(f"[主动消息] 读取 Markdown 文档失败喵: {e}")
                return JSONResponse(
                    {"error": "读取文档失败", "message": str(e)}, status_code=500
                )

            return {
                "path": state.to_workspace_relative_path(resolved),
                "title": resolved.stem,
                "content": content,
                "content_format": "markdown",
            }

        def _config_group(key: str) -> dict[str, Any]:
            """读取配置分组并保证返回对象，避免历史脏数据（如 null）让配置页整体不可用。"""
            value = self.config.get(key)
            return dict(value) if isinstance(value, dict) else {}

        # 读取配置（web_admin 分组会剔除 password 字段）。
        @app.get("/api/config")
        async def get_config():
            web_admin = {
                k: v for k, v in _config_group("web_admin").items() if k != "password"
            }
            # admin_users 是顶层列表而非分组，单独处理：它是管理指令的权限边界，
            # 必须在 WebUI 可读可写，否则用户无法在面板里授予插件管理员。
            admin_users = self.config.get("admin_users") or []
            if not isinstance(admin_users, list):
                admin_users = []
            # 返回所有可编辑分组，保证前端渲染的字段与服务端保存范围一致。
            return {
                "admin_users": [str(item) for item in admin_users],
                SESSION_DEFAULTS_KEY: _config_group(SESSION_DEFAULTS_KEY),
                "web_admin": web_admin,
                "notification_settings": _config_group(NOTIFICATION_SETTINGS_KEY),
                "telemetry_config": _config_group(TELEMETRY_CONFIG_KEY),
            }

        # 可用人格列表：直接读 AstrBot 的 persona_manager，与 WebUI 保持同源。
        @app.get("/api/personas")
        async def list_personas():
            personas = await plugin.context_service.list_available_personas()
            return {
                "personas": personas,
                # 供前端在 select_persona 控件中渲染“继承/不使用”两个特殊选项。
                "special_options": [
                    {"value": "", "label": "跟随 AstrBot 当前人格"},
                    {
                        "value": "[%None]",
                        "label": "不使用人格（仅用主动消息提示词）",
                    },
                ],
            }

        # 返回配置 schema，供前端渲染配置表单。
        # 优先使用运行期装配结果：已合并各 Feature 的配置声明，
        # 比磁盘上的静态文件更贴近当前实际生效的配置结构。
        @app.get("/api/config-schema")
        async def get_config_schema():
            runtime_schema = getattr(self.config, "schema", None)
            if isinstance(runtime_schema, dict) and runtime_schema:
                return runtime_schema

            schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
            if schema_path.exists():
                try:
                    schema_text = await asyncio.to_thread(
                        schema_path.read_text, encoding="utf-8"
                    )
                    return json.loads(schema_text)
                except Exception as e:
                    logger.error(f"[主动消息] 读取 Schema 失败喵: {e}")
            return {}

        # 更新配置：接受 Schema 中全部可编辑分组，避免前端保存后字段被静默丢弃。
        @app.post("/api/config")
        async def update_config(payload: dict[str, Any]):
            allowed_keys = {
                SESSION_DEFAULTS_KEY,
                WEB_ADMIN_KEY,
                NOTIFICATION_SETTINGS_KEY,
                TELEMETRY_CONFIG_KEY,
            }
            # 分组必须是对象：若写入 null，后续 GET /api/config 的 dict(...) 会抛错，
            # 配置编辑器将无法重新加载，只能手动改配置文件才能恢复。
            invalid_keys = [
                key
                for key in sorted(allowed_keys)
                if key in payload and not isinstance(payload[key], dict)
            ]
            if invalid_keys:
                return JSONResponse(
                    {
                        "error": "配置分组必须是对象",
                        "invalid_keys": invalid_keys,
                    },
                    status_code=400,
                )

            # admin_users 是顶层列表，单独校验并去空白，避免空串混入权限名单。
            admin_users_payload = None
            if "admin_users" in payload:
                raw_admins = payload.get("admin_users")
                if raw_admins is None:
                    raw_admins = []
                if not isinstance(raw_admins, list):
                    return JSONResponse(
                        {
                            "error": "admin_users 必须是字符串数组",
                            "invalid_keys": ["admin_users"],
                        },
                        status_code=400,
                    )
                if any(not isinstance(item, str) for item in raw_admins):
                    return JSONResponse(
                        {
                            "error": "admin_users 必须是字符串数组",
                            "invalid_keys": ["admin_users"],
                        },
                        status_code=400,
                    )
                admin_users_payload = [
                    item.strip() for item in raw_admins if item.strip()
                ]

            # 遥测开关必须是布尔值。
            telemetry_payload = payload.get("telemetry_config")
            if isinstance(telemetry_payload, dict) and "enabled" in telemetry_payload:
                if not isinstance(telemetry_payload["enabled"], bool):
                    return JSONResponse(
                        {
                            "error": "telemetry_config.enabled 必须是布尔值",
                            "invalid_keys": ["telemetry_config.enabled"],
                        },
                        status_code=400,
                    )

            for key in allowed_keys:
                if key not in payload:
                    continue
                if key == WEB_ADMIN_KEY:
                    # 走 _config_group 保留未提交的既有字段（如密码）。
                    old = _config_group(WEB_ADMIN_KEY)
                    old.update(payload.get(WEB_ADMIN_KEY, {}))
                    if "password" in payload.get(WEB_ADMIN_KEY, {}):
                        old["password"] = payload[WEB_ADMIN_KEY]["password"]
                    self.config[WEB_ADMIN_KEY] = old
                else:
                    self.config[key] = payload[key]

            if admin_users_payload is not None:
                self.config["admin_users"] = admin_users_payload

            # 遥测开关是运行期缓存的，必须在内存配置更新后同步，否则要重启才生效。
            self._sync_telemetry_config()
            # 配置可能改了人格，清掉人格摘要缓存，避免面板短时间仍显示旧值。
            invalidate = getattr(state, "invalidate_persona_cache", None)
            if callable(invalidate):
                invalidate()

            try:
                plugin.config.save_config()
            except Exception as e:
                # 内存已更新但落盘失败：仍广播以保持前端与内存一致，并明确报错。
                logger.error(f"[主动消息] 保存配置失败喵: {e}")
                await _broadcast_web_update("config")
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "配置已应用到内存，但保存到磁盘失败",
                        "message": str(e),
                    },
                    status_code=500,
                )

            await _broadcast_web_update("config")
            return {"ok": True}

        # 会话差异配置：列出已知会话及其生效状态。
        @app.get("/api/session-config/sessions")
        async def list_session_configs():
            sessions = state.list_known_sessions()
            result = []
            for session in sessions:
                override = plugin.session_override_manager.get_override(session)
                effective = plugin.config_service.get_session_config(session)
                session_name = plugin.session_service.get_session_name(
                    session, effective
                )
                result.append(
                    {
                        "session": session,
                        "session_name": session_name,
                        "session_display_name": plugin.session_service.get_session_display_name(
                            session, effective
                        ),
                        "has_override": bool(override),
                        "override_keys": list(override.keys()),
                        "enabled": bool(
                            effective and effective.get(ENABLED_KEY, False)
                        ),
                        "next_trigger_time": plugin.session_data.get(session, {}).get(
                            "next_trigger_time"
                        ),
                        "unanswered_count": plugin.session_data.get(session, {}).get(
                            "unanswered_count", 0
                        ),
                    }
                )
            return {"sessions": result}

        # 读取单个会话的 base/override/effective 三层配置。
        @app.get("/api/session-config/{umo:path}")
        async def get_session_config(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            base = plugin.config_service.get_base_session_config(normalized)
            return {
                "session": normalized,
                "base": base,
                "override": plugin.session_override_manager.get_override(normalized),
                "effective": plugin.config_service.get_session_config(normalized),
            }

        # 预览某会话最终生效的人格与来源，使用独立前缀避免路径贪婪匹配。
        @app.get("/api/persona-preview/{umo:path}")
        async def preview_session_persona(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            # 解析对话人格需要真实对话对象，缺失时按 None 处理即可。
            conversation = None
            try:
                conv_id = (
                    await plugin.context.conversation_manager.get_curr_conversation_id(
                        normalized
                    )
                )
                if conv_id:
                    conversation = (
                        await plugin.context.conversation_manager.get_conversation(
                            normalized, conv_id
                        )
                    )
            except Exception as e:
                logger.debug(f"[主动消息] 预览人格时读取对话失败喵: {e}")

            service = plugin.context_service
            # 预览属于调试路径，高频调用不应反复刷人格遮蔽日志。
            resolution = await service.resolve_persona(
                normalized, conversation, log_shadow=False
            )
            # 各来源原始取值一并返回，便于定位优先级遮蔽问题。
            candidates = {
                "plugin_config": service.get_plugin_persona_id(normalized),
                "profile": service.get_profile_persona_id(normalized),
                "session_rule": await service.get_session_rule_persona_id(normalized),
                "conversation": service.get_conversation_persona_id(conversation),
                "default": await service.get_default_persona_id(normalized),
            }
            return {
                "session": normalized,
                "persona_id": resolution.persona_id,
                "persona_name": resolution.persona_name,
                "persona_source": resolution.source,
                "persona_source_label": resolution.source_label,
                "has_prompt": bool(resolution.system_prompt),
                "disabled": resolution.disabled,
                "candidates": candidates,
            }

        # 写入会话配置：mode=override 直接覆盖差异；否则按 effective 反推差异。
        @app.post("/api/session-config/{umo:path}")
        async def update_session_config(umo: str, payload: dict[str, Any]):
            normalized = plugin.session_service.normalize_session_id(umo)
            mode = payload.get("mode", "effective")

            saved = True
            if mode == "override":
                override = payload.get("override", {})
                if not isinstance(override, dict):
                    return JSONResponse(
                        {"error": "override 必须是对象"}, status_code=400
                    )
                saved = await plugin.session_override_manager.set_override(
                    normalized, override
                )
            else:
                effective = payload.get("effective", {})
                if not isinstance(effective, dict):
                    return JSONResponse(
                        {"error": "effective 必须是对象"}, status_code=400
                    )
                base = plugin.config_service.get_base_session_config(normalized)
                if not base:
                    return JSONResponse(
                        {
                            "error": "会话未命中 friend/group 全局配置，无法保存 effective"
                        },
                        status_code=400,
                    )
                saved = (
                    await plugin.session_override_manager.update_session_from_effective(
                        normalized, base, effective
                    )
                )

            # 会话 override 可能包含 persona_id，必须在广播前失效该会话的
            # 人格缓存，否则状态卡片与任务列表会继续显示旧人格。
            invalidate = getattr(state, "invalidate_persona_cache", None)
            if callable(invalidate):
                invalidate(normalized)
            await _broadcast_web_update("session-config")
            if not saved:
                # 与 /api/config 保持一致：内存已改但磁盘没写成功，必须显式报错。
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "error": "会话配置已应用到内存，但保存到磁盘失败",
                    },
                    status_code=500,
                )
            return {
                "ok": True,
                "session": normalized,
                "override": plugin.session_override_manager.get_override(normalized),
                "effective": plugin.config_service.get_session_config(normalized),
            }

        # 删除会话差异配置，恢复到全局配置。
        @app.delete("/api/session-config/{umo:path}")
        async def reset_session_config(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            saved = await plugin.session_override_manager.delete_override(normalized)
            # 删除覆写同样会改变生效人格，需与写入路径一致地失效缓存。
            invalidate = getattr(state, "invalidate_persona_cache", None)
            if callable(invalidate):
                invalidate(normalized)
            await _broadcast_web_update("session-config")
            if not saved:
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "error": "会话配置已从内存移除，但保存到磁盘失败",
                    },
                    status_code=500,
                )
            return {
                "ok": True,
                "session": normalized,
                "override": {},
                "effective": plugin.config_service.get_session_config(normalized),
            }

        # 调度任务列表。
        @app.get("/api/jobs")
        async def list_jobs():
            return {"jobs": await state.collect_jobs_async()}

        # 手动重新调度下一次主动消息（不重置未回复计数）。
        @app.post("/api/jobs/{umo:path}/reschedule")
        async def reschedule_job(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            session_config = plugin.config_service.get_session_config(normalized)
            if not session_config or not session_config.get(ENABLED_KEY, False):
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "error": "会话未启用或配置不存在，无法重新调度",
                    },
                    status_code=400,
                )

            await plugin.scheduler_service.schedule_next_chat_and_save(
                normalized, reset_counter=False
            )
            await _broadcast_web_update("jobs")
            return {
                "ok": True,
                "session": normalized,
                "message": "已重新调度下一次主动消息时间",
            }

        # 通知列表（含未读标记）。
        @app.get("/api/notifications")
        async def get_notifications():
            return await state.build_notification_payload()

        # 标记单条通知为已读。
        @app.post("/api/notifications/read")
        async def mark_notification_read(payload: dict[str, Any]):
            if not getattr(plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)

            notification_id = payload.get("id")
            if notification_id is None:
                return JSONResponse({"error": "缺少必填字段 id"}, status_code=400)
            try:
                normalized_id = int(notification_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "id 必须是数字"}, status_code=400)

            result = await plugin.notification_center.mark_as_read(normalized_id)
            await _broadcast_web_update("notifications")
            return result

        # 全部标记为已读。
        @app.post("/api/notifications/read-all")
        async def mark_all_notifications_read():
            if not getattr(plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)
            result = await plugin.notification_center.mark_all_as_read()
            await _broadcast_web_update("notifications")
            return result

        # 立即拉取远端通知。
        @app.post("/api/notifications/refresh")
        async def refresh_notifications():
            if not getattr(plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)
            changed = await plugin.notification_center.refresh()
            await _broadcast_web_update("notifications")
            payload = await plugin.notification_center.get_payload()
            return {
                "ok": True,
                "changed": changed,
                "items": payload.get("items", []),
                "meta": payload.get("meta", {}),
            }

        # 在宿主机文件管理器中打开插件目录或数据目录。
        @app.post("/api/open-directory")
        async def open_directory(payload: dict[str, Any]):
            target = str(payload.get("path", "plugin")).strip().lower()
            if target == "data":
                # path=data 时打开插件的数据目录。
                directory = Path(plugin.data_dir)
            else:
                # 默认打开插件代码目录（console 上溯两级）。
                directory = Path(__file__).resolve().parents[2]

            try:
                directory.mkdir(parents=True, exist_ok=True)
                dir_str = str(directory)

                if self._is_running_in_docker():
                    return JSONResponse(
                        {
                            "error": "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径",
                            "path": dir_str,
                        },
                        status_code=400,
                    )

                if os.name == "nt":
                    await asyncio.to_thread(os.startfile, dir_str)
                elif sys.platform == "darwin":
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（macOS）",
                                "message": f"open 命令执行失败: {detail}",
                                "path": dir_str,
                            },
                            status_code=500,
                        )
                else:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["xdg-open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（Linux）",
                                "message": (
                                    "xdg-open 执行失败，服务器可能缺少桌面环境或未安装 xdg-open: "
                                    f"{detail}"
                                ),
                                "path": dir_str,
                            },
                            status_code=500,
                        )

                return {
                    "ok": True,
                    "path": dir_str,
                    "message": "已在系统文件管理器中打开目录",
                }
            except FileNotFoundError as e:
                logger.error(f"[主动消息] 打开目录失败（命令缺失）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：系统缺少所需命令",
                        "message": "请确认系统已安装对应文件管理器命令（如 open / xdg-open）",
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except PermissionError as e:
                logger.error(f"[主动消息] 打开目录失败（权限不足）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：权限不足",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except Exception as e:
                logger.error(f"[主动消息] 打开目录失败喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )

        # ------------------------------------------------------------------
        # 运维快捷操作（等价于 /主动消息重启 与 /重启AstrBot 指令）
        # ------------------------------------------------------------------
        def _reject_cross_site(request: Any) -> Any:
            """校验运维请求是否来自控制台页面；通过返回 None。

            WebUI 未设置密码时中间件完全放行，此时跨站表单可以直接 POST
            到本机端口。要求自定义头即可挡住这类无法预检的简单请求。
            """
            if request.headers.get(OPS_HEADER_NAME, "") != OPS_HEADER_VALUE:
                return JSONResponse(
                    {"error": "请求来源不受信任，请从管理端页面操作"},
                    status_code=403,
                )
            return None

        @app.post("/api/plugin/reload")
        async def reload_plugin(background_tasks: BackgroundTasks, request: Request):
            """重载本插件。

            BackgroundTasks 保证响应发送完成后才执行重载，避免销毁旧实例
            与响应返回之间的竞态。
            """
            rejected = _reject_cross_site(request)
            if rejected is not None:
                return rejected

            service = getattr(plugin, "command_service", None)
            if service is None:
                return JSONResponse({"error": "指令服务未就绪"}, status_code=503)

            try:
                message = service.register_reload_task(background_tasks)
            except Exception as e:
                logger.error(f"[主动消息] Web 端重载插件失败喵: {e}")
                return JSONResponse(
                    {"error": "重载插件失败", "message": str(e)}, status_code=500
                )

            return {"ok": True, "message": message}

        @app.post("/api/astrbot/restart")
        async def restart_astrbot(request: Request):
            """重启 AstrBot 进程，返回派发结果。"""
            rejected = _reject_cross_site(request)
            if rejected is not None:
                return rejected

            service = getattr(plugin, "command_service", None)
            if service is None:
                return JSONResponse({"error": "指令服务未就绪"}, status_code=503)

            try:
                ok, message, status_code = await service.web_restart_astrbot()
            except Exception as e:
                logger.error(f"[主动消息] Web 端重启 AstrBot 失败喵: {e}")
                return JSONResponse(
                    {"error": "重启 AstrBot 失败", "message": str(e)}, status_code=500
                )

            if not ok:
                # 409 表示已有重启任务在跑（幂等跳过），不是真的失败。
                return JSONResponse(
                    {"ok": False, "error": message, "in_progress": status_code == 409},
                    status_code=status_code,
                )
            return {"ok": True, "message": message}

        # 立即触发一次主动消息（异步执行，占用期间拒绝重复触发）。
        @app.post("/api/jobs/{umo:path}/trigger")
        async def trigger_job(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            if normalized in plugin.manual_trigger_sessions:
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "in_progress": True,
                        "message": "该任务正在立即触发中，请等待当前执行完成",
                    },
                    status_code=409,
                )

            plugin.manual_trigger_sessions.add(normalized)
            # 纳入统一任务登记，插件终止时可被一并取消，避免游离后台任务。
            plugin._track_task(
                asyncio.create_task(
                    plugin.check_and_chat(normalized, trigger=TriggerSource.MANUAL)
                )
            )
            await _broadcast_web_update("jobs")
            return {
                "ok": True,
                "session": normalized,
                "in_progress": True,
                "message": "已开始立即触发，正在等待 LLM 完成回复",
            }

        # 取消会话已注册的调度任务，并清除其持久化的触发时间。
        @app.delete("/api/jobs/{umo:path}")
        async def cancel_job(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            removed = False
            try:
                plugin.scheduler.remove_job(normalized)
                removed = True
            except Exception:
                pass

            async with plugin.data_lock:
                if normalized in plugin.session_data:
                    plugin.session_data[normalized].pop("next_trigger_time", None)
                    await plugin.storage_service.save_data()

            if removed:
                logger.info(
                    f"[主动消息] Web 管理端已取消 {plugin.session_service.get_session_log_str(normalized)} 的调度任务喵。"
                )
            else:
                logger.warning(
                    f"[主动消息] Web 管理端请求取消 {plugin.session_service.get_session_log_str(normalized)} 的调度任务喵，但当前未找到可取消任务。"
                )

            await _broadcast_web_update("jobs")
            return {"ok": True, "session": normalized, "removed": removed}

        # 结束会话当前的计时器：只退场并登记下一次任务，不会真正发送主动消息。
        @app.post("/api/timers/{umo:path}/finish")
        async def finish_timer(umo: str, timer_kind: str = ""):
            normalized = plugin.session_service.normalize_session_id(umo)
            result = await plugin.scheduler_service.finish_timer(normalized, timer_kind)
            await _broadcast_web_update("timers")
            if not result.get("ok"):
                if result.get("reason") == "reschedule_failed":
                    return JSONResponse(
                        {
                            "ok": False,
                            "session": normalized,
                            "error": "登记后续任务失败，原计时器已保留",
                        },
                        status_code=500,
                    )
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "error": "该会话当前没有正在运行的计时器",
                    },
                    status_code=404,
                )
            return {
                "ok": True,
                "session": normalized,
                "scheduled": bool(result.get("scheduled")),
                "message": "已结束计时器，下一次主动消息已进入任务管理页",
            }

        # 按当前配置重新开始计时，不发送消息也不改动未回复计数。
        @app.post("/api/timers/{umo:path}/reset")
        async def reset_timer(umo: str, timer_kind: str = ""):
            normalized = plugin.session_service.normalize_session_id(umo)
            result = await plugin.scheduler_service.reset_timer(normalized, timer_kind)
            await _broadcast_web_update("timers")
            if not result.get("ok"):
                reason = result.get("reason")
                if reason == "disabled":
                    error = "会话未启用或不存在，无法重置计时器"
                elif reason == "already_scheduled":
                    error = "该会话已有待发送任务，无需重新计时"
                elif reason == "max_unanswered":
                    error = "未回复次数已达上限，不会重新计时"
                else:
                    error = "该会话没有可重置的计时器配置"
                return JSONResponse(
                    {"ok": False, "session": normalized, "error": error},
                    status_code=400,
                )
            return {
                "ok": True,
                "session": normalized,
                "message": "已按当前配置重新开始计时",
            }

        async def _build_full_update_payload() -> dict[str, Any]:
            """构建 WebSocket 全量快照，状态与任务共用一次人格解析。"""
            combined = await state.build_realtime_payload()
            return {
                "status": combined["status"],
                "jobs": combined["jobs"],
                "sessions": state.list_known_session_summaries(),
                "notifications": await state.build_notification_payload(),
            }

        # WebSocket 实时通道：连接后先推送一次全量快照，之后响应 ping/refresh。
        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            if self.token_auth.enabled:
                token = websocket.query_params.get("token", "")
                if not token:
                    auth_header = websocket.headers.get("Authorization", "")
                    if auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                if not self.token_auth.verify_token(token):
                    await websocket.close(code=1008)
                    return

            await websocket.accept()
            self.realtime.add(websocket)

            try:
                await websocket.send_json(
                    {"type": "full_update", "data": await _build_full_update_payload()}
                )

                while True:
                    data = await websocket.receive_text()
                    try:
                        msg = json.loads(data)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        logger.debug(
                            f"[主动消息] WebSocket 收到无效 JSON 数据喵: {str(data)[:100]}"
                        )
                        continue

                    if not isinstance(msg, dict):
                        logger.debug(
                            "[主动消息] WebSocket 收到的 JSON 不是对象，已忽略喵。"
                        )
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg_type == "refresh":
                        await websocket.send_json(
                            {
                                "type": "full_update",
                                "data": await _build_full_update_payload(),
                            }
                        )
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"[主动消息] WebSocket 连接异常喵: {e}")
            finally:
                self.realtime.remove(websocket)
