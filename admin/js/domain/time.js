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

        const parsed = window.parseDateish ? window.parseDateish(raw) : new Date(raw);
        return parsed ? parsed.getTime() : null;
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

    // 计时器对象统一读取剩余秒数：优先使用后端字段，缺失时按目标时间推导。
    function resolveTimerRemainingSeconds(timer, nowMs) {
        const numeric = Number(timer && timer.remaining_seconds);
        if (Number.isFinite(numeric)) {
            return Math.max(0, numeric);
        }
        const targetMs = toEpochMs(timer && timer.target_time);
        if (targetMs === null) return 0;
        return Math.max(0, Math.ceil((targetMs - Number(nowMs || Date.now())) / 1000));
    }

    window.TimeDomain = Object.freeze({
        toEpochMs: toEpochMs,
        toDate: toDate,
        remainingSecondsUntil: remainingSecondsUntil,
        resolveTimerRemainingSeconds: resolveTimerRemainingSeconds,
    });
})();
