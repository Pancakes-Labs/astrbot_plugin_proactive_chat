"""清洗不合规的 LLM 输出。

主动消息链路上会直接泄漏给用户的两类脏内容都集中在这里处理：

1. 文本式工具调用标记（DSML 等 XML 风格方言，定界符为全角竖线 U+FF5C）；
2. JS 对象字符串化残留 [object Object]。

DSML 由 DeepSeek 系 chat template 产生，正常应由推理栈的 tool call parser
转换成原生 tool_calls 字段；上游缺失 parser 时会原样落进 content。
插件不执行这些“伪调用”，只在 send_message_to_user 的载荷可解析时把
正文还原回来，其余标记一律剥离。

DSML 可能被 max_tokens 截断，也可能被下游按换行切碎，所以这里用标签配平
扫描而不是单条非贪婪正则：配对失败时一路丢弃到文本末尾。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 全角竖线是 DeepSeek 特殊 token 的原生定界符；ASCII 竖线一并覆盖，
# 因为部分中继层会把特殊 token 规范化成半角。
_BAR = r"[｜|]"

# DSML 标签：<｜dsml｜ tool_calls>、</｜dsml｜ parameter> 等。
_DSML_TOKEN_RE = re.compile(
    rf"<(?P<closing>/)?\s*{_BAR}\s*dsml\s*{_BAR}[^<>]*>",
    re.IGNORECASE,
)

# 其余特殊 token：<｜tool▁calls▁begin｜>、<｜dsml｜ tool_calls> 等。
# 竖线之后可能还跟一段内容（如 tool_calls），整体到最近的 > 为止。
# 这些不构成闭合配对，只剥离标签本身，避免误伤后面的正文。
_SPECIAL_TOKEN_RE = re.compile(rf"<(?:/)?\s*{_BAR}[^<>]*{_BAR}[^<>]*>")

_SEND_INVOKE_RE = re.compile(
    r"invoke\s+name\s*=\s*[\"']send_message_to_user[\"']",
    re.IGNORECASE,
)

_MESSAGES_PARAM_RE = re.compile(
    rf"parameter\s+name\s*=\s*[\"']messages[\"'][^<>]*>(?P<body>.*?)"
    rf"</\s*{_BAR}\s*dsml\s*{_BAR}\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

_OBJECT_PLACEHOLDER_RE = re.compile(r"\[\s*object\s+Object\s*\]", re.IGNORECASE)

# 载荷递归上限：防止畸形结构导致深层遍历或超长文本。
_MAX_PAYLOAD_DEPTH = 8
_MAX_PAYLOAD_ITEMS = 50


def contains_tool_markup(text: Any) -> bool:
    """判断文本中是否存在特殊 token 形式的标记。"""
    if not isinstance(text, str) or "<" not in text:
        return False
    return bool(_SPECIAL_TOKEN_RE.search(text))


def _iter_dsml_regions(text: str) -> list[tuple[int, int, str]]:
    """返回所有 DSML 区域 (start, end, raw)。

    从起始标签开始按标签配对扫描深度，归零即视为区域结束；
    一直不归零说明输出被截断，此时丢弃到文本末尾。
    孤立的结束标签只剔除标签本身，不吞掉后续正文。
    """
    regions: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        match = _DSML_TOKEN_RE.search(text, cursor)
        if match is None:
            break
        if match.group("closing"):
            regions.append((match.start(), match.end(), match.group(0)))
            cursor = match.end()
            continue

        depth = 1
        end = len(text)
        for token in _DSML_TOKEN_RE.finditer(text, match.end()):
            depth += -1 if token.group("closing") else 1
            if depth <= 0:
                end = token.end()
                break
        regions.append((match.start(), end, text[match.start() : end]))
        cursor = end
    return regions


def _remove_regions(text: str, regions: list[tuple[int, int, str]]) -> str:
    """按区间剔除内容，区间外文本原样保留。"""
    if not regions:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end, _raw in regions:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _collect_payload_texts(node: Any, out: list[str], depth: int = 0) -> None:
    """从参数载荷中递归收集正文文本。"""
    if depth > _MAX_PAYLOAD_DEPTH or len(out) >= _MAX_PAYLOAD_ITEMS:
        return
    if isinstance(node, str):
        if node.strip():
            out.append(node.strip())
        return
    if isinstance(node, list):
        for item in node:
            _collect_payload_texts(item, out, depth + 1)
        return
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
            return
        for value in node.values():
            if isinstance(value, (list, dict)):
                _collect_payload_texts(value, out, depth + 1)


# HTML 实体形式的引号：拆开拼接，避免被编辑器或注释处理链路转义。
_QUOT_ENTITY = "&" + "quot;"
_NUM_QUOT_ENTITY = "&" + "#34;"
_QUOTE_CHAR = chr(34)


def _load_json_lenient(body: str) -> Any:
    """解析参数载荷，兼容 HTML 实体转义过的引号。"""
    candidates = [
        body,
        body.replace(_QUOT_ENTITY, _QUOTE_CHAR).replace(_NUM_QUOT_ENTITY, _QUOTE_CHAR),
    ]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _extract_payload_texts(region_texts: list[str]) -> list[str]:
    """从 DSML 区域中取出 send_message_to_user 的正文载荷。

    只还原“模型想说的话”，不执行任何工具。载荷无法解析时返回空列表，
    交由调用方按纯标记剥离处理。
    """
    texts: list[str] = []
    for raw in region_texts:
        if not _SEND_INVOKE_RE.search(raw):
            continue
        param = _MESSAGES_PARAM_RE.search(raw)
        if param is None:
            continue
        body = param.group("body").strip()
        if not body:
            continue
        parsed: Any = _load_json_lenient(body)
        if parsed is None:
            continue
        _collect_payload_texts(parsed, texts)
    return texts


def _clean_object_placeholders(text: str) -> str:
    """剥离 JS 对象字符串化残留。"""
    if "object" not in text.lower():
        return text
    return _OBJECT_PLACEHOLDER_RE.sub(" ", text)


def _collapse_blank_lines(text: str) -> str:
    """压缩连续空行并去掉行尾空白，保留原有段落结构。"""
    result: list[str] = []
    pending_blank = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped:
            if pending_blank and result:
                result.append("")
            result.append(stripped)
            pending_blank = False
        else:
            pending_blank = True
    return "\n".join(result)


def clean_llm_text(text: str, *, extract_payload: bool = True) -> str:
    """清洗单段 LLM 输出文本。

    extract_payload 为 True 时尝试把 send_message_to_user 的正文还原回来，
    拼接在清洗后的正文之后；无法还原的内容一律剥离。
    """
    if not isinstance(text, str) or not text:
        return ""

    regions = _iter_dsml_regions(text)
    payload_texts = (
        _extract_payload_texts([raw for _start, _end, raw in regions])
        if extract_payload
        else []
    )

    body = _remove_regions(text, regions)
    body = _SPECIAL_TOKEN_RE.sub("", body)
    body = _clean_object_placeholders(body)
    body = _collapse_blank_lines(body).strip()

    if not payload_texts:
        return body

    joined = "\n".join(payload_texts)
    return f"{body}\n{joined}" if body else joined


def clean_chain_components(chain: list) -> list:
    """清洗消息链中的纯文本组件。

    非文本组件原样保留；文本组件清洗后为空则整段丢弃，
    避免发送空消息。不依赖具体组件类型，靠 text 属性识别。
    """
    cleaned: list = []
    for comp in chain or []:
        text = getattr(comp, "text", None)
        if not isinstance(text, str):
            cleaned.append(comp)
            continue

        new_text = clean_llm_text(text)
        if not new_text.strip():
            continue
        if new_text == text:
            cleaned.append(comp)
            continue
        try:
            cleaned.append(type(comp)(text=new_text))
        except Exception:
            try:
                comp.text = new_text
            except Exception:
                continue
            cleaned.append(comp)
    return cleaned
