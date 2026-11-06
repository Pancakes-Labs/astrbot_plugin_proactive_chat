"""领域层：纯数据模型与纯函数，不依赖任何框架。

本层是全项目的稳定契约，任何 services / features / adapters 都只能依赖它，
而它自身不反向依赖任何上层模块，也不导入 astrbot。
"""

from .context_block import BlockKind, ContextBlock, ContextBundle
from .delivery_report import DeliveryReport
from .domain_enums import HookPoint, StageResult, TriggerSource
from .llm_outcome import LlmOutcome, ToolCallRecord
from .persona_resolution import (
    PERSONA_SOURCE_BUILTIN,
    PERSONA_SOURCE_CONVERSATION,
    PERSONA_SOURCE_DEFAULT,
    PERSONA_SOURCE_DISABLED,
    PERSONA_SOURCE_PLUGIN,
    PERSONA_SOURCE_PROFILE,
    PERSONA_SOURCE_SESSION_RULE,
    PersonaResolution,
)
from .prompt_block import PromptBlock, PromptBundle
from .prompt_templates import (
    LEGACY_PROMPT_KEY,
    MIGRATED_TEMPLATE_ID,
    PROMPT_TEMPLATES_KEY,
    build_templates_from_legacy,
    normalize_templates,
    resolve_active_prompt,
    resolve_active_template,
)
from .request_context import ProactiveContext
from .session_config import SessionConfig
from .session_key import (
    SessionKey,
    contains_group_marker,
    is_friend_type,
    is_group_type,
    is_group_umo,
)

__all__ = [
    "BlockKind",
    "ContextBlock",
    "ContextBundle",
    "DeliveryReport",
    "HookPoint",
    "LEGACY_PROMPT_KEY",
    "LlmOutcome",
    "MIGRATED_TEMPLATE_ID",
    "PERSONA_SOURCE_BUILTIN",
    "PERSONA_SOURCE_CONVERSATION",
    "PERSONA_SOURCE_DEFAULT",
    "PERSONA_SOURCE_DISABLED",
    "PERSONA_SOURCE_PLUGIN",
    "PERSONA_SOURCE_PROFILE",
    "PERSONA_SOURCE_SESSION_RULE",
    "PersonaResolution",
    "PROMPT_TEMPLATES_KEY",
    "ProactiveContext",
    "PromptBlock",
    "PromptBundle",
    "SessionConfig",
    "SessionKey",
    "StageResult",
    "ToolCallRecord",
    "TriggerSource",
    "build_templates_from_legacy",
    "contains_group_marker",
    "is_friend_type",
    "is_group_type",
    "is_group_umo",
    "normalize_templates",
    "resolve_active_prompt",
    "resolve_active_template",
]
