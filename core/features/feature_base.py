"""Feature 基类。

新增功能只需继承本类并实现 register，可选实现 config_schema。
"""

from __future__ import annotations

from typing import Any


class BaseFeature:
    """所有功能的基类。"""

    name: str = "unnamed_feature"

    def register(self, registry: Any) -> None:  # pragma: no cover - 子类实现
        """向注册表登记本功能的 Stage。"""
        raise NotImplementedError

    def config_schema(self) -> dict[str, Any] | None:
        """声明本功能附加的配置 schema（可选）。"""
        return None

    def default_config(self) -> dict[str, Any]:
        """声明本功能的默认配置（可选）。"""
        return {}
