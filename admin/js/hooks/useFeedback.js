/**
 * 文件职责：操作反馈 Hook，统一管理“按会话/条目记录反馈 + 定时清理”。
 */

(function () {
    function useFeedbackMap(ttlMs) {
        const lifetime = Number(ttlMs) > 0 ? Number(ttlMs) : 3000;
        const [feedbackMap, setFeedbackMap] = React.useState({});
        // 每个 key 独立持有清理定时器，新增反馈不会重置其它 key 的倒计时。
        const timersRef = React.useRef({});

        const clearTimer = React.useCallback((key) => {
            const timerId = timersRef.current[key];
            if (!timerId) return;
            clearTimeout(timerId);
            delete timersRef.current[key];
        }, []);

        const removeKey = React.useCallback((key) => {
            setFeedbackMap((prev) => {
                if (!Object.prototype.hasOwnProperty.call(prev, key)) return prev;
                const next = Object.assign({}, prev);
                delete next[key];
                return next;
            });
        }, []);

        const clearFeedback = React.useCallback((key) => {
            clearTimer(key);
            removeKey(key);
        }, [clearTimer, removeKey]);

        const setFeedback = React.useCallback((key, value) => {
            setFeedbackMap((prev) => {
                const current = prev[key];
                // 相同反馈不触发新对象，避免无意义的重渲染。
                if (current && current.status === value.status && current.text === value.text) {
                    return prev;
                }
                return Object.assign({}, prev, { [key]: value });
            });

            clearTimer(key);
            if (value && value.status === 'success') {
                timersRef.current[key] = setTimeout(() => {
                    delete timersRef.current[key];
                    removeKey(key);
                }, lifetime);
            }
        }, [clearTimer, removeKey, lifetime]);

        // 卸载时统一回收，避免定时器在组件销毁后回写状态。
        React.useEffect(() => {
            return () => {
                Object.keys(timersRef.current).forEach((key) => {
                    clearTimeout(timersRef.current[key]);
                });
                timersRef.current = {};
            };
        }, []);

        return {
            feedbackMap: feedbackMap,
            setFeedback: setFeedback,
            clearFeedback: clearFeedback,
        };
    }

    window.useFeedbackMap = useFeedbackMap;
})();
