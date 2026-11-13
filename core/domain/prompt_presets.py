"""内置提示词预设池（出厂模板）。

这里是产品的「默认体验」：每套模板只负责一种主动开口的动机，运行时按权重
随机抽一套，让同一个 bot 在不同时间呈现不同的姿态，而不是永远顺着上下文接话。


PRESET_REVISION 变更后，启动期会把全部用户（含改过模板的用户）的模板整组
替换为当前预设，只依赖迁移前的配置快照与提示词汇总文件做兜底。
"""

from __future__ import annotations

from typing import Any

from .prompt_templates import (
    DEFAULT_TEMPLATE_KEY,
    SCOPE_FRIEND,
    SCOPE_GROUP,
    TEMPLATE_KEY_FIELD,
)

# 预设版本号。修改任何模板正文或权重都要递增，否则老用户不会收到新预设。
PRESET_REVISION = 1
# 记录在 prompt_settings 下的版本键，用于判断是否需要强推。
PRESET_REVISION_KEY = "preset_revision"

# 两段式段头。
HEADER_INTENT = "[这次我想]"
HEADER_FINAL = "[最终指令]"


def _compose(intent: str, final: str) -> str:
    """按两段式拼装正文。

    正文始终以 HEADER_INTENT 开头，满足 build_instruction_text() 的
    「首字符为 [ 即不再补标题」判定。
    """
    return f"{HEADER_INTENT}\n{intent.strip()}\n\n{HEADER_FINAL}\n{final.strip()}"


# ----------------------------------------------------------------------
# 私聊池：一对一，对方是在意的人。权重合计 100。
# 只有 10% 的权重留给「突然记起的旧话头」，其余全部不依赖上文。
# ----------------------------------------------------------------------
_FRIEND_PRESET: tuple[tuple[str, str, int, str, str], ...] = (
    (
        "friend_share_moment",
        "当下碎片分享",
        22,
        "结合当前时间，跟对方随口分享一件我此刻正在做的事、刚弄完的小事，或者眼前看到的一幕。",
        "直接说我自己的事，不需要在结尾反问对方在干嘛。",
    ),
    (
        "friend_random_thought",
        "突发奇想与脑洞",
        16,
        "说出我刚刚突然冒出来的一个奇怪念头、冷知识、荒诞假设，或者发呆时想到的脑洞。",
        "就像脑子里突然闪过这个想法、顺手发给对方一样。"
        "如果涉及具体的数据、年份或人名而我不确定，就说成自己的猜想或者"
        "“好像听说过”，不要编造确切的数字。",
    ),
    (
        "friend_self_mood",
        "自我状态与轻度吐槽",
        14,
        "结合当前时间，向对方表达我此刻的生理或情绪状态（比如犯困、发呆、嘴馋、无聊，"
        "或者对某件小事的轻微吐槽）。",
        "重点在于抒发我自己的感受，不要去关心或追问对方。",
    ),
    (
        "friend_curious_question",
        "真诚好奇的提问",
        14,
        "向对方提出一个我真正好奇的具体问题：关于对方的个人偏好、"
        "生活习惯、过往经历，或者对某件事的看法。",
        "先简短说说我为什么突然好奇这个，然后直接提问。一次只问一个问题。",
    ),
    (
        "friend_poke",
        "纯粹刷存在感",
        12,
        "没有什么非说不可的正事，只是突然想戳对方一下、刷个存在感。",
        "用简短、符合我性格的方式喊对方一声，发个简短的感叹或无意义的小动静。"
        "字数越短越好。",
    ),
    (
        "friend_recall_topic",
        "突然记起的旧话头",
        10,
        "从我们之前的对话里挑一件让我印象深刻的事，像“事后突然又想起来”一样，"
        "补充一句当时没说的新想法，或者追问一下那件事的后续进展。",
        "不要复述或总结已经聊过的内容。",
    ),
    (
        "friend_casual_checkin",
        "随口问候对方当下",
        7,
        "结合当前时间点（如清晨、饭点、工作时段或深夜），自然地猜测一下对方现在在忙什么、"
        "状态怎么样，随口问一句。",
        "保持轻松随意，不要提及之前的聊天内容。",
    ),
    (
        "friend_express_missing",
        "直接表达想念",
        5,
        "安静了这么久，我有点想念对方，或者觉得没人说话有点冷清。",
        "用完全符合我性格的方式表达一下我想跟对方说话的心情。",
    ),
)


# ----------------------------------------------------------------------
# 群聊池：多人群，群里可能有点冷。权重合计 100。
# 群聊的关键是「有具体靶子、回复门槛低」，不能像主持人那样泛泛发问。
# ----------------------------------------------------------------------
_GROUP_PRESET: tuple[tuple[str, str, int, str, str], ...] = (
    (
        "group_self_rant",
        "个人见闻与吐槽",
        26,
        "像普通群友水群一样，在群里随口丢出一件我刚遇到的小事、刚看到的东西，"
        "或者对某件日常小事的吐槽。",
        "自顾自地说出来即可，不要点名，也不要像主持人一样强行号召大家讨论。",
    ),
    (
        "group_time_vibe",
        "群体状态共鸣",
        21,
        "结合现在的具体时间段，说一句能引起群友当下状态共鸣的感慨"
        "（比如困意、饿意、摸鱼冲动、熬夜或周末状态），",
        "顺带试探一下群里这会儿还有没有活人。",
    ),
    (
        "group_weird_question",
        "脑洞怪问题投石问路",
        17,
        "把我突然想不通的一个奇怪问题、冷知识或脑洞抛进群里。",
        "真诚或者促狭地问问群里有没有懂行的人，或者跟我想的一样的人。"
        "如果涉及具体的数据、年份、人名而我不确定，就说成“我好像记得”或者"
        "“我猜”，不要编造确切的数字。",
    ),
    (
        "group_quick_poll",
        "轻量化二选一投票",
        15,
        "开启一个全新的轻松话题。向群里丢出一个生活化的“二选一”"
        "或者容易引发不同派别争论的小问题（饮食口味、生活习惯、奇葩假设之类的）。",
        "先亮出我自己的鲜明立场，再看群里其他人怎么选。",
    ),
    (
        "group_revive_topic",
        "重拾群内未尽话题",
        12,
        "群里之前聊到一半冷掉的话题，我挑一个最感兴趣的点，"
        "说自己的看法、吐槽，或者追问一句。",
        "不要总结群聊记录，直接切入观点。",
    ),
    (
        "group_bubble",
        "纯粹冒泡炸鱼",
        9,
        "群里太安静了，我只是想出来冒个泡。",
        "用一句简短的话、一声感叹，或者符合我性格的出场方式刷个存在感。"
        "不需要开启复杂话题。",
    ),
)


def _build_entry(
    template_id: str,
    name: str,
    weight: int,
    scope: str,
    intent: str,
    final: str,
) -> dict[str, Any]:
    """构造单条预设模板（与 normalize_template 的输出结构保持一致）。"""
    return {
        # AstrBot 管理面板按 __template_key 匹配模板定义，缺失会拒绝保存。
        TEMPLATE_KEY_FIELD: DEFAULT_TEMPLATE_KEY,
        "id": template_id,
        "name": name,
        "text": _compose(intent, final),
        "enabled": True,
        "weight": weight,
        "scope": scope,
    }


def build_preset_templates() -> list[dict[str, Any]]:
    """返回完整的出厂模板列表（私聊在前，群聊在后，各自保持设计顺序）。"""
    entries: list[dict[str, Any]] = []
    for template_id, name, weight, intent, final in _FRIEND_PRESET:
        entries.append(
            _build_entry(template_id, name, weight, SCOPE_FRIEND, intent, final)
        )
    for template_id, name, weight, intent, final in _GROUP_PRESET:
        entries.append(
            _build_entry(template_id, name, weight, SCOPE_GROUP, intent, final)
        )
    return entries


def preset_signature() -> str:
    """预设指纹，供调试日志与前端标注「当前是否仍是出厂预设」。"""
    parts = [f"v{PRESET_REVISION}"]
    for source in (_FRIEND_PRESET, _GROUP_PRESET):
        for template_id, _name, weight, _intent, _final in source:
            parts.append(f"{template_id}:{weight}")
    return "|".join(parts)


__all__ = [
    "HEADER_FINAL",
    "HEADER_INTENT",
    "PRESET_REVISION",
    "PRESET_REVISION_KEY",
    "build_preset_templates",
    "preset_signature",
]
