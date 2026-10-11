"""流水线引擎层。

提供 Stage 协议、HookPoint 注册与执行器，负责按稳定骨架驱动一次主动消息。
本层只依赖 domain，不直接依赖具体服务和框架。
"""

from .feature_registry import FeatureRegistry
from .hook_registry import HookRegistry
from .pipeline_runner import PipelineRunner
from .stage_protocol import BaseStage, Stage

__all__ = [
    "FeatureRegistry",
    "HookRegistry",
    "PipelineRunner",
    "BaseStage",
    "Stage",
]
