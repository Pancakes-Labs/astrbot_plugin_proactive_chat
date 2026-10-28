/**
 * 文件职责：滚动位置领域工具，统一滚动位置存储键与读写行为。
 */

(function () {
    // 统一版本前缀，便于后续结构升级时整体迁移旧键。
    const SCROLL_KEY_PREFIX = 'astrbot_scroll_v2_';

    function buildScrollKey(scope) {
        return `${SCROLL_KEY_PREFIX}${scope || 'default'}`;
    }

    function readScrollTop(key) {
        try {
            const value = parseInt(localStorage.getItem(key) || '0', 10);
            return Number.isFinite(value) && value > 0 ? value : 0;
        } catch (e) {
            return 0;
        }
    }

    function writeScrollTop(key, value) {
        try {
            localStorage.setItem(key, String(Math.max(0, Math.round(Number(value) || 0))));
        } catch (e) {
            // 存储受限时静默降级，滚动位置属于非关键体验。
        }
    }

    function clearScrollTop(key) {
        try {
            localStorage.removeItem(key);
        } catch (e) {
            // 同上，失败无需中断业务流程。
        }
    }

    window.ScrollDomain = Object.freeze({
        buildScrollKey: buildScrollKey,
        readScrollTop: readScrollTop,
        writeScrollTop: writeScrollTop,
        clearScrollTop: clearScrollTop,
    });
})();
