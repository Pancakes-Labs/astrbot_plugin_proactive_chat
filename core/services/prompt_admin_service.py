"""提示词管理服务（供 Web 控制台的提示词页使用）。

把「模板读写 / 概率计算 / 完整 Prompt 预览 / 抽样预演 / 预设套装」这些
不属于流水线运行期的逻辑集中在这里，路由层只做参数校验与响应包装。

与运行期的边界：
- 运行期读的是 plugin.config 里已装配好的生效配置；
- 本服务写入时直接改 session_defaults.prompt_settings.templates，
  并由调用方负责落盘与广播，避免路由散落持久化细节。
"""

from __future__ import annotations

import copy
import random
from typing import Any

from astrbot.api import logger

from ..domain.config_keys import (
    OUTPUT_FORMAT_CUSTOM_KEY,
    OUTPUT_FORMAT_KEY,
    PROMPT_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
)
from ..domain.prompt_contract import build_prompt_layers
from ..domain.prompt_placeholders import (
    PLACEHOLDER_CONVERSATION_TYPE,
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_SPECS,
    PLACEHOLDER_UNANSWERED_COUNT,
    build_capabilities,
    conversation_type_label,
    resolve_placeholders,
)
from ..domain.prompt_presets import (
    HEADER_FINAL,
    HEADER_INTENT,
    PRESET_REVISION,
    PRESET_REVISION_KEY,
    build_preset_templates,
)
from ..domain.prompt_templates import (
    OUTPUT_FORMAT_CUSTOM,
    build_candidate_pool,
    compute_probabilities,
    get_output_format,
    get_templates,
    normalize_templates,
    resolve_active_template,
)

# 预览/预演默认使用的未回复次数与流水条数。
PREVIEW_DEFAULT_UNANSWERED = 0
PREVIEW_DEFAULT_HISTORY_COUNT = 20
# 抽样预演的默认次数。
SIMULATE_DEFAULT_ROUNDS = 1000
SIMULATE_MAX_ROUNDS = 20000


class PromptAdminService:
    """提示词模板的管理与预演。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------
    def _defaults_section(self, *, create: bool = False) -> dict[str, Any]:
        """取 session_defaults 段（必要时就地创建）。"""
        config = self.plugin.config
        section = config.get(SESSION_DEFAULTS_KEY)
        if isinstance(section, dict):
            return section
        if not create:
            return {}
        section = {}
        config[SESSION_DEFAULTS_KEY] = section
        return section

    def _prompt_settings(self, *, create: bool = False) -> dict[str, Any]:
        """取 session_defaults.prompt_settings（必要时就地创建）。"""
        section = self._defaults_section(create=create)
        if not section:
            return {}
        settings = section.get(PROMPT_SETTINGS_KEY)
        if isinstance(settings, dict):
            return settings
        if not create:
            return {}
        settings = {}
        section[PROMPT_SETTINGS_KEY] = settings
        return settings

    def read_templates(self) -> list[dict[str, Any]]:
        """读取当前生效的模板列表（已归一化）。"""
        settings = self._prompt_settings()
        if not settings:
            return []
        return get_templates({PROMPT_SETTINGS_KEY: settings})

    # ------------------------------------------------------------------
    # 列表载荷
    # ------------------------------------------------------------------
    def build_list_payload(self) -> dict[str, Any]:
        """构建提示词页的列表载荷：模板 + 各池概率分布。"""
        templates = self.read_templates()
        settings = self._prompt_settings()

        pools: dict[str, Any] = {}
        for session_type, label in (("friend", "私聊"), ("group", "群聊")):
            pool = build_candidate_pool(
                {PROMPT_SETTINGS_KEY: settings}, session_type=session_type
            )
            pools[session_type] = {
                "label": label,
                "count": len(pool),
                "total_weight": sum(int(item.get("weight") or 0) for item in pool),
                "probabilities": compute_probabilities(pool),
            }

        return {
            "templates": templates,
            "pools": pools,
            "preset_revision": PRESET_REVISION,
            "stored_revision": settings.get(PRESET_REVISION_KEY),
            "output_format": get_output_format({PROMPT_SETTINGS_KEY: settings})[0],
            "output_format_custom": get_output_format({PROMPT_SETTINGS_KEY: settings})[
                1
            ],
            "total_weight": sum(int(item.get("weight") or 0) for item in templates),
            "enabled_count": sum(1 for item in templates if item.get("enabled")),
            "headers": {"intent": HEADER_INTENT, "final": HEADER_FINAL},
            "placeholders": [
                {"key": spec.key, "label": spec.label} for spec in PLACEHOLDER_SPECS
            ],
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def save_templates(self, raw_templates: Any) -> list[str]:
        """整表保存模板，返回发生的结构性问题（供前端提示）。

        保存即代表用户主动确认当前内容：顺带把 preset_revision 写成当前版本，
        防止下次启动的强推把这些改动覆盖掉。
        """
        warnings: list[str] = []
        normalized = (
            normalize_templates(raw_templates)
            if isinstance(raw_templates, list)
            else []
        )

        if not isinstance(raw_templates, list):
            warnings.append("模板列表必须是数组，已忽略本次提交。")
            return warnings

        kept: list[dict[str, Any]] = []
        for index, item in enumerate(normalized, start=1):
            text = str(item.get("text") or "")
            if not text.strip():
                warnings.append(f"第 {index} 条模板正文为空，它不会参与抽样。")
            elif not text.lstrip().startswith("["):
                # 正文不以 [ 开头时，拼装层会自动补一层 [最终指令]，
                # 若作者又在正文里写了 [最终指令]，就会出现双标题。
                if HEADER_FINAL in text:
                    warnings.append(
                        f"第 {index} 条模板正文不以 [ 开头却内含 {HEADER_FINAL}，"
                        f"实际注入时可能出现重复标题。建议改为以 {HEADER_INTENT} 开头。"
                    )
            kept.append(item)

        settings = self._prompt_settings(create=True)
        settings["templates"] = kept
        settings[PRESET_REVISION_KEY] = PRESET_REVISION
        return warnings

    def apply_preset(self) -> int:
        """把模板整体恢复为出厂预设，返回恢复后的条数。"""
        presets = build_preset_templates()
        settings = self._prompt_settings(create=True)
        settings["templates"] = copy.deepcopy(presets)
        settings[PRESET_REVISION_KEY] = PRESET_REVISION
        return len(presets)

    def export_payload(self) -> dict[str, Any]:
        """导出为可分享的模板包。"""
        settings = self._prompt_settings()
        mode, custom = get_output_format({PROMPT_SETTINGS_KEY: settings})
        return {
            "format": "astrbot-proactive-chat/prompts",
            "version": 1,
            "preset_revision": PRESET_REVISION,
            "templates": self.read_templates(),
            "output_format": mode,
            "output_format_custom": custom,
        }

    def import_payload(self, payload: Any) -> tuple[int, list[str]]:
        """导入模板包，返回 (导入条数, 问题清单)。

        只接受本插件导出的结构，或裸模板数组；其余形态直接拒绝，
        避免把任意 JSON 写进配置。
        """
        warnings: list[str] = []
        templates: Any = None
        if isinstance(payload, list):
            templates = payload
        elif isinstance(payload, dict):
            templates = payload.get("templates")
        if not isinstance(templates, list):
            return 0, ["导入内容里没有找到 templates 数组。"]

        count = len(templates)
        warnings.extend(self.save_templates(templates))

        if isinstance(payload, dict):
            mode = str(payload.get("output_format") or "")
            custom = payload.get("output_format_custom")
            settings = self._prompt_settings(create=True)
            if mode in {"default", "custom"}:
                settings[OUTPUT_FORMAT_KEY] = mode
            if isinstance(custom, str):
                settings[OUTPUT_FORMAT_CUSTOM_KEY] = custom
            elif mode == OUTPUT_FORMAT_CUSTOM:
                warnings.append(
                    "导入包选择了自定义输出格式但没有提供正文，已回退默认格式。"
                )
                settings[OUTPUT_FORMAT_KEY] = "default"

        return count, warnings

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------
    def resolve_template_for_preview(
        self, session_type: str, template_id: str = ""
    ) -> dict[str, Any] | None:
        """取用于预览的模板。

        指定 template_id 时按 id 精确命中；否则按真实抽样逻辑抽一条，
        这样预览看到的就是模型本轮真正会收到的那份提示词。
        """
        settings = self._prompt_settings()
        section = {PROMPT_SETTINGS_KEY: settings}
        if template_id:
            for item in self.read_templates():
                if str(item.get("id") or "") == template_id:
                    return item
            return None
        return resolve_active_template(section, session_type=session_type)

    def build_prompt_preview(
        self,
        *,
        session_type: str = "friend",
        unanswered_count: int = PREVIEW_DEFAULT_UNANSWERED,
        source_mode: str = "conversation_history",
        template_id: str = "",
        platform_history_count: int = PREVIEW_DEFAULT_HISTORY_COUNT,
    ) -> dict[str, Any]:
        """服务端跑一遍真实装配逻辑，返回完整提示词与分段结构。

        复用 build_prompt_layers + resolve_placeholders，保证预览内容与
        运行期送给模型的提示词完全同源，不做任何前端近似模拟。
        """
        settings = self._prompt_settings()
        section = {PROMPT_SETTINGS_KEY: settings}

        # sampled=True 表示本轮是现场抽签（未指定 template_id），
        # 每次调用都可能抽到不同模板。前端据此提示用户，避免误认为结果不稳定。
        sampled = not template_id
        template = self.resolve_template_for_preview(session_type, template_id)
        template = template or {}

        mode, custom_style = get_output_format(section)
        if mode != OUTPUT_FORMAT_CUSTOM:
            custom_style = ""

        capabilities = build_capabilities(
            session_type=session_type,
            source_mode=source_mode,
            platform_history_count=platform_history_count,
            conversation_history_count=(
                PREVIEW_DEFAULT_HISTORY_COUNT
                if source_mode in {"conversation_history", "hybrid"}
                else 0
            ),
        )
        values = {
            PLACEHOLDER_CURRENT_TIME: "2026-01-01 12:00:00",
            PLACEHOLDER_UNANSWERED_COUNT: str(unanswered_count),
            PLACEHOLDER_CONVERSATION_TYPE: conversation_type_label(session_type),
        }

        head_layers, tail_layers = build_prompt_layers(
            session_type, source_mode, custom_style=custom_style
        )

        sections: list[dict[str, Any]] = []
        for layer in head_layers:
            sections.append(
                {
                    "kind": "contract",
                    "label": "系统任务",
                    "source": layer.get("source", ""),
                    "text": str(layer.get("text") or ""),
                }
            )
        for suffix, label in ((":scene", "会话场景"), (":style", "输出风格")):
            for layer in tail_layers:
                if not str(layer.get("source", "")).endswith(suffix):
                    continue
                sections.append(
                    {
                        "kind": "contract",
                        "label": label,
                        "source": layer.get("source", ""),
                        "text": str(layer.get("text") or ""),
                    }
                )

        raw_text = str(template.get("text") or "")
        rendered = (
            resolve_placeholders(raw_text, values=values, capabilities=capabilities)
            if raw_text
            else ""
        )
        if rendered:
            sections.append(
                {
                    "kind": "template",
                    "label": "用户模板（本轮抽中）",
                    "source": "prompt_stage",
                    "template_id": template.get("id", ""),
                    "template_name": template.get("name", ""),
                    "weight": template.get("weight"),
                    "text": rendered,
                }
            )

        for layer in tail_layers:
            if not str(layer.get("source", "")).endswith(":safety"):
                continue
            sections.append(
                {
                    "kind": "contract",
                    "label": "安全约束",
                    "source": layer.get("source", ""),
                    "text": str(layer.get("text") or ""),
                }
            )

        merged = "\n\n".join(
            section["text"] for section in sections if section["text"].strip()
        )
        return {
            "session_type": session_type,
            "unanswered_count": unanswered_count,
            "source_mode": source_mode,
            "sampled": sampled,
            "template": {
                "id": template.get("id", ""),
                "name": template.get("name", ""),
                "weight": template.get("weight"),
                "scope": template.get("scope", ""),
                "matched": bool(template),
            },
            "sections": sections,
            "prompt": merged,
            "char_count": len(merged),
        }

    # ------------------------------------------------------------------
    # 抽样预演
    # ------------------------------------------------------------------
    def simulate(
        self,
        *,
        session_type: str = "friend",
        rounds: int = SIMULATE_DEFAULT_ROUNDS,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """真实跑 N 次抽样，返回分布与偏差提示。

        用途是让用户直观看到权重是否合理：如果某两条实际占比接近，
        说明它们的权重差在体感上毫无意义，需要重新分配。
        """
        rounds = max(
            1, min(int(rounds or SIMULATE_DEFAULT_ROUNDS), SIMULATE_MAX_ROUNDS)
        )
        settings = self._prompt_settings()
        pool = build_candidate_pool(
            {PROMPT_SETTINGS_KEY: settings}, session_type=session_type
        )
        if not pool:
            return {
                "session_type": session_type,
                "rounds": 0,
                "results": [],
                "message": "当前会话类型没有可用模板，无法预演。",
            }

        rng = random.Random(seed)
        counts: dict[str, int] = {str(item.get("id") or ""): 0 for item in pool}
        for _ in range(rounds):
            picked = resolve_active_template(
                {PROMPT_SETTINGS_KEY: settings},
                session_type=session_type,
                weighted=True,
                rng=rng,
            )
            if not picked:
                continue
            key = str(picked.get("id") or "")
            counts[key] = counts.get(key, 0) + 1

        expected = {
            item["id"]: item["probability"] for item in compute_probabilities(pool)
        }
        results = []
        for item in pool:
            key = str(item.get("id") or "")
            actual = counts.get(key, 0) / rounds if rounds else 0.0
            results.append(
                {
                    "id": key,
                    "name": item.get("name", ""),
                    "weight": int(item.get("weight") or 0),
                    "expected": expected.get(key, 0.0),
                    "actual": actual,
                    "count": counts.get(key, 0),
                }
            )
        results.sort(key=lambda entry: entry["actual"], reverse=True)

        logger.debug(
            f"[主动消息] 提示词预演完成：{session_type} 池 {len(pool)} 套，"
            f"抽样 {rounds} 次喵。"
        )
        return {
            "session_type": session_type,
            "rounds": rounds,
            "results": results,
            "message": "",
        }


__all__ = ["PromptAdminService"]
