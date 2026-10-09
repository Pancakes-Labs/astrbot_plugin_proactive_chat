"""HookPoint 阶段注册表。

按 HookPoint 收集 Stage，并在执行前按 priority 稳定排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.domain_enums import HookPoint


@dataclass
class HookRegistry:
    """按挂载点组织 Stage 的注册表。"""

    _stages: dict[HookPoint, list] = field(default_factory=dict)

    # 稳定的执行顺序，与骨架设计一致。
    ORDER: tuple[HookPoint, ...] = (
        HookPoint.GATE,
        HookPoint.INTENT,
        HookPoint.CONTEXT,
        HookPoint.PROMPT,
        HookPoint.DELIBERATE,
        HookPoint.REVIEW,
        HookPoint.DELIVER,
        HookPoint.RECORD,
        HookPoint.SCHEDULE,
    )

    def register(self, stage) -> None:
        """把某个 Stage 注册到它声明的 HookPoint。"""
        self._stages.setdefault(stage.hook, []).append(stage)

    def stages_for(self, hook: HookPoint) -> list:
        """返回某挂载点下按 priority 稳定排序后的 Stage 列表。"""
        stages = list(self._stages.get(hook, []))
        # 稳定排序：priority 相同则保持注册顺序。
        stages.sort(key=lambda s: getattr(s, "priority", 100))
        return stages

    def ordered_stages(self) -> list:
        """按骨架顺序返回全部 Stage。"""
        result: list = []
        for hook in self.ORDER:
            result.extend(self.stages_for(hook))
        return result
