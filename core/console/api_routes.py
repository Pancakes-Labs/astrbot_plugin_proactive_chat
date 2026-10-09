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

# 注意：WebSocket / FileResponse / JSONResponse 必须在模块全局命名空间可用。
try:  # pragma: no cover - 取决于是否安装 FastAPI
    from fastapi import WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
except ImportError:  # pragma: no cover
    WebSocket = None  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]


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
            return state.build_status_payload()

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

        # 读取配置（web_admin 分组会剔除 password 字段）。
        @app.get("/api/config")
        async def get_config():
            web_admin = {
                k: v
                for k, v in self.config.get("web_admin", {}).items()
                if k != "password"
            }
            return {
                "friend_settings": dict(self.config.get("friend_settings", {})),
                "group_settings": dict(self.config.get("group_settings", {})),
                "web_admin": web_admin,
                "notification_settings": dict(
                    self.config.get("notification_settings", {})
                ),
            }

        # 返回 _conf_schema.json 内容，供前端渲染配置表单。
        @app.get("/api/config-schema")
        async def get_config_schema():
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

        # 更新配置：仅接受 friend_settings / group_settings / web_admin 三组。
        @app.post("/api/config")
        async def update_config(payload: dict[str, Any]):
            allowed_keys = {"friend_settings", "group_settings", "web_admin"}
            for key in allowed_keys:
                if key not in payload:
                    continue
                if key == "web_admin":
                    old = dict(self.config.get("web_admin", {}))
                    old.update(payload.get("web_admin", {}))
                    if "password" in payload.get("web_admin", {}):
                        old["password"] = payload["web_admin"]["password"]
                    self.config["web_admin"] = old
                else:
                    self.config[key] = payload[key]

            plugin.config.save_config()
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
                        "enabled": bool(effective and effective.get("enable", False)),
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

        # 写入会话配置：mode=override 直接覆盖差异；否则按 effective 反推差异。
        @app.post("/api/session-config/{umo:path}")
        async def update_session_config(umo: str, payload: dict[str, Any]):
            normalized = plugin.session_service.normalize_session_id(umo)
            mode = payload.get("mode", "effective")

            if mode == "override":
                override = payload.get("override", {})
                if not isinstance(override, dict):
                    return JSONResponse(
                        {"error": "override 必须是对象"}, status_code=400
                    )
                await plugin.session_override_manager.set_override(normalized, override)
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
                await plugin.session_override_manager.update_session_from_effective(
                    normalized, base, effective
                )

            await _broadcast_web_update("session-config")
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
            await plugin.session_override_manager.delete_override(normalized)
            await _broadcast_web_update("session-config")
            return {
                "ok": True,
                "session": normalized,
                "override": {},
                "effective": plugin.config_service.get_session_config(normalized),
            }

        # 调度任务列表。
        @app.get("/api/jobs")
        async def list_jobs():
            return {"jobs": state.collect_jobs()}

        # 手动重新调度下一次主动消息（不重置未回复计数）。
        @app.post("/api/jobs/{umo:path}/reschedule")
        async def reschedule_job(umo: str):
            normalized = plugin.session_service.normalize_session_id(umo)
            session_config = plugin.config_service.get_session_config(normalized)
            if not session_config or not session_config.get("enable", False):
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
            asyncio.create_task(plugin.check_and_chat(normalized))
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
                    {
                        "type": "full_update",
                        "data": {
                            "status": state.build_status_payload(),
                            "jobs": state.collect_jobs(),
                            "sessions": state.list_known_session_summaries(),
                            "notifications": await state.build_notification_payload(),
                        },
                    }
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
                                "data": {
                                    "status": state.build_status_payload(),
                                    "jobs": state.collect_jobs(),
                                    "sessions": state.list_known_session_summaries(),
                                    "notifications": await state.build_notification_payload(),
                                },
                            }
                        )
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"[主动消息] WebSocket 连接异常喵: {e}")
            finally:
                self.realtime.remove(websocket)
