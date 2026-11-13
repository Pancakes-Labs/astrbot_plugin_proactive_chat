"""模型路由服务：解析场景候选链、载入 Provider。

职责边界：
- 从会话配置里读出某个场景的候选链（场景显式配置 -> 回退链 -> 会话默认）；
- 把候选载入为可调用的 Provider 实例；
- 提供该场景的失败处置策略（超时预算、重试次数等）。

本服务不负责发起请求，也不处理失败归因，那两件事分别由
LlmService（重试循环）与 llm_failure（归因与策略）承担。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..domain.config_keys import MODEL_SETTINGS_KEY
from ..domain.model_scene import (
    FAILURE_POLICY_KEY,
    FALLBACK_CHAIN_KEY,
    MODEL_KEY,
    PROVIDER_ID_KEY,
    SCENES_KEY,
    ModelRef,
    format_model_ref,
    parse_model_ref_list,
)

# 同一候选重试的固定退避秒数。
RETRY_BACKOFF_SECONDS = 5.0

# 单个候选的默认超时。回退链会让最坏总耗时成倍增长，必须有单点上界。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0

# 同一候选的默认重试次数（仅瞬时故障使用）。
DEFAULT_RETRY_SAME_PROVIDER_TIMES = 1

# 候选来源标签，用于日志与遥测归因。
SOURCE_SCENE = "scene"
SOURCE_FALLBACK = "fallback"
SOURCE_SESSION = "session_default"


@dataclass(frozen=True)
class ModelCandidate:
    """候选链上的一个条目。"""

    ref: ModelRef
    # 该候选来自哪里：场景显式配置 / 回退链 / 会话默认。
    source: str = SOURCE_SCENE
    # 在最终候选链中的下标，便于日志定位。
    index: int = 0

    @property
    def provider_id(self) -> str:
        return self.ref.provider_id

    @property
    def model(self) -> str:
        return self.ref.model

    @property
    def is_session_default(self) -> bool:
        """是否为"跟随会话当前 Provider"的兜底候选。"""
        return self.source == SOURCE_SESSION


@dataclass(frozen=True)
class ModelPolicy:
    """场景的失败处置策略。"""

    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    retry_same_provider_times: int = DEFAULT_RETRY_SAME_PROVIDER_TIMES
    fallback_on_content_refusal: bool = False


class ModelRouter:
    """按场景解析模型候选链并载入 Provider。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    @staticmethod
    def _model_settings(session_config: Any) -> dict[str, Any]:
        """读取 model_settings 段，容错为非 dict 时返回空。"""
        if not isinstance(session_config, dict):
            return {}
        value = session_config.get(MODEL_SETTINGS_KEY)
        return value if isinstance(value, dict) else {}

    def scene_settings(self, scene: str, session_config: Any) -> dict[str, Any]:
        """读取单个场景的配置节点。"""
        scenes = self._model_settings(session_config).get(SCENES_KEY)
        if not isinstance(scenes, dict):
            return {}
        node = scenes.get(scene)
        return node if isinstance(node, dict) else {}

    def get_policy(self, session_config: Any) -> ModelPolicy:
        """读取失败处置策略并做数值兜底。

        非法值一律回落到默认值而不是抛错：配置是用户手填的，
        一个填错的超时值不应该让整条主动消息链路不可用。
        """
        raw = self._model_settings(session_config).get(FAILURE_POLICY_KEY)
        policy = raw if isinstance(raw, dict) else {}

        timeout = self._positive_float(
            policy.get("request_timeout_seconds"), DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        retries = self._non_negative_int(
            policy.get("retry_same_provider_times"),
            DEFAULT_RETRY_SAME_PROVIDER_TIMES,
        )
        return ModelPolicy(
            request_timeout_seconds=timeout,
            retry_same_provider_times=retries,
            fallback_on_content_refusal=bool(
                policy.get("fallback_on_content_refusal", False)
            ),
        )

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    @staticmethod
    def _non_negative_int(value: Any, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number >= 0 else default

    # ------------------------------------------------------------------
    # 候选链
    # ------------------------------------------------------------------
    def resolve_chain(
        self,
        scene: str,
        session_config: Any,
        *,
        session_id: str = "",
    ) -> list[ModelCandidate]:
        """构建候选链。

        顺序：场景显式配置 -> 回退链逐项 -> 会话当前 Provider 兜底。
        按 (provider_id, model) 组合去重并保持顺序，因此"同一 Provider 换轻量
        模型"这类降级不会被误删。

        session_id 只用于日志定位；实际解析会话默认 Provider 发生在载入阶段。
        """
        node = self.scene_settings(scene, session_config)

        # 1. 场景显式配置：provider_id 与 model 都是可选的。
        #    只填 model（::model）表示"用会话默认 Provider + 指定模型"。
        scene_ref = ModelRef(
            provider_id=str(node.get(PROVIDER_ID_KEY) or "").strip(),
            model=str(node.get(MODEL_KEY) or "").strip(),
        )

        ordered: list[tuple[ModelRef, str]] = []
        if not scene_ref.is_empty:
            ordered.append((scene_ref, SOURCE_SCENE))

        # 2. 回退链：解析失败的条目打一次告警，避免用户填错后静默不生效。
        chain_refs, invalid = parse_model_ref_list(node.get(FALLBACK_CHAIN_KEY))
        if invalid:
            logger.warning(
                f"[主动消息] 场景 {scene} 的回退链存在无法识别的条目，已跳过喵: "
                f"{', '.join(invalid)}。格式应为 provider_id 或 provider_id::model。"
            )
        for ref in chain_refs:
            ordered.append((ref, SOURCE_FALLBACK))

        # 3. 兜底：跟随会话当前 Provider。空 ref 表示两者都不覆盖。
        ordered.append((ModelRef(), SOURCE_SESSION))

        candidates: list[ModelCandidate] = []
        seen: set[str] = set()
        for ref, source in ordered:
            key = ref.dedupe_key
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                ModelCandidate(ref=ref, source=source, index=len(candidates))
            )

        if session_id:
            logger.debug(
                f"[主动消息] 场景 {scene} 候选链（{session_id}）喵: "
                + " -> ".join(
                    f"[{item.source}]{format_model_ref(item.ref) or '会话默认'}"
                    for item in candidates
                )
            )
        return candidates

    # ------------------------------------------------------------------
    # Provider 载入
    # ------------------------------------------------------------------
    async def load_provider(
        self, candidate: ModelCandidate, *, session_id: str = ""
    ) -> Any:
        """把候选载入为 Provider 实例，失败返回 None。"""
        if candidate.is_session_default or not candidate.provider_id:
            return await self._load_session_provider(session_id)

        plugin = self.plugin
        manager = getattr(getattr(plugin, "context", None), "provider_manager", None)
        if manager is None:
            logger.warning("[主动消息] 缺少提供商管理器，无法按 ID 获取 Provider 喵。")
            return None

        getter = getattr(manager, "get_provider_by_id", None)
        if not callable(getter):
            logger.warning("[主动消息] 提供商管理器未提供按 ID 获取提供商的能力喵。")
            return None

        try:
            # 统一用 awaitable 适配。
            provider = await maybe_await(getter(candidate.provider_id))
        except Exception as e:
            logger.warning(
                f"[主动消息] 按 ID 获取 Provider 失败喵（{candidate.provider_id}）: {e}"
            )
            return None

        if provider is None:
            logger.warning(
                f"[主动消息] 配置的 Provider 不存在喵: {candidate.provider_id}。"
                "请检查模型设置里的服务提供商是否已被删除或改名。"
            )
        return provider

    async def _load_session_provider(self, session_id: str) -> Any:
        """取该会话当前使用的对话 Provider。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None

        # 优先用异步版本：同步版 get_using_provider 在上游已标记 deprecated。
        async_getter = getattr(context, "get_using_provider_async", None)
        if callable(async_getter):
            try:
                return await maybe_await(async_getter(session_id or None))
            except Exception as e:
                logger.warning(f"[主动消息] 异步获取会话 Provider 失败喵: {e}")

        legacy_getter = getattr(context, "get_using_provider", None)
        if callable(legacy_getter):
            try:
                return await maybe_await(legacy_getter(umo=session_id or None))
            except Exception as e:
                logger.warning(f"[主动消息] 回退获取会话 Provider 失败喵: {e}")

        return None

    async def resolve(
        self, scene: str, session_config: Any, *, session_id: str = ""
    ) -> tuple[Any | None, ModelCandidate | None]:
        """便捷入口：返回首个可载入的 (Provider, 候选)。"""
        for candidate in self.resolve_chain(
            scene, session_config, session_id=session_id
        ):
            provider = await self.load_provider(candidate, session_id=session_id)
            if provider is not None:
                return provider, candidate
        return None, None


async def maybe_await(value: Any) -> Any:
    """兼容同步/异步返回值，统一以 await 语义取结果。"""
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_RETRY_SAME_PROVIDER_TIMES",
    "ModelCandidate",
    "ModelPolicy",
    "ModelRouter",
    "RETRY_BACKOFF_SECONDS",
    "SOURCE_FALLBACK",
    "SOURCE_SCENE",
    "SOURCE_SESSION",
    "maybe_await",
]
