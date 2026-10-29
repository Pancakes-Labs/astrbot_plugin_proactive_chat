"""共享日志脱敏工具。

供「运行日志上传」链路复用，规则对齐 TelemetryManager 中既有的消息/堆栈脱敏口径
（URL 凭据、家目录路径、插件与 site-packages 路径），并额外覆盖键值对形式的凭据字段，
避免真实的 Key / Token 随日志外泄。

脱敏策略宁过度不遗漏：凭据键名命中即整体替换为占位符，
极少数同名非敏感字段会被误伤，可接受。
"""

from __future__ import annotations

import re

# 凭据类字段键名模式（允许 -/_ 分隔符，大小写不敏感），
# 覆盖 snake_case / camelCase / kebab-case 变体（如 refreshToken、apiKey）。
_CREDENTIAL_FIELD_PATTERN = (
    r"(?:token|key|secret|password|pwd|cookie|authorization|"
    r"api[-_]?key|refresh[-_]?token|access[-_]?token|app[-_]?key|"
    r"app[-_]?secret|client[-_]?secret|cookie[-_]?str)"
)

# URL 中凭据：
# 1. userinfo 形态 scheme://user:pass@host 或 scheme://token@host → 整段替换为 ***@；
#    userinfo 段限制在 authority 内：不跨越 / ? # 与空白，并以贪婪方式匹配到该段
#    最后一个 @（未编码密码可能含 @），避免误伤 ?email=a@b.com 这类普通 URL。
# 2. query 参数形态 ?token=xxx / &api-key=yyy → 值替换为 ***。
_URL_USERINFO_RE = re.compile(r"\b([\w+-]+://)[^\s/?#]+@")
_URL_CREDENTIAL_RE = re.compile(
    rf"(?i)([?&](?:{_CREDENTIAL_FIELD_PATTERN})=)[^&\s\"']+"
)

# Bearer/Basic 等鉴权方案后的令牌：Bearer <JWT>、Basic <base64> → 保留方案词，令牌 ***。
# 阈值 6 个字符，避免把普通英文词当令牌；JWT（含 . 分隔的 base64url 段）可完整覆盖。
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(bearer|basic|digest|token)\s+([A-Za-z0-9._~+/=-]{6,})"
)

# 带引号值的键值对（JSON/字典形态）：覆盖 "key": "value"、'key': 'value'、
# key: "value" 等写法，仅遮蔽值部分，保留键名便于阅读定位。
_CRED_QUOTED_VALUE_RE = re.compile(
    rf"""(?i)(?P<pre>["']?)(?P<name>{_CREDENTIAL_FIELD_PATTERN})(?P<post>["']?)"""
    rf"""(?P<sep>\s*[:=]\s*)(?P<q>["'])(?P<val>[^"'\n]*)(?P=q)"""
)

# 未加引号的冒号形态：api_key: xxx（可读日志常见写法，含全角冒号）。
# 值若以鉴权方案词开头（Bearer *** 已由 _BEARER_TOKEN_RE 处理）则跳过，保留方案词。
_CRED_COLON_VALUE_RE = re.compile(
    rf"(?i)\b(?P<name>{_CREDENTIAL_FIELD_PATTERN})(?P<sep>\s*[:：]\s*)"
    rf"(?P<val>(?!(?:bearer|basic|digest|token)\b)[^\s\"'&,，。；：）)】\]]+)"
)

# 裸键值对形态：key=value（值取到空白/引号/分隔符为止）。
_CRED_BARE_VALUE_RE = re.compile(
    rf"(?i)\b(?P<name>{_CREDENTIAL_FIELD_PATTERN})(?P<sep>\s*=\s*)"
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

# 会话日志字符串（get_session_log_str 的输出格式）：私聊/群聊/未知类型 ID (备注名)，
# 备注名可缺省。整段替换，避免群号、会话 ID 与会话备注名随日志外泄。
_SESSION_LOG_STR_RE = re.compile(
    r"(?:私聊|群聊|未知类型)\s*[A-Za-z0-9_@.\-]+\s*(?:\([^)]*\))?"
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


def sanitize_url_credentials(text: str) -> str:
    """脱敏 URL 中的凭据：userinfo（scheme://user:pass@host）与 query 参数值。"""
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    return _URL_CREDENTIAL_RE.sub(r"\1***", text)


def sanitize_credential_assignments(text: str) -> str:
    """脱敏键值对形式的凭据字段值。

    覆盖：Bearer/Basic 等鉴权令牌（保留方案词）、JSON 双引号、单引号、
    未加引号的 key: value（可读日志格式，含全角冒号）与裸 key=value。
    """
    text = _BEARER_TOKEN_RE.sub(r"\1 ***", text)
    text = _CRED_QUOTED_VALUE_RE.sub(_mask_quoted_value, text)
    text = _CRED_COLON_VALUE_RE.sub(_mask_bare_value, text)
    return _CRED_BARE_VALUE_RE.sub(_mask_bare_value, text)


def sanitize_session_identifiers(text: str) -> str:
    """脱敏会话标识与配置的会话备注名（UMO 与 私聊/群聊 ID (名称) 两种形态）。

    避免发送失败等异常消息中内嵌的群号、会话 ID 与会话备注名随日志外泄。
    """
    text = _UMO_SESSION_RE.sub("<SESSION>", text)
    text = _SESSION_LOG_STR_RE.sub("<SESSION>", text)
    return text


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
