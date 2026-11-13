"""人格解析结果模型。

本模块只承载“最终选中了哪个 AI 人格”这一事实，不依赖任何框架。
解析动作本身由 ContextService.resolve_persona() 完成，
本模型用于在阶段之间传递结论，也便于后续功能复用。

解析优先级（从高到低），与 AstrBot 原生 _ensure_persona 保持一致：
1. plugin_config    插件配置/会话差异覆写里显式指定的人格
2. session_rule     AstrBot 自定义规则（session_service_config.persona_id）
3. conversation     当前对话对象上绑定的人格（/persona 设置）
4. profile          会话绑定的 AstrBot 配置档案中的默认人格
5. default          AstrBot 全局默认人格
6. disabled         被显式配置为不使用人格（[%None]）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 人格来源标识
PERSONA_SOURCE_PLUGIN = "plugin_config"
PERSONA_SOURCE_PROFILE = "profile"
PERSONA_SOURCE_SESSION_RULE = "session_rule"
PERSONA_SOURCE_CONVERSATION = "conversation"
PERSONA_SOURCE_DEFAULT = "default"
PERSONA_SOURCE_BUILTIN = "builtin_fallback"
PERSONA_SOURCE_DISABLED = "disabled"

PERSONA_SOURCE_LABELS: dict[str, str] = {
    PERSONA_SOURCE_PLUGIN: "插件配置",
    PERSONA_SOURCE_PROFILE: "配置档案默认人格",
    PERSONA_SOURCE_SESSION_RULE: "自定义规则",
    PERSONA_SOURCE_CONVERSATION: "当前对话",
    PERSONA_SOURCE_DEFAULT: "默认人格",
    PERSONA_SOURCE_BUILTIN: "内置兜底",
    PERSONA_SOURCE_DISABLED: "显式禁用",
}

# AstrBot 中表示“用户显式取消人格”的特殊标记
PERSONA_NONE_MARKERS = frozenset({"[%none]", "[%None]"})

# 兜底人格提示词（与 AstrBot DEFAULT_PERSONALITY 保持一致）
BUILTIN_FALLBACK_PERSONA_PROMPT = "You are a helpful and friendly assistant."


@dataclass
class PersonaResolution:
    """一次人格解析的最终结论。

    Attributes:
        persona_id: 生效的人格标识，可能为空（走兜底时）。
        persona_name: 人格展示名，用于日志与前端展示。
        system_prompt: 实际注入请求的系统提示词，为空表示解析失败。
        source: 命中的来源标识，取值见本模块 PERSONA_SOURCE_* 常量。
    """

    persona_id: str = ""
    persona_name: str = ""
    system_prompt: str = ""
    source: str = PERSONA_SOURCE_BUILTIN
    # 人格卡自带的示例对话。追加在末尾：作为公共 dataclass。
    begin_dialogs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        """人格来源的中文展示名。"""
        return PERSONA_SOURCE_LABELS.get(self.source, self.source)

    @property
    def display_name(self) -> str:
        """优先展示人格名，缺失时回退 ID，最后回退来源标签。"""
        return self.persona_name or self.persona_id or self.source_label

    @property
    def begin_dialog_count(self) -> int:
        """人格示例对话的条数，供日志与诊断使用。"""
        return len(self.begin_dialogs)

    @property
    def ok(self) -> bool:
        """是否成功取得可用的人格提示词。"""
        return bool(self.system_prompt)

    @property
    def disabled(self) -> bool:
        """是否被显式配置为不使用人格（对应 AstrBot 的 [%None] 语义）。"""
        return self.source == PERSONA_SOURCE_DISABLED

    def as_metadata(self) -> dict[str, str]:
        """转换为可序列化的元数据，供遥测与调试透传。"""
        return {
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "persona_source": self.source,
            "persona_source_label": self.source_label,
            "persona_begin_dialog_count": str(self.begin_dialog_count),
        }


__all__ = [
    "BUILTIN_FALLBACK_PERSONA_PROMPT",
    "PERSONA_NONE_MARKERS",
    "PERSONA_SOURCE_BUILTIN",
    "PERSONA_SOURCE_CONVERSATION",
    "PERSONA_SOURCE_DEFAULT",
    "PERSONA_SOURCE_DISABLED",
    "PERSONA_SOURCE_LABELS",
    "PERSONA_SOURCE_PLUGIN",
    "PERSONA_SOURCE_PROFILE",
    "PERSONA_SOURCE_SESSION_RULE",
    "PersonaResolution",
]
