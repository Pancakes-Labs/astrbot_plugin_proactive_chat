"""Feature 注册与装配。

Feature 是拼装的最小单元：一个功能对应一个 Feature，
在 register 中向 HookRegistry 注册 Stage，并可选声明自带配置。
"""

from __future__ import annotations

from typing import Any

from .hook_registry import HookRegistry
from .schema_merge import merge_defaults_into, merge_schema_into


class FeatureRegistry:
    """收集并装配所有 Feature。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.hooks = HookRegistry()
        self.features: list[Any] = []

    def register_feature(self, feature: Any) -> None:
        """注册单个 Feature 并触发其 Stage 注册。"""
        self.features.append(feature)
        feature.register(self)

    def register_features(self, features: list[Any]) -> None:
        for feature in features:
            self.register_feature(feature)

    def add_stage(self, hook, stage, priority: int | None = None) -> None:
        """供 Feature 调用的便捷注册入口。

        Args:
            hook: HookPoint 枚举值。
            stage: Stage 实例。
            priority: 可选，覆盖 Stage 自身声明的优先级。
        """
        if priority is not None:
            try:
                stage.priority = priority
            except Exception:
                pass
        if getattr(stage, "hook", None) is None:
            try:
                stage.hook = hook
            except Exception:
                pass
        self.hooks.register(stage)

    def collected_config_schema(self) -> dict[str, Any]:
        """汇总所有 Feature 声明的配置 schema。

        键支持点号路径（如 friend_settings.prompt_templates），启动时会由
        配置装配服务合并进 _conf_schema.json 对应的嵌套位置。
        """
        merged: dict[str, Any] = {}
        for feature in self.features:
            getter = getattr(feature, "config_schema", None)
            if not callable(getter):
                continue
            try:
                schema = getter()
            except Exception:
                # 单个 Feature 声明失败不应阻断整体装配。
                continue
            if isinstance(schema, dict) and schema:
                merge_schema_into(merged, schema)
        return merged

    def collected_default_config(self) -> dict[str, Any]:
        """汇总所有 Feature 声明的默认配置（键同样支持点号路径）。"""
        merged: dict[str, Any] = {}
        for feature in self.features:
            getter = getattr(feature, "default_config", None)
            if not callable(getter):
                continue
            try:
                defaults = getter()
            except Exception:
                continue
            if isinstance(defaults, dict) and defaults:
                merge_defaults_into(merged, defaults)
        return merged


__all__ = ["FeatureRegistry"]
