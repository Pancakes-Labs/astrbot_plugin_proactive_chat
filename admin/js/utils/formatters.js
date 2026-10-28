/**
 * 文件职责：格式化工具模块，负责日期时间解析、显示格式化与时长文本处理。
 */

function parseDateish(value) {
    // 秒/毫秒阈值与字符串解析统一由 TimeDomain 提供。
    if (window.TimeDomain && typeof window.TimeDomain.toDate === 'function') {
        return window.TimeDomain.toDate(value);
    }

    // TimeDomain 尚未加载时的最小兜底，仅覆盖 Date 与空值。
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) {
        return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
    }
    const fallback = new Date(value);
    return Number.isNaN(fallback.getTime()) ? null : fallback;
}

function resolveTimeZone(timeZone = 'Asia/Shanghai') {
    const fallback = 'Asia/Shanghai';
    const tz = String(timeZone || fallback).trim();

    if (!tz) return { mode: 'iana', value: fallback };

    // 兼容 UTC+8 / UTC-5.5 这类偏移写法，便于不支持 IANA 时区名时仍可工作。
    if (/^UTC[+-]\d+(?:\.\d+)?$/i.test(tz)) {
        return { mode: 'offset', value: Number(tz.slice(3)) || 0 };
    }

    // 默认按 IANA 时区名处理，如 Asia/Shanghai、UTC、America/New_York。
    return { mode: 'iana', value: tz };
}

function getZonedDateParts(date, timeZone = 'Asia/Shanghai', options = {}) {
    const parsed = parseDateish(date);
    if (!parsed) return null;

    const tz = resolveTimeZone(timeZone);
    const includeYear = options.includeYear !== false;
    const includeSeconds = Boolean(options.includeSeconds);

    if (tz.mode === 'offset') {
        // 对纯 UTC 偏移模式，直接手动换算到目标时区，避免依赖 Intl 的 IANA 支持。
        const shifted = new Date(parsed.getTime() + tz.value * 3600000);
        return {
            year: String(shifted.getUTCFullYear()),
            month: String(shifted.getUTCMonth() + 1).padStart(2, '0'),
            day: String(shifted.getUTCDate()).padStart(2, '0'),
            hour: String(shifted.getUTCHours()).padStart(2, '0'),
            minute: String(shifted.getUTCMinutes()).padStart(2, '0'),
            second: includeSeconds ? String(shifted.getUTCSeconds()).padStart(2, '0') : '00',
            includeYear,
            includeSeconds,
        };
    }

    // IANA 模式交给 Intl 处理，可正确覆盖夏令时等复杂时区规则。
    const formatter = new Intl.DateTimeFormat('zh-CN', {
        year: includeYear ? 'numeric' : undefined,
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: includeSeconds ? '2-digit' : undefined,
        hour12: false,
        timeZone: tz.value,
    });

    const parts = formatter.formatToParts(parsed);
    const mapped = {};
    parts.forEach(({ type, value }) => {
        mapped[type] = value;
    });

    return {
        year: mapped.year || '',
        month: mapped.month || '00',
        day: mapped.day || '00',
        hour: mapped.hour || '00',
        minute: mapped.minute || '00',
        second: includeSeconds ? (mapped.second || '00') : '00',
        includeYear,
        includeSeconds,
    };
}

function formatDateTime(value, timeZone = 'Asia/Shanghai', options = {}) {
    const parts = getZonedDateParts(value, timeZone, options);
    if (!parts) return '--';

    const datePart = parts.includeYear
        ? `${parts.year}-${parts.month}-${parts.day}`
        : `${parts.month}-${parts.day}`;
    const timePart = parts.includeSeconds
        ? `${parts.hour}:${parts.minute}:${parts.second}`
        : `${parts.hour}:${parts.minute}`;

    // 统一输出为易读的“日期 + 时间”文本，供状态页、任务页、头部时钟复用。
    // 日期与时间之间保留两个空格，增强卡片中时间文本的视觉层次。
    return `${datePart}  ${timePart}`;
}

function formatFriendlyTime(value, timeZone = 'Asia/Shanghai') {
    const date = parseDateish(value);
    if (!date) return '--';

    // 以当前时间为基准生成“几秒前 / 几分钟后”这类相对时间提示。
    const diffMs = date.getTime() - Date.now();
    const absMs = Math.abs(diffMs);
    const absSeconds = Math.floor(absMs / 1000);
    const absMinutes = Math.floor(absMs / 60000);
    const absHours = Math.floor(absMs / 3600000);

    if (diffMs < 0) {
        if (absSeconds < 15) return '刚刚';
        if (absSeconds < 60) return `${absSeconds} 秒前`;
        if (absMinutes < 60) return `${absMinutes} 分钟前`;
        if (absHours < 24) return `${absHours} 小时前`;
        // 超过一天时回退到绝对时间，避免相对时间过于模糊。
        return formatDateTime(date, timeZone, { includeYear: true, includeSeconds: true });
    }

    if (absSeconds < 15) return '即将到来';
    if (absSeconds < 60) return `${absSeconds} 秒后`;
    if (absMinutes < 60) return `${absMinutes} 分钟后`;
    if (absHours < 24) return `${absHours} 小时后`;
    return formatDateTime(date, timeZone, { includeYear: true, includeSeconds: true });
}

function formatDuration(totalSeconds, options = {}) {
    const value = Number(totalSeconds);
    if (!Number.isFinite(value) || value <= 0) {
        return options.compact ? '0秒' : '0 秒';
    }

    const seconds = Math.max(0, Math.floor(value));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    const compact = Boolean(options.compact);
    const padHours = Boolean(options.padHours);
    const maxUnits = Number(options.maxUnits) || 4;

    if (compact) {
        // 紧凑模式更适合卡片内展示，可在有限宽度中保留更多信息。
        const tokens = [];
        if (days > 0) tokens.push(`${days}天`);
        if (hours > 0 || (padHours && tokens.length)) tokens.push(`${days > 0 || padHours ? String(hours).padStart(2, '0') : hours}小时`);
        if (minutes > 0 || tokens.length) tokens.push(`${tokens.length ? String(minutes).padStart(2, '0') : minutes}分`);
        tokens.push(`${tokens.length ? String(secs).padStart(2, '0') : secs}秒`);
        return tokens.slice(0, maxUnits).join('');
    }

    // 常规模式更适合阅读型文本，如“1 天 2 小时 3 分钟”。
    const segments = [];
    if (days > 0) segments.push(`${days} 天`);
    if (hours > 0) segments.push(`${hours} 小时`);
    if (minutes > 0) segments.push(`${minutes} 分钟`);
    if (secs > 0 || !segments.length) segments.push(`${secs} 秒`);
    return segments.slice(0, maxUnits).join(' ');
}


function resolveSourceModeLabel(sourceMode) {
    const normalized = String(sourceMode || '').trim().toLowerCase();
    switch (normalized) {
        case 'platform_message_history':
            return '平台完整聊天流水';
        case 'hybrid':
            return '混合模式';
        case 'conversation_history':
        default:
            return '当前 AstrBot LLM 对话历史';
    }
}

function formatUnansweredLabel(currentCount, maxCount) {
    const current = Math.max(0, Number(currentCount) || 0);
    const max = Math.max(0, Number(maxCount) || 0);
    return max > 0 ? `未回复次数: ${current}/${max}` : `未回复: ${current}`;
}

// 统一挂到 window，供各页面和组件直接调用，无需额外模块系统。
window.parseDateish = parseDateish;
window.formatDateTime = formatDateTime;
window.formatFriendlyTime = formatFriendlyTime;
window.formatDuration = formatDuration;
window.resolveSourceModeLabel = resolveSourceModeLabel;
window.formatUnansweredLabel = formatUnansweredLabel;

