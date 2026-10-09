"""领域枚举定义。"""

from __future__ import annotations

from enum import Enum


class HookPoint(Enum):
    """流水线阶段挂载点（稳定扩展契约）。

    每个枚举值代表主动消息流水线中的一个可插拔位置，
    新增功能通过向这些位置注册 Stage 来接入，主干无需改动。
    """

    GATE = "gate"  # 是否允许本轮（启用/免打扰/未回复上限）
    INTENT = "intent"  # 是否真的发言（可由模型自主决定）
    CONTEXT = "context"  # 汇总上下文来源
    PROMPT = "prompt"  # 拼装提示词
    DELIBERATE = "deliberate"  # 调用 LLM（可含链式工具调用/富媒体）
    REVIEW = "review"  # 生成后复核（新消息检测等）
    DELIVER = "deliver"  # 发送（装饰/分段/TTS）
    RECORD = "record"  # 存档与计数
    SCHEDULE = "schedule"  # 安排下一次


class TriggerSource(Enum):
    """主动消息的触发来源。"""

    SCHEDULE = "schedule"  # 持久化定时任务（私聊）
    AUTO_TRIGGER = "auto_trigger"  # 插件启动后的自动触发
    GROUP_IDLE = "group_idle"  # 群聊沉默倒计时
    MANUAL = "manual"  # Web 管理端手动立即触发


class StageResult(Enum):
    """单个 Stage 执行后的走向。"""

    CONTINUE = "continue"  # 继续后续 Stage
    STOP = "stop"  # 正常结束（如被 GATE 拦截）
    ABORT = "abort"  # 异常/放弃本次
    RESCHEDULE = "reschedule"  # 直接安排下一次
