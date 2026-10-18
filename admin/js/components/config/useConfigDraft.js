/**
 * 文件职责：配置草稿 Hook，负责草稿缓存、展开状态记忆与滚动位置恢复。
 */

(function () {
    const { useEffect } = React;

    function useConfigDraft(options) {
        const mode = options.mode;
        const selectedSession = options.selectedSession;
        const config = options.config;
        const schema = options.schema;
        const loading = options.loading;
        const expandedKeys = options.expandedKeys;
        const isDirtyRef = options.isDirtyRef;
        const scrollContainerRef = options.scrollContainerRef;

        // 动态缓存 key：不同模式 / 不同会话使用不同 key，避免草稿串线。
        const getDraftKey = React.useCallback((currentMode, currentSession) => {
            const m = currentMode || mode;
            const s = currentSession || selectedSession;
            if (m === 'session' && s) {
                return `astrbot_plugin_proactive_draft_config_session_${s}`;
            }
            return 'astrbot_plugin_proactive_draft_config_global';
        }, [mode, selectedSession]);

        const getExpandedKey = React.useCallback((currentMode, currentSession) => {
            const m = currentMode || mode;
            const s = currentSession || selectedSession;
            if (m === 'session' && s) {
                return `astrbot_plugin_proactive_expanded_keys_session_${s}`;
            }
            return 'astrbot_plugin_proactive_expanded_keys_global';
        }, [mode, selectedSession]);

        // 记录上一次的上下文标识，切换会话当帧禁止写草稿，避免旧值落到新 key。
        const contextKey = options.contextKey;
        const lastContextKeyRef = React.useRef(contextKey);

        const scrollKey = window.ScrollDomain.buildScrollKey(
            mode === 'session' && selectedSession
                ? `config_session_${selectedSession}`
                : 'config_global'
        );

        // 自动保存草稿，但只有发生本地修改时才写入，避免把服务端新值反向覆盖成本地草稿。
        useEffect(() => {
            if (lastContextKeyRef.current !== contextKey) {
                // 上下文刚切换，此时 config 仍是上一个会话的快照，跳过本次写入。
                lastContextKeyRef.current = contextKey;
                return;
            }
            if (config && isDirtyRef.current) {
                localStorage.setItem(getDraftKey(), JSON.stringify(config));
            }
        }, [config, getDraftKey, contextKey]);

        // 记忆折叠 / 展开状态，让用户在大型 Schema 中保持稳定的浏览结构。
        useEffect(() => {
            if (schema) {
                localStorage.setItem(getExpandedKey(), JSON.stringify(expandedKeys || []));
            }
        }, [expandedKeys, schema, getExpandedKey]);

        const restoreExpandedKeys = React.useCallback((currentMode, currentSession) => {
            const cached = localStorage.getItem(getExpandedKey(currentMode, currentSession));
            if (!cached) return window.ConfigShared.getAllExpandablePaths(schema || {});
            try {
                const parsed = JSON.parse(cached);
                // 缓存可能是旧版本写入的非数组结构，直接使用会让 includes 调用崩溃。
                if (!Array.isArray(parsed)) {
                    return window.ConfigShared.getAllExpandablePaths(schema || {});
                }
                return parsed.filter((item) => typeof item === 'string');
            } catch (e) {
                console.error('解析展开状态失败', e);
                return [];
            }
        }, [getExpandedKey, schema]);

        // 滚动恢复与记录复用统一 Hook，与主内容区规则保持一致。
        window.useScrollRestore(scrollContainerRef, scrollKey, { enabled: !loading });

        return {
            getDraftKey: getDraftKey,
            getExpandedKey: getExpandedKey,
            scrollKey: scrollKey,
            restoreExpandedKeys: restoreExpandedKeys,
        };
    }

    window.useConfigDraft = useConfigDraft;
})();
