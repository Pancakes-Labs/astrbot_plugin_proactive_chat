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
from ..domain.prompt_contract import build_instruction_text, build_prompt_layers
from ..domain.prompt_placeholders import (
    CAP_PLATFORM_HISTORY,
    PLACEHOLDER_CONVERSATION_TYPE,
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME,
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
        if not create and not section:
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
    def normalize_for_save(
        self, raw_templates: Any
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """只做规范化与校验，不触碰配置。

        导入流程要先验证、后提交：先把结果算出来，确认可用再写入，
        这样一次性拷入错误内容时不会先把现有模板清空。
        """
        warnings: list[str] = []
        if not isinstance(raw_templates, list):
            return [], ["模板列表必须是数组，已忽略本次提交。"]

        normalized = normalize_templates(raw_templates)
        if len(normalized) != len(raw_templates):
            skipped = len(raw_templates) - len(normalized)
            warnings.append(f"有 {skipped} 条不是合法模板对象，已忽略。")

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
        return kept, warnings

    def save_templates(self, raw_templates: Any) -> list[str]:
        """整表保存模板，返回发生的结构性问题（供前端提示）。

        保存即代表用户主动确认当前内容：顺带把 preset_revision 写成当前版本，
        防止下次启动的强推把这些改动覆盖掉。
        """
        kept, warnings = self.normalize_for_save(raw_templates)
        if not isinstance(raw_templates, list):
            return warnings

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

        必须先规范化并确认至少留下一条模板，再写入配置：
        校验失败时原模板必须原样保留，否则一次误粘贴就清空了全部提示词。
        """
        warnings: list[str] = []
        templates: Any = None
        if isinstance(payload, list):
            templates = payload
        elif isinstance(payload, dict):
            templates = payload.get("templates")
        if not isinstance(templates, list):
            return 0, ["导入内容里没有找到 templates 数组。"]
        if not templates:
            return 0, ["导入内容里的模板是空数组，已保留现有模板。"]

        # 先验证：拿到真正会被保存的条目，避免"校验前先清空"。
        kept, warnings = self.normalize_for_save(templates)
        if not kept:
            warnings.append("导入内容里没有可用模板，已保留现有模板。")
            return 0, warnings

        settings = self._prompt_settings(create=True)
        settings["templates"] = kept
        settings[PRESET_REVISION_KEY] = PRESET_REVISION
        count = len(kept)

        if isinstance(payload, dict):
            mode = str(payload.get("output_format") or "")
            custom = payload.get("output_format_custom")
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
    def _resolve_settings(
        self, draft_templates: Any = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """取用于预览/预演的 settings 与模板列表。

        draft_templates 非空时优先使用它：前端把未保存的草稿传上来，
        预览与预演才能和左侧概率面板显示的是同一份数据。
        """
        settings = self._prompt_settings()
        if not isinstance(draft_templates, list):
            return settings, self.read_templates()
        kept, _ = self.normalize_for_save(draft_templates)
        merged = copy.deepcopy(settings)
        merged["templates"] = kept
        return merged, kept

    def resolve_template_for_preview(
        self,
        session_type: str,
        template_id: str = "",
        *,
        draft_templates: Any = None,
    ) -> dict[str, Any] | None:
        """取用于预览的模板。

        指定 template_id 时按 id 精确命中；否则按真实抽样逻辑抽一条，
        这样预览看到的就是模型本轮真正会收到的那份提示词。
        """
        settings, templates = self._resolve_settings(draft_templates)
        section = {PROMPT_SETTINGS_KEY: settings}
        if template_id:
            for item in templates:
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
        draft_templates: Any = None,
    ) -> dict[str, Any]:
        """服务端跑一遍真实装配逻辑，返回完整提示词与分段结构。

        复用 build_prompt_layers + resolve_placeholders，保证预览内容与
        运行期送给模型的提示词完全同源，不做任何前端近似模拟。
        """
        settings, _ = self._resolve_settings(draft_templates)
        section = {PROMPT_SETTINGS_KEY: settings}

        # sampled=True 表示本轮是现场抽签（未指定 template_id），
        # 每次调用都可能抽到不同模板。前端据此提示用户，避免误认为结果不稳定。
        sampled = not template_id
        template = self.resolve_template_for_preview(
            session_type, template_id, draft_templates=draft_templates
        )
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
        # 流水能力已判定可用时必须给出时间取值。
        if capabilities.get(CAP_PLATFORM_HISTORY):
            values[PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME] = "未知"

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
        # 运行期会用 build_instruction_text 补 [最终指令] 标题，
        # 预览必须同源处理，否则裸文本模板的预览与真实输入不一致。
        rendered = build_instruction_text(rendered)
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

        # 运行期会在用户消息之后追加动态上下文块（模板未含对应占位符时），
        # 预览必须一并展示，否则"完整提示词"的说法与实际输入不符。
        llm_service = getattr(self.plugin, "llm_service", None)
        dynamic_text = ""
        if llm_service is not None:
            dynamic_text = llm_service.build_dynamic_context_text(
                unanswered_count,
                {"_session_type": session_type},
                include_time=PLACEHOLDER_CURRENT_TIME not in raw_text,
                include_unanswered=PLACEHOLDER_UNANSWERED_COUNT not in raw_text,
            )
        if dynamic_text:
            sections.append(
                {
                    "kind": "extra",
                    "label": "动态上下文（随消息追加）",
                    "source": "llm_service",
                    "text": dynamic_text,
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
        draft_templates: Any = None,
    ) -> dict[str, Any]:
        """真实跑 N 次抽样，返回分布与偏差提示。

        用途是让用户直观看到权重是否合理：如果某两条实际占比接近，
        说明它们的权重差在体感上毫无意义，需要重新分配。
        """
        rounds = max(
            1, min(int(rounds or SIMULATE_DEFAULT_ROUNDS), SIMULATE_MAX_ROUNDS)
        )
        settings, _ = self._resolve_settings(draft_templates)
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
        # 归一化允许 id 为空或重复，按 id 聚合会让同名条目互相覆盖，
        # 因此统计与期望值都按池内索引对齐，id 只作为展示字段。
        # 映射用对象身份而非 id：pick_template 返回的是池内同一引用。
        counts: list[int] = [0] * len(pool)
        index_of = {id(item): index for index, item in enumerate(pool)}
        for _ in range(rounds):
            picked = resolve_active_template(
                {PROMPT_SETTINGS_KEY: settings},
                session_type=session_type,
                weighted=True,
                rng=rng,
            )
            if not picked:
                continue
            index = index_of.get(id(picked))
            if index is not None:
                counts[index] += 1

        probabilities = compute_probabilities(pool)
        results = []
        for index, item in enumerate(pool):
            actual = counts[index] / rounds if rounds else 0.0
            results.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": item.get("name", ""),
                    "weight": int(item.get("weight") or 0),
                    "expected": probabilities[index]["probability"],
                    "actual": actual,
                    "count": counts[index],
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
