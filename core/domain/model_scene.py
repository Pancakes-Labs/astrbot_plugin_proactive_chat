"""模型场景与模型引用。

一个"场景"代表一类 LLM 用途（主动消息生成、轻量判定、图片理解），
每个场景可独立指定 Provider 与模型，并配一条有序的回退链。

链上每个元素编码为 "provider_id::model" 字符串：
- 顺序即优先级，纯文本一行一项，任何编辑器与手改文件语义一致；
- model 可省略，此时使用该 Provider 自身的默认模型；
- provider_id 与模型名都不含 "::"，因此切分无歧义。
"""

from __future__ import annotations

from dataclasses import dataclass

# 模型引用分隔符：provider_id 与 model 的组合键。
REF_SEPARATOR = "::"

# 回退链在配置里的字段名（场景对象内部）。
FALLBACK_CHAIN_KEY = "fallback_chain"
PROVIDER_ID_KEY = "provider_id"
MODEL_KEY = "model"
SCENES_KEY = "scenes"
FAILURE_POLICY_KEY = "failure_policy"


class ModelScene:
    """场景标识。

    用普通类而非 StrEnum：需要兼容 Python 3.10 且要允许任意字符串场景名
    平滑降级，枚举成员只作为"已知场景"的规范名使用。
    """

    # 主动消息主生成
    PROACTIVE = "proactive"
    # 轻量判定（是否发言、二次筛选等，供后续使用）
    DECISIONS = "decisions"
    # 图片理解 / 生成类（供后续使用）
    VISION = "vision"

    # 所有内置场景，供配置装配与校验遍历。
    ALL: tuple[str, ...] = (PROACTIVE, DECISIONS, VISION)

    # 常见别名 -> 规范场景名。用户或下游可能写成单数或其他叫法。
    _ALIASES = {
        "proactive": PROACTIVE,
        "proactive_chat": PROACTIVE,
        "chat": PROACTIVE,
        "main": PROACTIVE,
        "decision": DECISIONS,
        "decide": DECISIONS,
        "judge": DECISIONS,
        "judgement": DECISIONS,
        "classify": DECISIONS,
        "vision": VISION,
        "image": VISION,
        "multimodal": VISION,
        "ocr": VISION,
    }

    @classmethod
    def normalize(cls, value: object, default: str = PROACTIVE) -> str:
        """把任意场景写法归一为规范名；无法识别时返回 default。"""
        if value is None:
            return default
        text = str(value).strip().lower()
        if not text:
            return default
        return cls._ALIASES.get(text, text)


@dataclass(frozen=True)
class ModelRef:
    """一个 Provider + 模型的组合引用。

    provider_id 为空表示"跟随会话当前 Provider"，用于兜底候选。
    model 为空表示"使用该 Provider 的默认模型"。
    """

    provider_id: str = ""
    model: str = ""

    @property
    def is_empty(self) -> bool:
        """两个字段都为空时没有任何指向意义。"""
        return not self.provider_id and not self.model

    @property
    def dedupe_key(self) -> str:
        """去重键。

        必须把 model 纳入：同一个 Provider 换用轻量模型是最常见的降级手段，
        只按 provider_id 去重会把这种候选误删。
        """
        return f"{self.provider_id}{REF_SEPARATOR}{self.model}"


def parse_model_ref(value: object) -> ModelRef | None:
    """解析 "provider_id::model" / "provider_id" / "::model" 三种写法。

    返回 None 表示该值无法构成有效引用，调用方应跳过并告警。
    """
    if isinstance(value, ModelRef):
        return None if value.is_empty else value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if REF_SEPARATOR in text:
        provider_id, model = text.split(REF_SEPARATOR, 1)
        ref = ModelRef(provider_id=provider_id.strip(), model=model.strip())
    else:
        ref = ModelRef(provider_id=text, model="")

    return None if ref.is_empty else ref


def format_model_ref(ref: ModelRef) -> str:
    """把 ModelRef 编码回字符串；model 为空时只输出 provider_id。"""
    if not ref.model:
        return ref.provider_id
    if not ref.provider_id:
        return f"{REF_SEPARATOR}{ref.model}"
    return f"{ref.provider_id}{REF_SEPARATOR}{ref.model}"


def parse_model_ref_list(values: object) -> tuple[list[ModelRef], list[str]]:
    """解析回退链列表，返回 (有效引用列表, 无法识别的原始值列表)。

    去重按 dedupe_key 进行且保持顺序：用户可能会在链里重复填同一项。
    """
    if isinstance(values, str):
        # 兼容用户把链写成逗号分隔字符串的情况，减少一类必然踩到的配置错误。
        raw_items: list[object] = list(values.replace("，", ",").split(","))
    elif isinstance(values, (list, tuple)):
        raw_items = list(values)
    else:
        return [], []

    refs: list[ModelRef] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw in raw_items:
        if isinstance(raw, str) and not raw.strip():
            # 空行是纯文本编辑器留下的常见残留，静默跳过。
            continue
        ref = parse_model_ref(raw)
        if ref is None:
            invalid.append(str(raw))
            continue
        if ref.dedupe_key in seen:
            continue
        seen.add(ref.dedupe_key)
        refs.append(ref)

    return refs, invalid


__all__ = [
    "FAILURE_POLICY_KEY",
    "FALLBACK_CHAIN_KEY",
    "MODEL_KEY",
    "ModelRef",
    "ModelScene",
    "PROVIDER_ID_KEY",
    "REF_SEPARATOR",
    "SCENES_KEY",
    "format_model_ref",
    "parse_model_ref",
    "parse_model_ref_list",
]
