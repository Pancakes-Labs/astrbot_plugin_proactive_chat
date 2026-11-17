"""本轮提示词模板预选服务。

把抽模板从生成时提前到排期时：卡片能提前看到、也能提前换掉，
而不必等到 LLM 生成时才知道抽中了什么。

两条铁律：

1. 预选只存模板 id 与展示快照，绝不存渲染后的正文。占位符在 PROMPT 阶段按生成时刻
   求值，提前几小时渲染会把时间定格成旧值，模型读到的是过期上下文。
2. 预选按排期时刻绑定轮次。只有同一 round_token 才允许复用，
   上一轮遗留（含 GATE 中止后没来得及清理的残留）会自动作废，
   不会带着一条用户早已忘记的模板进入新一轮。
"""

from __future__ import annotations

import random
import time
from typing import Any

from astrbot.api import logger

from ..domain.prompt_templates import (
    build_candidate_pool,
    get_avoid_repeat,
    resolve_active_template,
    session_type_of,
    template_fingerprint,
)

# 存放在 session_data[session_id] 下的键。
# 与 next_trigger_time 同层，跟着调度状态一起持久化。
PROMPT_PRESET_KEY = "prompt_preset"
# 最近实际发出的模板指纹（新的在前）。
LAST_TEMPLATE_KEY = "last_prompt_templates"
# 历史上限。
LAST_TEMPLATE_LIMIT = 10


class PromptPresetService:
    """提示词模板的预选、复用与消费。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 内部读取
    # ------------------------------------------------------------------
    def _session_payload(self, session_id: str) -> dict[str, Any] | None:
        """取出会话数据字典；不存在或类型异常时返回 None。"""
        info = self.plugin.session_data.get(session_id)
        return info if isinstance(info, dict) else None

    def _pool(self, session_id: str) -> list[dict[str, Any]]:
        """构建该会话本轮真实可抽取的候选池（含 scope 过滤）。"""
        session_config = self.plugin.config_service.get_session_config(session_id) or {}
        return build_candidate_pool(
            session_config, session_type=session_type_of(session_config)
        )

    @staticmethod
    def _round_token(scheduled_at: float | None) -> str:
        """把排期时刻转成轮次标识。

        时刻缺失时退化为空串。
        """
        if scheduled_at is None:
            return ""
        try:
            return f"{float(scheduled_at):.3f}"
        except (TypeError, ValueError):
            return ""

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def resolve_template(
        self, session_id: str, template_id: Any
    ) -> dict[str, Any] | None:
        """按 id 在真实候选池内查找模板。

        必须查池而不是直接查配置。
        """
        text_id = str(template_id or "")
        if not text_id:
            return None
        for item in self._pool(session_id):
            if str(item.get("id") or "") == text_id:
                return item
        return None

    def peek(self, session_id: str) -> dict[str, Any] | None:
        """读取当前预选记录（不校验有效性、不清理）。"""
        payload = self._session_payload(session_id)
        if payload is None:
            return None
        preset = payload.get(PROMPT_PRESET_KEY)
        return preset if isinstance(preset, dict) else None

    def recent_fingerprints(self, session_id: str) -> list[str]:
        """读取最近实际发出的模板指纹（新的在前）。

        损坏数据一律当作空记录：回避只是锦上添花，不能因为它让抽签失败。
        """
        payload = self._session_payload(session_id)
        if payload is None:
            return []
        records = payload.get(LAST_TEMPLATE_KEY)
        if not isinstance(records, list):
            return []
        return [
            str(item.get("fingerprint") or "")
            for item in records
            if isinstance(item, dict) and str(item.get("fingerprint") or "")
        ]

    def avoid_for_session(self, session_id: str, session_config: dict) -> list[str]:
        """按配置返回本轮需要回避的指纹列表；未启用时返回空列表。"""
        enabled, window = get_avoid_repeat(session_config)
        if not enabled:
            return []
        return self.recent_fingerprints(session_id)[:window]

    def remember_template(
        self,
        session_id: str,
        template: dict[str, Any] | None,
    ) -> bool:
        """记录本轮实际发出的模板，返回是否确有变更。

        只操作内存，不落盘：调用方通常已持有 data_lock，
        由它决定何时 save_data，避免这里二次加锁造成死锁。
        """
        fingerprint = template_fingerprint(template)
        if not fingerprint:
            return False
        payload = self._session_payload(session_id)
        if payload is None:
            payload = {}
            self.plugin.session_data[session_id] = payload

        records = payload.get(LAST_TEMPLATE_KEY)
        history = records if isinstance(records, list) else []
        kept = [
            item
            for item in history
            if isinstance(item, dict)
            and str(item.get("fingerprint") or "")
            and str(item.get("fingerprint") or "") != fingerprint
        ]
        kept.insert(
            0,
            {
                "fingerprint": fingerprint,
                "name": str((template or {}).get("name") or ""),
                "used_at": time.time(),
            },
        )
        payload[LAST_TEMPLATE_KEY] = kept[:LAST_TEMPLATE_LIMIT]
        return True

    def describe(self, session_id: str) -> dict[str, Any]:
        """给 Web 卡片用的预选摘要。

        available 表示预选指向的模板此刻是否仍可用。
        """
        preset = self.peek(session_id)
        if not preset:
            return {"picked": False, "available": False}
        template = self.resolve_template(session_id, preset.get("id"))
        return {
            "picked": True,
            "available": template is not None,
            "id": str(preset.get("id") or ""),
            "name": str(preset.get("name") or ""),
            "weight": preset.get("weight"),
            "scope": str(preset.get("scope") or ""),
            "picked_at": preset.get("picked_at"),
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def pick_for_session(
        self,
        session_id: str,
        *,
        scheduled_at: float | None = None,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """为当前排期轮次准备预选模板。

        已有可用预选时默认复用，force=True 才强制重抽。
        """
        payload = self._session_payload(session_id)
        if payload is None:
            # 会话数据尚未建立：此刻无处存放，交给生成时现场抽样。
            return None

        round_token = self._round_token(scheduled_at)
        if not force:
            preset = self.peek(session_id)
            if preset:
                template = self.resolve_template(session_id, preset.get("id"))
                if template is not None:
                    return template

        session_config = self.plugin.config_service.get_session_config(session_id) or {}
        template = resolve_active_template(
            session_config,
            session_type=session_type_of(session_config),
            rng=random.Random(),
            avoid=self.avoid_for_session(session_id, session_config),
        )
        if template is None:
            payload.pop(PROMPT_PRESET_KEY, None)
            logger.warning(
                "[主动消息] 没有可用的提示词模板，本轮预选为空，"
                "生成时将只使用内置场景层与约束层喵。"
            )
            return None

        payload[PROMPT_PRESET_KEY] = {
            "id": str(template.get("id") or ""),
            "name": str(template.get("name") or ""),
            "weight": template.get("weight"),
            "scope": str(template.get("scope") or ""),
            "picked_at": time.time(),
            "round_token": round_token,
        }
        logger.info(
            f"[主动消息] 已预选本轮提示词模板："
            f"{template.get('name') or '(未命名)'}"
            f"（权重为 {template.get('weight')}）喵。"
        )
        return template

    def clear(self, session_id: str) -> bool:
        """移除预选记录。

        只操作内存，不落盘：调用方可能已经持有 data_lock，
        由它决定何时 save_data，避免这里二次加锁造成死锁。
        """
        payload = self._session_payload(session_id)
        if payload is None or PROMPT_PRESET_KEY not in payload:
            return False
        del payload[PROMPT_PRESET_KEY]
        return True

    # ------------------------------------------------------------------
    # 生成期消费
    # ------------------------------------------------------------------
    def consume_for_generation(
        self, session_id: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """取走本轮预选，返回 (模板, 是否命中预选)。

        无论命中与否都会清除预选：一轮只消费一次，
        否则下一轮会继续沿用同一套模板，权重抽签形同虚设。
        """
        preset = self.peek(session_id)
        if not preset:
            return None, False
        template = self.resolve_template(session_id, preset.get("id"))
        self.clear(session_id)
        if template is None:
            logger.info(
                "[主动消息] 预选提示词模板已不可用（可能被删除、停用或改了适用范围），"
                "本轮重新抽取喵。"
            )
            return None, False
        return template, True

    # ------------------------------------------------------------------
    # Web 端重抽
    # ------------------------------------------------------------------
    def resample(self, session_id: str) -> dict[str, Any]:
        """强制重抽预选，供任务卡片按钮使用。

        只改预选本身：不碰排期时间、不碰计时器、不碰未回复计数。
        """
        payload = self._session_payload(session_id)
        if payload is None:
            return {"ok": False, "reason": "session_data_missing"}

        # 沿用当前轮次标识，保证这条新预选归属同一次排期。
        template = self.pick_for_session(
            session_id,
            scheduled_at=payload.get("last_scheduled_at"),
            force=True,
        )
        if template is None:
            return {"ok": False, "reason": "no_available_template"}
        return {"ok": True, "preset": self.describe(session_id)}


__all__ = [
    "LAST_TEMPLATE_KEY",
    "LAST_TEMPLATE_LIMIT",
    "PROMPT_PRESET_KEY",
    "PromptPresetService",
]
