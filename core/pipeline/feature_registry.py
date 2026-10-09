"""Feature 注册与装配。

Feature 是拼装的最小单元：一个功能对应一个 Feature，
在 register 中向 HookRegistry 注册 Stage，并可选提供配置 schema。
"""

from __future__ import annotations

from typing import Any

from .hook_registry import HookRegistry


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
        """汇总所有 Feature 声明的配置 schema（供未来动态合并）。"""
        merged: dict[str, Any] = {}
        for feature in self.features:
            getter = getattr(feature, "config_schema", None)
            if not callable(getter):
                continue
            schema = getter()
            if isinstance(schema, dict):
                merged.update(schema)
        return merged
