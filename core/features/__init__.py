"""功能拼装层：每个功能自包含，通过 register 向流水线挂载 Stage。"""

from .feature_base import BaseFeature

__all__ = ["BaseFeature"]
