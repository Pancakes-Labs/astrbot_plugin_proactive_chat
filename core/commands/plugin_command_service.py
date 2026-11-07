"""插件指令业务实现。

main.py 只保留 @filter.command 薄壳，真正逻辑收敛在这里：
帮助索引、状态查询、立即发送、开关切换、插件重载与进程重启。

所有写操作都遵守同一条链路：权限守卫 -> 参数校验 -> 落盘
-> 重排期或取消任务 -> 广播 Web 控制台 -> 一句话确认回显。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ...utils.astrbot_restart import (
    is_demo_mode,
    is_desktop_managed,
    restart_astrbot_in_background,
)
from ...utils.time_utils import is_quiet_time
from ..domain.domain_enums import TriggerSource
from .command_support_service import (
    SCOPE_CURRENT,
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    CommandSupportService,
)
from .forward_helper import FORWARD_NAME, send_forward_blocks

# 提示语前缀常量，避免多处手写不一致。
_DENY = "🚫 权限不足：此命令仅限管理员使用。"


class PluginCommandService:
    """插件指令服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.support = CommandSupportService(plugin)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    async def _broadcast(self, reason: str) -> None:
        """广播 Web 控制台刷新（失败不影响指令主流程）。"""
        web = getattr(self.plugin, "web_admin_server", None)
        if not web:
            return
        try:
            await web._broadcast_update(reason)
        except Exception as e:  # noqa: BLE001 - 广播是旁路
            logger.debug(f"[主动消息] 广播 {reason} 失败喵: {e}")

    async def _admin_denied(self, event: Any) -> bool:
        """非管理员时返回 True；调用方负责 yield 权限提示。"""
        return not await self.support.is_plugin_admin(event)

    def _session_disabled_reason(self, session_id: str) -> str:
        """为“该会话不会主动发送”给出具体原因。"""
        base = self.support.base_config_of(session_id)
        if base is None:
            scope_key = (
                "group_settings"
                if self.support.is_group_session(session_id)
                else "friend_settings"
            )
            return (
                f"该会话未命中 {scope_key} 会话列表，插件不会对其主动发送。\n"
                f"请先在插件配置或 WebUI 中把该会话加入对应列表。"
            )
        if not base.get("enable", False):
            return "该会话所属的全局开关已关闭，请在配置中开启后再试。"
        return ""

    # ------------------------------------------------------------------
    # 1. 帮助
    # ------------------------------------------------------------------
    async def handle_help(self, event: Any):
        """显示插件帮助信息（分组索引页）。"""
        # 显式标记已处理，避免帮助内容被 LLM 二次接管。
        try:
            event.should_call_llm(True)
        except Exception:
            pass

        header = (
            "🐱 主动消息插件使用指南\n"
            "──────────────\n"
            "📌 参数约定：<必填> [可选]\n"
            "🔐 除本帮助外，其余指令均需管理员权限\n"
            "💡 输入 /主动消息 可随时查看本指南"
        )
        blocks = [
            (
                "📊 状态查询\n"
                "• /主动消息状态 [当前会话|全局|<会话UMO>]\n"
                "   缺省只返回当前会话；全局会以合并转发列出全部会话\n"
                "   例：/主动消息状态\n"
                "        /主动消息状态 全局\n"
                "        /主动消息状态 default:GroupMessage:123456789"
            ),
            (
                "📤 立即发送\n"
                "• /主动消息发送 [当前会话|全局|<会话UMO>]\n"
                "   立即触发一次主动消息（需会话已启用）\n"
                "   例：/主动消息发送\n"
                "        /主动消息发送 全局"
            ),
            (
                "🔘 开关推送\n"
                "• /主动消息开关 [当前会话|全局|<会话UMO>] [on|off]\n"
                "   不带 on/off 时切换当前状态\n"
                "   全局作用域等价于 friend_settings / group_settings 两个总开关\n"
                "   例：/主动消息开关 off\n"
                "        /主动消息开关 全局 on"
            ),
            (
                "🛠️ 运维管理\n"
                "• /主动消息重启 - 重载本插件（别名 /主动消息重载）\n"
                "• /重启AstrBot - 重启整个 AstrBot 进程"
            ),
            ("──────────────\n📚 进行会话差异配置请前往插件的 WebUI 管理端"),
        ]

        sent = await send_forward_blocks(
            self.plugin, event, blocks, header=header, name=FORWARD_NAME
        )
        if sent:
            return
        # 平台不支持合并转发时回退普通文本，保证帮助内容可达。
        yield event.plain_result(header + "\n\n" + "\n\n".join(blocks))

    # ------------------------------------------------------------------
    # 2. 状态查询
    # ------------------------------------------------------------------
    # 触发来源对应的文案：群聊沉默计时到点只是“计划”，不等于真的发送，
    # 只有已登记任务（schedule）才使用“预计发送时间”。
    _TRIGGER_LABELS = {
        "group_silence": "沉默计时结束",
        "auto_trigger": "自动触发检查",
        "schedule": "预计发送时间",
    }

    def _format_trigger_line(self, trigger: dict) -> str:
        """把触发信息格式化为一行，标签随触发来源动态变化。"""
        if trigger.get("remaining") is None:
            return "预计发送时间：当前没有待执行任务"

        label = self._TRIGGER_LABELS.get(trigger.get("kind"), "预计发送时间")
        clock = self.support.format_clock(trigger["target_time"])
        remain = self.support.format_duration(trigger["remaining"])
        return f"{label}：{clock}（还有 {remain}）"

    def _build_session_status(self, session_id: str) -> str:
        """构建单个会话的状态文本。"""
        plugin = self.plugin
        session_config = plugin.config_service.get_session_config(session_id)
        session_data = plugin.session_data.get(session_id, {})
        base_config = self.support.base_config_of(session_id)
        label = self.support.session_label(session_id, session_config)

        lines = [f"🐱 主动消息状态 · {label}", "──────────────"]
        lines.append(f"会话 UMO：{session_id}")

        # 启用状态：区分「未命中列表 / 全局关闭 / 会话覆写」三种来源。
        if session_config is None:
            lines.append("启用状态：⛔ 未启用")
            lines.append(f"原因：{self._session_disabled_reason(session_id)}")
            return "\n".join(lines)

        enabled = bool(session_config.get("enable", False))
        scope_name = "群聊" if self.support.is_group_session(session_id) else "私聊"
        if enabled:
            source = f"继承全局 {scope_name}设置"
            if session_config.get("_has_override"):
                source += "（含会话覆写）"
            lines.append(f"启用状态：✅ 已启用 · {source}")
        else:
            lines.append("启用状态：⛔ 已关闭")
            if base_config is not None and base_config.get("enable", False):
                lines.append("原因：该会话被单独关闭了")
            else:
                lines.append("原因：所属全局开关已关闭")

        # 触发时间：标签随来源变化，群聊只承诺“沉默计时结束”。
        trigger = self.support.describe_next_trigger(session_id, session_config)
        lines.append(self._format_trigger_line(trigger))

        # 人格：只回显会话级配置，避免为一条指令付出完整解析成本。
        persona_id = str(session_config.get("persona_id") or "").strip()
        if not persona_id:
            lines.append("使用的人格：跟随对话 / 全局默认")
        elif persona_id == "[%None]":
            lines.append("使用的人格：不使用人格")
        else:
            lines.append(f"使用的人格：{persona_id}")

        # 自动触发：到点只是“创建待执行任务”，并非直接发送。
        auto_settings = session_config.get("auto_trigger_settings", {})
        if auto_settings.get("enable_auto_trigger", False):
            lines.append(
                "自动触发设置：启动后 "
                f"{auto_settings.get('auto_trigger_after_minutes', 5)} 分钟无消息时创建待执行任务"
            )
        else:
            lines.append("自动触发设置：未启用")

        # 沉默计时时长：仅群聊有意义。
        if self.support.is_group_session(session_id):
            idle = session_config.get("group_idle_trigger_minutes", 0)
            lines.append(f"沉默计时时长：{idle} 分钟")

        # 调度区间。
        schedule_settings = session_config.get("schedule_settings", {})
        min_interval = schedule_settings.get("min_interval_minutes")
        max_interval = schedule_settings.get("max_interval_minutes")
        if min_interval is not None and max_interval is not None:
            lines.append(f"调度区间：{min_interval} - {max_interval} 分钟")

        # 未回复与上限。
        unanswered = session_data.get("unanswered_count", 0)
        max_unanswered = int(schedule_settings.get("max_unanswered_times", 0) or 0)
        if max_unanswered > 0:
            lines.append(f"未回复次数：{unanswered} / {max_unanswered}")
        else:
            lines.append(f"未回复次数：{unanswered}（未设上限）")

        # 免打扰。
        quiet_text = self.support.format_quiet_hours(schedule_settings)
        if schedule_settings.get("enable_quiet_hours", True) and is_quiet_time(
            str(schedule_settings.get("quiet_hours", "") or ""), plugin.timezone
        ):
            lines.append(f"免打扰时段：{quiet_text} · 当前正在生效")
        else:
            lines.append(f"免打扰时段：{quiet_text} · 当前未生效")

        return "\n".join(lines)

    def _build_global_status(self) -> tuple[str, list[str]]:
        """构建全局状态头部与逐会话明细块。"""
        plugin = self.plugin
        friend_settings = plugin.config.get("friend_settings", {}) or {}
        group_settings = plugin.config.get("group_settings", {}) or {}

        sessions = self.support.list_enabled_sessions()
        now = time.time()
        uptime = max(0, int(now - plugin.plugin_start_time))

        header = "\n".join(
            [
                "🌐 主动消息全局状态",
                "──────────────",
                f"插件版本：{getattr(plugin, 'version', '未知')}",
                f"运行时长：{self.support.format_duration(uptime)}",
                f"私聊开关：{'✅ 已启用' if friend_settings.get('enable', False) else '⛔ 已关闭'}"
                f"（{len(friend_settings.get('session_list', []) or [])} 个会话）",
                f"群聊开关：{'✅ 已启用' if group_settings.get('enable', False) else '⛔ 已关闭'}"
                f"（{len(group_settings.get('session_list', []) or [])} 个会话）",
                f"运行中计时器：群沉默 {len(plugin.group_timers)} 个 / "
                f"自动触发 {len(plugin.auto_trigger_timers)} 个",
                f"已注册任务：{len(plugin.scheduler.get_jobs()) if plugin.scheduler else 0} 个",
                f"会话数据量：{len(plugin.session_data)} 个",
            ]
        )

        if not sessions:
            return header, ["📋 当前没有处于启用状态的会话。"]

        blocks: list[str] = []
        for index, session_id in enumerate(sessions, start=1):
            session_config = plugin.config_service.get_session_config(session_id) or {}
            session_data = plugin.session_data.get(session_id, {})
            label = self.support.session_label(session_id, session_config)
            trigger = self.support.describe_next_trigger(session_id, session_config)

            trigger_text = self._format_trigger_line(trigger)

            schedule_settings = session_config.get("schedule_settings", {})
            max_unanswered = int(schedule_settings.get("max_unanswered_times", 0) or 0)
            unanswered = session_data.get("unanswered_count", 0)
            unanswered_text = (
                f"{unanswered} / {max_unanswered}"
                if max_unanswered > 0
                else str(unanswered)
            )

            blocks.append(
                f"{index}. {label}\n"
                f"状态：{'✅ 启用' if session_config.get('enable', False) else '⛔ 关闭'}\n"
                f"{trigger_text}\n"
                f"未回复：{unanswered_text}"
            )

        return header, blocks

    async def handle_status(self, event: Any, scope_token: str | None = None):
        """查询主动消息状态。"""
        if await self._admin_denied(event):
            yield event.plain_result(_DENY)
            return

        scope, target, error = self.support.resolve_scope(event, scope_token)
        if error:
            yield event.plain_result(f"❌ {error}")
            return

        if scope != SCOPE_GLOBAL:
            yield event.plain_result(self._build_session_status(target))
            return

        header, blocks = self._build_global_status()
        sent = await send_forward_blocks(
            self.plugin, event, blocks, header=header, name=FORWARD_NAME
        )
        if sent:
            return
        yield event.plain_result(header + "\n\n" + "\n\n".join(blocks))

    # ------------------------------------------------------------------
    # 3. 立即发送
    # ------------------------------------------------------------------
    async def _trigger_session(self, session_id: str) -> tuple[bool, str]:
        """对单个会话发起一次手动主动消息。

        Returns:
            (started, message)。started 为 False 时 message 是拒绝原因。
        """
        plugin = self.plugin
        if session_id in plugin.manual_trigger_sessions:
            return False, "该会话正在发送中，请等待发送完成。"

        disabled_reason = self._session_disabled_reason(session_id)
        if disabled_reason:
            return False, disabled_reason

        session_config = plugin.config_service.get_session_config(session_id) or {}
        if not session_config.get("enable", False):
            return False, "该会话已被单独关闭，请先执行 /主动消息开关 on。"

        schedule_settings = session_config.get("schedule_settings", {})
        if schedule_settings.get("enable_quiet_hours", True) and is_quiet_time(
            str(schedule_settings.get("quiet_hours", "") or ""), plugin.timezone
        ):
            return (
                False,
                f"当前处于免打扰时段（{self.support.format_quiet_hours(schedule_settings)}），"
                "主动消息会被拦截。",
            )

        plugin.manual_trigger_sessions.add(session_id)
        plugin._track_task(
            asyncio.create_task(
                plugin.check_and_chat(session_id, trigger=TriggerSource.MANUAL)
            )
        )
        return True, "已开始立即发送"

    async def handle_send(self, event: Any, scope_token: str | None = None):
        """立即发送指定作用域的主动消息。"""
        if await self._admin_denied(event):
            yield event.plain_result(_DENY)
            return

        scope, target, error = self.support.resolve_scope(event, scope_token)
        if error:
            yield event.plain_result(f"❌ {error}")
            return

        if scope != SCOPE_GLOBAL:
            started, message = await self._trigger_session(target)
            if started:
                label = self.support.session_label(target)
                await self._broadcast("jobs")
                yield event.plain_result(
                    f"✅ 已开始为 {label} 立即发送主动消息，请稍候。"
                )
            else:
                yield event.plain_result(f"❌ {message}")
            return

        # 全局：逐个会话尝试，汇总成功与跳过原因。
        sessions = self.support.list_enabled_sessions()
        if not sessions:
            yield event.plain_result("❌ 当前没有处于启用状态的会话。")
            return

        started_labels: list[str] = []
        skipped: list[str] = []
        for session_id in sessions:
            started, message = await self._trigger_session(session_id)
            label = self.support.session_label(session_id)
            if started:
                started_labels.append(label)
            else:
                skipped.append(f"{label}：{message}")

        if started_labels:
            await self._broadcast("jobs")

        if started_labels:
            lines = [f"✅ 已开始立即发送 {len(started_labels)} 个会话"]
            lines.extend(f"  · {item}" for item in started_labels)
        else:
            # 全部被跳过时不能再用 ✅ 开头，否则与「0 个会话」自相矛盾。
            lines = ["⏭️ 没有会话可以立即发送"]

        if skipped:
            lines.append("")
            lines.append(f"⏭️ 跳过 {len(skipped)} 个会话")
            lines.extend(f"  · {item}" for item in skipped)
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 4. 开关
    # ------------------------------------------------------------------
    async def _set_session_enable(self, session_id: str, enabled: bool) -> str:
        """写会话级 enable 覆写，并按新状态重排期或清理任务。"""
        plugin = self.plugin

        base_config = self.support.base_config_of(session_id)
        if base_config is None:
            return self._session_disabled_reason(session_id)

        override = plugin.session_override_manager.get_override(session_id)
        override["enable"] = enabled
        await plugin.session_override_manager.set_override(session_id, override)

        if enabled:
            # 群聊由沉默计时驱动，不注册持久化定时任务，否则会多出一条主动消息。
            if self.support.is_group_session(session_id):
                await plugin.scheduler_service.reset_group_silence_timer(session_id)
            else:
                await plugin.scheduler_service.schedule_next_chat_and_save(
                    session_id, reset_counter=False
                )
            return ""

        # 关闭：撤掉所有与之相关的调度与计时器，避免幽灵任务。
        await self._cancel_session_schedule(session_id)
        return ""

    async def _cancel_session_schedule(self, session_id: str) -> None:
        """取消会话的调度任务、持久化触发时间与两类计时器。"""
        plugin = self.plugin

        scheduler = getattr(plugin, "scheduler", None)
        if scheduler:
            try:
                scheduler.remove_job(session_id)
            except Exception:
                pass
            try:
                plugin.scheduler_service.purge_related_jobs(session_id)
            except Exception:
                pass

        await plugin.scheduler_service.cancel_auto_trigger(session_id)

        for timer_key in (session_id,):
            timer = plugin.group_timers.pop(timer_key, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

        async with plugin.data_lock:
            payload = plugin.session_data.get(session_id)
            if isinstance(payload, dict):
                payload.pop("next_trigger_time", None)
                payload.pop("last_scheduled_at", None)
            await plugin.storage_service.save_data()

    async def _set_global_enable(self, enabled: bool) -> list[str]:
        """切换 friend_settings / group_settings 两个总开关。

        Returns:
            实际发生变化的配置分组名列表。
        """
        plugin = self.plugin
        changed: list[str] = []
        # 关闭前先快照会话清单：配置一旦置为 False，list_enabled_sessions 会立刻返回空。
        known_sessions = list(plugin.session_data.keys())
        for session_id in self.support.list_enabled_sessions():
            if session_id not in known_sessions:
                known_sessions.append(session_id)

        for scope_key in ("friend_settings", "group_settings"):
            settings = plugin.config.get(scope_key)
            if not isinstance(settings, dict):
                continue
            if bool(settings.get("enable", False)) == enabled:
                continue
            settings["enable"] = enabled
            changed.append(scope_key)

        if not changed:
            return changed

        try:
            plugin.config.save_config()
        except Exception as e:  # noqa: BLE001 - 内存已改，落盘失败仅告警
            logger.error(f"[主动消息] 保存全局开关失败喵: {e}")

        if enabled:
            await plugin.scheduler_service.setup_auto_triggers_for_enabled_sessions()
            for session_id in self.support.list_enabled_sessions():
                if self.support.is_group_session(session_id):
                    await plugin.scheduler_service.reset_group_silence_timer(session_id)
                else:
                    await plugin.scheduler_service.schedule_next_chat_and_save(
                        session_id, reset_counter=False
                    )
        else:
            for session_id in known_sessions:
                await self._cancel_session_schedule(session_id)

        return changed

    async def handle_toggle(
        self,
        event: Any,
        scope_token: str | None = None,
        switch_token: str | None = None,
    ):
        """开启或关闭指定作用域的主动消息推送。"""
        if await self._admin_denied(event):
            yield event.plain_result(_DENY)
            return

        # 允许省略作用域直接写开关词：/主动消息开关 off
        first_is_switch = self.support.parse_switch(scope_token) is not None
        if first_is_switch:
            scope_token, switch_token = None, scope_token
        elif self.support.parse_switch(switch_token) is not None:
            # 参数写反了（/主动消息开关 on 全局）：无法判断用户本意是
            # 「当前会话 + 开启」还是「全局 + 开启」，直接提示正确用法，
            # 不做任何状态变更，避免静默丢弃作用域参数。
            yield event.plain_result(
                "❌ 参数顺序有误：开关词应写在作用域之后。\n"
                "用法：/主动消息开关 [当前会话|全局|<会话UMO>] [on|off]\n"
                "例：/主动消息开关 全局 on"
            )
            return

        scope, target, error = self.support.resolve_scope(event, scope_token)
        if error:
            yield event.plain_result(f"❌ {error}")
            return

        requested = self.support.parse_switch(switch_token)

        if scope != SCOPE_GLOBAL:
            session_config = self.plugin.config_service.get_session_config(target)
            current = bool(session_config and session_config.get("enable", False))
            enabled = (not current) if requested is None else requested

            reason = await self._set_session_enable(target, enabled)
            if reason:
                yield event.plain_result(f"❌ {reason}")
                return

            await self._broadcast("session-config")
            label = self.support.session_label(target)
            state_text = "开启" if enabled else "关闭"
            yield event.plain_result(
                f"✅ 已{state_text} {label} 的主动消息推送。"
                + ("" if enabled else "\n已同步取消该会话的调度任务与计时器。")
            )
            return

        # 全局作用域。
        friend_enabled = bool(
            (self.plugin.config.get("friend_settings", {}) or {}).get("enable", False)
        )
        group_enabled = bool(
            (self.plugin.config.get("group_settings", {}) or {}).get("enable", False)
        )
        if requested is None:
            # 未指定 on/off 时取反：任一分组开启就整体关闭。
            enabled = not (friend_enabled or group_enabled)
        else:
            enabled = requested

        changed = await self._set_global_enable(enabled)
        await self._broadcast("config")

        state_text = "开启" if enabled else "关闭"
        if not changed:
            yield event.plain_result(
                f"ℹ️ 全局主动消息已经是{state_text}状态，未做任何改动。"
            )
            return

        scope_text = "、".join(
            "私聊" if key == "friend_settings" else "群聊" for key in changed
        )
        yield event.plain_result(
            f"✅ 已{state_text}全局主动消息（{scope_text}）。"
            + ("" if enabled else "\n已同步取消全部会话的调度任务与计时器。")
        )

    # ------------------------------------------------------------------
    # 5. 重载插件
    # ------------------------------------------------------------------
    async def handle_reload_plugin(self, event: Any):
        """重载本插件（等价于 AstrBot WebUI 的重载插件）。"""
        if await self._admin_denied(event):
            yield event.plain_result(_DENY)
            return

        plugin_manager = getattr(self.plugin.context, "_star_manager", None)
        if plugin_manager is None:
            yield event.plain_result("❌ 无法获取 AstrBot 插件管理器。")
            return

        plugin_name = Path(__file__).resolve().parents[2].name
        yield event.plain_result("🔄 正在重载主动消息插件，请稍候…")

        try:
            success, message = await plugin_manager.reload(plugin_name)
        except Exception as e:  # noqa: BLE001 - 重载失败需回报用户
            logger.error(f"[主动消息] 插件重载失败喵: {e}")
            yield event.plain_result(f"❌ 插件重载失败：{e}")
            return

        if success:
            # 重载后旧实例已被销毁，yield 管道不再可靠，改用 context 直发。
            await self.plugin.context.send_message(
                event.unified_msg_origin,
                self._plain_chain("✅ 主动消息插件重载完成，正在重新启动。"),
            )
        else:
            yield event.plain_result(f"❌ 插件重载失败：{message}")

    @staticmethod
    def _plain_chain(text: str) -> Any:
        """构造只含纯文本的消息链。"""
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageChain

        return MessageChain([Comp.Plain(text)])

    # ------------------------------------------------------------------
    # 6. 重启 AstrBot
    # ------------------------------------------------------------------
    async def handle_restart_astrbot(self, event: Any):
        """重启整个 AstrBot 进程。"""
        if await self._admin_denied(event):
            yield event.plain_result(_DENY)
            return

        if is_desktop_managed():
            yield event.plain_result(
                "❌ 当前由 AstrBot Desktop 托管运行，无法通过核心命令重启。\n"
                "请从桌面客户端执行重启或更新。"
            )
            return

        if is_demo_mode():
            yield event.plain_result("❌ 演示模式（DEMO_MODE）下不允许执行此操作。")
            return

        yield event.plain_result(
            "🔄 已触发 AstrBot 重启，进程将在数秒内重新启动，请稍候…"
        )

        ok, error = restart_astrbot_in_background()
        if not ok:
            await self.plugin.context.send_message(
                event.unified_msg_origin,
                self._plain_chain(
                    f"❌ 触发 AstrBot 重启失败：{error or '未知原因'}\n请查看服务端日志。"
                ),
            )

    # ------------------------------------------------------------------
    # Web 端复用入口（供 /api/plugin/reload 与 /api/astrbot/restart 调用）
    # ------------------------------------------------------------------
    def register_reload_task(self, background_tasks: Any) -> str:
        """向 FastAPI BackgroundTasks 注册插件重载任务，返回响应文案。"""
        plugin_manager = getattr(self.plugin.context, "_star_manager", None)
        if plugin_manager is None:
            raise RuntimeError("无法获取 AstrBot 插件管理器")

        plugin_name = Path(__file__).resolve().parents[2].name

        async def _run_reload() -> None:
            try:
                success, message = await plugin_manager.reload(plugin_name)
                if not success:
                    logger.warning(f"[主动消息] Web 端重载插件失败喵: {message}")
            except Exception as e:  # noqa: BLE001 - 后台任务边界兜底
                logger.error(f"[主动消息] Web 端重载插件失败喵: {e}")

        async def _dispatch() -> None:
            # 立即返回独立任务，绝不在请求上下文内等待重载完成。
            asyncio.create_task(_run_reload())

        background_tasks.add_task(_dispatch)
        return "正在重载主动消息插件，请稍候…"

    async def web_restart_astrbot(self) -> tuple[bool, str]:
        """Web 管理端重启 AstrBot 进程，返回 (ok, message)。"""
        if is_desktop_managed():
            return False, (
                "当前由 AstrBot Desktop 托管运行，无法通过核心命令重启。\n"
                "请从桌面客户端执行重启或更新。"
            )
        if is_demo_mode():
            return False, "演示模式（DEMO_MODE）下不允许执行此操作。"

        ok, error = restart_astrbot_in_background()
        if not ok:
            logger.error(f"[主动消息] Web 端触发 AstrBot 重启失败喵: {error}")
            return False, "触发 AstrBot 重启失败，请查看服务端日志"

        return True, "已触发 AstrBot 重启，进程将在数秒内重新启动，请稍候…"


__all__ = [
    "SCOPE_CURRENT",
    "SCOPE_GLOBAL",
    "SCOPE_SESSION",
    "PluginCommandService",
]
