"""领域层：纯数据模型与纯函数，不依赖任何框架。

本层是全项目的稳定契约，任何 services / features / adapters 都只能依赖它，
而它自身不反向依赖任何上层模块，也不导入 astrbot。
"""

from .config_keys import (
    CONTEXT_SETTINGS_KEY,
    ENABLED_KEY,
    GROUP_SILENCE_SETTINGS_KEY,
    LEGACY_TYPE_FALLBACK_KEY,
    OUTPUT_FORMAT_CUSTOM_KEY,
    OUTPUT_FORMAT_KEY,
    PERSONA_ID_KEY,
    PROMPT_SETTINGS_KEY,
    SCHEDULE_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
    SESSION_LIST_KEY,
    STARTUP_CHECK_SETTINGS_KEY,
)
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
from .prompt_block import PromptBlock, PromptBundle, PromptExtra
from .prompt_contract import (
    CONTRACT_VERSION,
    ROLE_CONTRACT,
    ROLE_SCENE,
    ROLE_TASK,
    build_prompt_layers,
)
from .prompt_placeholders import (
    PLACEHOLDER_CONVERSATION_TYPE,
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME,
    PLACEHOLDER_PLATFORM_HISTORY_LINES,
    PLACEHOLDER_UNANSWERED_COUNT,
    PLACEHOLDERS,
    build_capabilities,
    contains_placeholder,
    render_placeholders,
    resolve_placeholders,
)
from .prompt_templates import (
    MIGRATED_TEMPLATE_ID,
    OUTPUT_FORMAT_CUSTOM,
    OUTPUT_FORMAT_DEFAULT,
    TEMPLATES_KEY,
    get_output_format,
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
from .tool_markup_cleaner import (
    clean_chain_components,
    clean_llm_text,
    contains_tool_markup,
    extract_payload_text,
)

__all__ = [
    "BlockKind",
    "CONTRACT_VERSION",
    "CONTEXT_SETTINGS_KEY",
    "ContextBlock",
    "ContextBundle",
    "DeliveryReport",
    "ENABLED_KEY",
    "GROUP_SILENCE_SETTINGS_KEY",
    "HookPoint",
    "LEGACY_TYPE_FALLBACK_KEY",
    "LlmOutcome",
    "MIGRATED_TEMPLATE_ID",
    "PERSONA_ID_KEY",
    "PERSONA_SOURCE_BUILTIN",
    "PERSONA_SOURCE_CONVERSATION",
    "PERSONA_SOURCE_DEFAULT",
    "PERSONA_SOURCE_DISABLED",
    "PERSONA_SOURCE_PLUGIN",
    "PERSONA_SOURCE_PROFILE",
    "PERSONA_SOURCE_SESSION_RULE",
    "PLACEHOLDERS",
    "PLACEHOLDER_CONVERSATION_TYPE",
    "PLACEHOLDER_CURRENT_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LINES",
    "PLACEHOLDER_UNANSWERED_COUNT",
    "OUTPUT_FORMAT_CUSTOM",
    "OUTPUT_FORMAT_CUSTOM_KEY",
    "OUTPUT_FORMAT_DEFAULT",
    "OUTPUT_FORMAT_KEY",
    "PROMPT_SETTINGS_KEY",
    "PersonaResolution",
    "ProactiveContext",
    "PromptBlock",
    "PromptBundle",
    "PromptExtra",
    "ROLE_CONTRACT",
    "ROLE_SCENE",
    "ROLE_TASK",
    "SCHEDULE_SETTINGS_KEY",
    "SESSION_DEFAULTS_KEY",
    "SESSION_LIST_KEY",
    "STARTUP_CHECK_SETTINGS_KEY",
    "SessionConfig",
    "SessionKey",
    "StageResult",
    "TEMPLATES_KEY",
    "ToolCallRecord",
    "TriggerSource",
    "build_capabilities",
    "build_prompt_layers",
    "clean_chain_components",
    "clean_llm_text",
    "contains_group_marker",
    "contains_placeholder",
    "contains_tool_markup",
    "extract_payload_text",
    "is_friend_type",
    "is_group_type",
    "get_output_format",
    "is_group_umo",
    "normalize_templates",
    "render_placeholders",
    "resolve_active_prompt",
    "resolve_active_template",
    "resolve_placeholders",
]
