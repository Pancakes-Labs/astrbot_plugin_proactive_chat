"""共享日志脱敏工具。

供「运行日志上传」链路复用，规则对齐 TelemetryManager 中既有的消息/堆栈脱敏口径
（URL 凭据、家目录路径、插件与 site-packages 路径），并额外覆盖键值对形式的凭据字段，
避免真实的 Key / Token 随日志外泄。

脱敏策略宁过度不遗漏：凭据键名命中即整体替换为占位符，
极少数同名非敏感字段会被误伤，可接受。
"""

from __future__ import annotations

import re

# 凭据类字段的词根：键名只要包含其中之一（大小写不敏感）即视为敏感。
# 这里不使用 \b 锚定键名开头，因为 \b 在 "_" 与字母之间不成立，
# 会让 openai_api_key / secret_key / bot_token 这类带前缀的键名完全绕过；
# 改为「可选前后缀 + 词根」，即可同时覆盖 snake_case、camelCase（botToken）、
# kebab-case（X-Amz-Credential）等写法。
_CREDENTIAL_KEYWORDS = (
    r"token|secret|password|passwd|pwd|cookie|authorization|credential|signature|"
    r"session[-_]?id|access[-_]?key|private[-_]?key|api[-_]?key|key"
)
# 键名边界：前后都不能再紧邻字母/数字，避免把半个单词当成键名的一部分。
_CREDENTIAL_NAME_BOUNDARY = r"(?<![A-Za-z0-9])"
_CREDENTIAL_FIELD_PATTERN = (
    rf"{_CREDENTIAL_NAME_BOUNDARY}"
    rf"[A-Za-z0-9_.-]*(?:{_CREDENTIAL_KEYWORDS})[A-Za-z0-9_.-]*"
    rf"(?![A-Za-z0-9])"
)

# URL 中凭据：
# 1. userinfo 形态 scheme://user:pass@host 或 scheme://token@host → 整段替换为 ***@；
#    userinfo 段限制在 authority 内：不跨越 / ? # 与空白，并以贪婪方式匹配到该段
#    最后一个 @（未编码密码可能含 @），避免误伤 ?email=a@b.com 这类普通 URL。
# 2. query 参数：默认遮蔽所有参数值，只放行明确无害的参数白名单。
#    不做键名白名单是因为签名类链接的参数名（X-Amz-Credential / X-Amz-Signature 等）
#    无法穷举，漏一个就等于把签名密钥写进公开链接。
_URL_USERINFO_RE = re.compile(r"\b([\w+-]+://)[^\s/?#]+@")
# 白名单参数：仅做展示/分页用途，取值无敏感性。
_SAFE_QUERY_KEYS = frozenset(
    {
        "page",
        "limit",
        "offset",
        "size",
        "count",
        "lang",
        "locale",
        "format",
        "mode",
        "level",
        "action",
        "type",
        "v",
        "t",
        "ts",
        "q",
    }
)
_URL_QUERY_RE = re.compile(r"(?i)([?&])([^=&\s\"'#]+)=([^&\s\"'#]*)")

# Bearer/Basic 等鉴权方案后的令牌：Bearer <JWT>、Basic <base64> → 保留方案词，令牌 ***。
# 阈值 6 个字符，避免把普通英文词当令牌；JWT（含 . 分隔的 base64url 段）可完整覆盖。
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(bearer|basic|digest|token)\s+([A-Za-z0-9._~+/=-]{6,})"
)

# 带引号值的键值对（JSON/字典形态）：覆盖 "key": "value"、'key': 'value'、
# key: "value" 等写法，仅遮蔽值部分，保留键名便于阅读定位。
# 值以惰性匹配到同名引号闭合（而不是排除两种引号），这样
# {"api_key": "abc'def"} 这种「值内含另一种引号」的写法也能正确脱敏；
# 同时把值限制在同一行内，避免跨行贪婪吞掉后续日志。
_CRED_QUOTED_VALUE_RE = re.compile(
    rf"""(?i)(?P<pre>["']?)(?P<name>{_CREDENTIAL_FIELD_PATTERN})"""
    rf"""(?P<post>["']?)(?P<sep>\s*[:=]\s*)(?P<q>["'])(?P<val>[^\n]*?)(?P=q)"""
)

# 未加引号的冒号形态：api_key: xxx（可读日志常见写法，含全角冒号）。
# 值若以鉴权方案词开头（Bearer *** 已由 _BEARER_TOKEN_RE 处理）则跳过，保留方案词。
_CRED_COLON_VALUE_RE = re.compile(
    rf"(?i)(?P<name>{_CREDENTIAL_FIELD_PATTERN})(?P<sep>\s*[:：]\s*)"
    rf"(?P<val>(?!(?:bearer|basic|digest|token)\b)[^\s\"'&,，。；：）)】\]]+)"
)

# 裸键值对形态：key=value（值取到空白/引号/分隔符为止）。
_CRED_BARE_VALUE_RE = re.compile(
    rf"(?i)(?P<name>{_CREDENTIAL_FIELD_PATTERN})(?P<sep>\s*=\s*)"
    rf"(?P<val>[^\s\"'&,]+)"
)

# 家目录路径：隐藏宿主机用户名。
_HOME_PATH_RE = re.compile(r"/(?:home|Users|root)/[^/\s]+/")
_ROOT_PATH_RE = re.compile(r"/root/")
_WIN_HOME_PATH_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\")

# AstrBot 统一会话标识（UMO）：platform:MessageType:target，
# MessageType 覆盖 GroupMessage/FriendMessage/PrivateMessage/GuildMessage
# 及 DirectMessage/OtherMessage 等变体；可选尾部「 (备注名)」一并吞掉。
_UMO_SESSION_RE = re.compile(
    r"\b[\w-]+:[A-Za-z]+Message:[^\s'\"，。；）)】\]]+(?:\s*\([^)]*\))?"
)

# 会话日志字符串（get_session_log_str 的输出格式）：私聊/群聊/未知类型 <target> (<备注名>)。
# - target 不限定 ASCII：部分平台用 UUID / 邮箱 / 手机号当用户标识；
# - 备注名由用户自定义，可能含括号，故允许一层嵌套括号，避免名称在首个 ")" 处截断
#   而把后缀留在导出文本里；同层不使用贪婪匹配，避免把该行后续内容一并吞掉。
_SESSION_LOG_STR_RE = re.compile(
    r"(?:私聊|群聊|未知类型)\s+[^\s()（）]+"
    r"(?:\s+\(\s*[^\n()]*(?:\([^\n()]*\)[^\n()]*)*\))?"
)

# 带「…标识为…」标签的标识字段。典型来源是 ContextService 的
# 「平台标识为“xxx”，用户标识为“yyy”」——这类裸字段不满足 UMO 或
# 会话日志字符串的形态，必须单独覆盖，否则邮箱/手机号型的 user_id 会直接外泄。
# 标签本身保留（便于定位日志来源），值整段遮蔽。
_IDENTIFIER_LABEL_RE = re.compile(
    r"(?P<label>(?:用户|发送者|接收者|目标|群组|会话)?标识为)\s*"
    r"(?:“[^”\n]*”"
    r"|「[^」\n]*」"
    r'|"[^"\n]*"'
    r"|'[^'\n]*')"
)

# 插件与 site-packages 前缀：只吃行内连续非空白前缀，
# 避免对齐堆栈用的 .* 规则把长行中更早的内容一并吞掉。
_PLUGIN_PATH_RE = re.compile(r"[^\s\"']*astrbot_plugin_proactive_chat[/\\]")
_SITE_PACKAGES_PATH_RE = re.compile(r"[^\s\"']*site-packages[/\\]")


def _mask_quoted_value(match: re.Match) -> str:
    """键值对脱敏替换：保留键名与分隔符，值统一替换为 ***。"""
    return (
        f"{match.group('pre')}{match.group('name')}{match.group('post')}"
        f"{match.group('sep')}{match.group('q')}***{match.group('q')}"
    )


def _mask_bare_value(match: re.Match) -> str:
    """裸键值对脱敏替换：保留键名与分隔符，值统一替换为 ***。"""
    return f"{match.group('name')}{match.group('sep')}***"


def _mask_query_value(match: re.Match) -> str:
    """URL query 脱敏：白名单参数原样保留，其余参数值替换为 ***。"""
    sep, key, value = match.group(1), match.group(2), match.group(3)
    if key.lower() in _SAFE_QUERY_KEYS:
        return f"{sep}{key}={value}"
    return f"{sep}{key}=***"


def _mask_identifier_label(match: re.Match) -> str:
    """「…标识为 “xxx”」脱敏：保留标签，值统一替换为 ***。"""
    return f"{match.group('label')}“***”"


def sanitize_url_credentials(text: str) -> str:
    """脱敏 URL 中的凭据：userinfo（scheme://user:pass@host）与 query 参数值。"""
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    return _URL_QUERY_RE.sub(_mask_query_value, text)


def sanitize_credential_assignments(text: str) -> str:
    """脱敏键值对形式的凭据字段值。

    覆盖：Bearer/Basic 等鉴权令牌（保留方案词）、JSON 双引号、单引号、
    未加引号的 key: value（可读日志格式，含全角冒号）与裸 key=value。
    键名支持前缀式与 camelCase 写法（openai_api_key / botToken / secret_key）。
    """
    text = _BEARER_TOKEN_RE.sub(r"\1 ***", text)
    text = _CRED_QUOTED_VALUE_RE.sub(_mask_quoted_value, text)
    text = _CRED_COLON_VALUE_RE.sub(_mask_bare_value, text)
    return _CRED_BARE_VALUE_RE.sub(_mask_bare_value, text)


def sanitize_session_identifiers(text: str) -> str:
    """脱敏会话标识与配置的会话备注名。

    覆盖三种形态：UMO（platform:MessageType:target）、
    get_session_log_str 的「私聊/群聊 ID (备注名)」、以及「…标识为 “xxx”」标签字段。
    避免异常消息中内嵌的群号、会话 ID、会话备注名与平台用户标识随日志外泄。
    """
    text = _UMO_SESSION_RE.sub("<SESSION>", text)
    text = _SESSION_LOG_STR_RE.sub("<SESSION>", text)
    return _IDENTIFIER_LABEL_RE.sub(_mask_identifier_label, text)


def sanitize_home_paths(text: str) -> str:
    """脱敏家目录绝对路径，隐藏宿主机用户名。"""
    text = _HOME_PATH_RE.sub("<USER_HOME>/", text)
    text = _ROOT_PATH_RE.sub("<USER_HOME>/", text)
    text = _WIN_HOME_PATH_RE.sub(r"<USER_HOME>\\", text)
    return text


def sanitize_plugin_paths(text: str) -> str:
    """缩短插件与 site-packages 绝对路径前缀，压缩体积。"""
    text = _PLUGIN_PATH_RE.sub("<PLUGIN>/", text)
    text = _SITE_PACKAGES_PATH_RE.sub("<SITE_PACKAGES>/", text)
    return text


def sanitize_log_text(text: str) -> str:
    """组合脱敏入口：URL 凭据 → 键值对凭据 → 会话标识 → 家目录路径 → 插件路径。"""
    text = sanitize_url_credentials(text)
    text = sanitize_credential_assignments(text)
    text = sanitize_session_identifiers(text)
    text = sanitize_home_paths(text)
    text = sanitize_plugin_paths(text)
    return text


__all__ = [
    "sanitize_credential_assignments",
    "sanitize_home_paths",
    "sanitize_log_text",
    "sanitize_plugin_paths",
    "sanitize_session_identifiers",
    "sanitize_url_credentials",
]
