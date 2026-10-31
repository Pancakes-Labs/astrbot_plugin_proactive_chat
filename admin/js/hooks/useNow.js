/**
 * 文件职责：全局时钟 Hook，所有需要每秒刷新的组件共用同一个定时器。
 */

(function () {
    // 订阅集合与唯一 interval，避免每个视图各起一个每秒定时器。
    const listeners = new Set();
    let timer = null;
    let lastValue = Date.now();

    function notify() {
        lastValue = Date.now();
        listeners.forEach((listener) => {
            try {
                listener(lastValue);
            } catch (e) {
                // 单个订阅者异常不应影响其余组件刷新。
            }
        });
    }

    function startTimer() {
        if (timer || listeners.size === 0) return;
        // 递归 setTimeout 并对其到下一个整秒，避免 setInterval 累积漂移
        // 导致秒数偶发跳两秒或卡住一拍。
        const delay = 1000 - (Date.now() % 1000);
        timer = setTimeout(() => {
            timer = null;
            // 页面隐藏时暂停刷新，回到前台后由 visibilitychange 立即补一帧。
            if (document.visibilityState !== 'hidden') notify();
            startTimer();
        }, delay);
    }

    function stopTimerIfIdle() {
        if (listeners.size > 0 || !timer) return;
        clearTimeout(timer);
        timer = null;
    }

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && listeners.size > 0) {
            notify();
            startTimer();
        }
    });

    function useNow(enabled) {
        const active = enabled !== false;
        const [nowMs, setNowMs] = React.useState(() => Date.now());

        React.useEffect(() => {
            if (!active) return undefined;

            // 订阅集合里必须放真正的 state setter，否则 notify 时会整体报错。
            listeners.add(setNowMs);
            setNowMs(Date.now());
            startTimer();

            return () => {
                listeners.delete(setNowMs);
                stopTimerIfIdle();
            };
        }, [active]);

        return nowMs;
    }

    window.useNow = useNow;
})();
