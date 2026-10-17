/**
 * 文件职责：操作反馈 Hook，统一管理“按会话/条目记录反馈 + 定时清理”。
 */

(function () {
    function useFeedbackMap(ttlMs) {
        const lifetime = Number(ttlMs) > 0 ? Number(ttlMs) : 3000;
        const [feedbackMap, setFeedbackMap] = React.useState({});

        const setFeedback = React.useCallback((key, value) => {
            setFeedbackMap((prev) => {
                const current = prev[key];
                // 相同反馈不触发新对象，避免无意义的重渲染。
                if (current && current.status === value.status && current.text === value.text) {
                    return prev;
                }
                return Object.assign({}, prev, { [key]: value });
            });
        }, []);

        const clearFeedback = React.useCallback((key) => {
            setFeedbackMap((prev) => {
                if (!Object.prototype.hasOwnProperty.call(prev, key)) return prev;
                const next = Object.assign({}, prev);
                delete next[key];
                return next;
            });
        }, []);

        React.useEffect(() => {
            const successKeys = Object.keys(feedbackMap)
                .filter((key) => feedbackMap[key] && feedbackMap[key].status === 'success');
            if (successKeys.length === 0) return undefined;

            const timers = successKeys.map((key) => setTimeout(() => clearFeedback(key), lifetime));
            return () => {
                timers.forEach((timer) => clearTimeout(timer));
            };
        }, [feedbackMap, clearFeedback, lifetime]);

        return {
            feedbackMap: feedbackMap,
            setFeedback: setFeedback,
            clearFeedback: clearFeedback,
        };
    }

    window.useFeedbackMap = useFeedbackMap;
})();
