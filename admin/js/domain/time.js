/**
 * 文件职责：时间领域纯函数，统一秒级/毫秒级时间戳归一与剩余时间计算。
 */

(function () {
    // 小于该阈值的数字视为秒级时间戳，大于视为毫秒级。
    const SECOND_TIMESTAMP_THRESHOLD = 1000000000000;

    function normalizeNumberTimestamp(value) {
        return value < SECOND_TIMESTAMP_THRESHOLD ? value * 1000 : value;
    }

    // 将任意时间输入归一为毫秒时间戳；无法解析时返回 null。
    function toEpochMs(value) {
        if (value === null || value === undefined || value === '') return null;

        if (value instanceof Date) {
            const time = value.getTime();
            return Number.isNaN(time) ? null : time;
        }

        if (typeof value === 'number') {
            const ms = normalizeNumberTimestamp(value);
            return Number.isFinite(ms) ? ms : null;
        }

        const raw = String(value).trim();
        if (!raw) return null;

        // 纯数字字符串同样按秒/毫秒阈值处理，避免被 Date 当作年份解析。
        if (/^-?\d+(?:\.\d+)?$/.test(raw)) {
            const ms = normalizeNumberTimestamp(Number(raw));
            return Number.isFinite(ms) ? ms : null;
        }

        // 兼容 "YYYY-MM-DD HH:mm:ss" 这类非标准 ISO 文本，统一替换为 T 分隔。
        const normalized = raw.includes('T') ? raw : raw.replace(' ', 'T');
        const parsed = new Date(normalized);
        return Number.isNaN(parsed.getTime()) ? null : parsed.getTime();
    }

    function toDate(value) {
        const ms = toEpochMs(value);
        return ms === null ? null : new Date(ms);
    }

    function remainingSecondsUntil(value, nowMs) {
        const targetMs = toEpochMs(value);
        if (targetMs === null) return null;
        return Math.max(0, Math.ceil((targetMs - Number(nowMs || Date.now())) / 1000));
    }

    // 计时器剩余秒数的统一来源，按“后端快照 + 本地流逝”递增，避免整秒值停滞。
    // 优先使用快照秒数：它与服务器时钟同源，可规避浏览器与服务器时钟偏差；
    // 快照缺失时才退回目标时间推导，两者都没有时返回 null 交由调用方显示“待确认”。
    function resolveTimerRemainingSeconds(timer, nowMs, snapshotAtMs) {
        if (!timer) return null;
        const now = Number(nowMs || Date.now());

        // 显式判空，避免 null 被 Number() 变成 0 而误判为“已归零”。
        const rawRemaining = timer.remaining_seconds;
        if (rawRemaining !== null && rawRemaining !== undefined && rawRemaining !== '') {
            const numeric = Number(rawRemaining);
            if (Number.isFinite(numeric)) {
                const snapshot = Number(snapshotAtMs);
                if (!Number.isFinite(snapshot) || snapshot <= 0) {
                    return Math.max(0, numeric);
                }
                const elapsedSeconds = Math.max(0, Math.floor((now - snapshot) / 1000));
                return Math.max(0, numeric - elapsedSeconds);
            }
        }

        const targetMs = toEpochMs(timer.target_time);
        if (targetMs !== null) {
            return Math.max(0, Math.ceil((targetMs - now) / 1000));
        }

        return null;
    }

    window.TimeDomain = Object.freeze({
        toEpochMs: toEpochMs,
        toDate: toDate,
        remainingSecondsUntil: remainingSecondsUntil,
        resolveTimerRemainingSeconds: resolveTimerRemainingSeconds,
    });
})();
