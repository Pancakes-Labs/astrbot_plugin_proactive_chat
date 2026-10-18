/**
 * 文件职责：配置加载 Hook，负责 Schema、会话列表与当前配置快照的拉取与竞态控制。
 */

(function () {
    const { useState, useRef, useEffect } = React;

    function useConfigLoader(api) {
        const [schema, setSchema] = useState(null);
        const [loading, setLoading] = useState(true);
        const [sessions, setSessions] = useState([]);
        const [sessionLoading, setSessionLoading] = useState(false);
        const [sessionConfigState, setSessionConfigState] = useState({ baseAvailable: true, message: '' });
        const [config, setConfig] = useState(null);
        const loadConfigSeqRef = useRef(0);

        const loadSessions = React.useCallback(async () => {
            try {
                // 会话列表用于会话差异配置模式下的下拉框、状态提示与覆写标记展示。
                const result = await api.listSessions();
                const normalized = window.SessionDomain.normalizeSessionList(
                    (result && result.sessions) || []
                );
                setSessions(normalized);
                return normalized;
            } catch (e) {
                console.error('加载会话列表失败', e);
                return [];
            }
        }, [api]);

        const loadConfig = React.useCallback(async (currentMode, currentSession) => {
            // 所有配置加载都以当前 Schema 为前提，避免出现“无结构定义的盲编辑”。
            if (!schema) return;

            // 递增请求序号，防止快速切换会话时后返回的旧请求覆盖新状态。
            const requestSeq = ++loadConfigSeqRef.current;
            setLoading(true);
            try {
                let configData = null;

                if (currentMode === 'session') {
                    if (!currentSession) {
                        if (requestSeq === loadConfigSeqRef.current) {
                            setConfig(null);
                            setSessionConfigState({ baseAvailable: false, message: '请先选择一个会话' });
                        }
                        return;
                    }
                    setSessionLoading(true);
                    const sessionData = await api.getSessionConfig(currentSession);
                    const hasBaseConfig = Boolean(sessionData && sessionData.base);
                    setSessionConfigState(
                        hasBaseConfig
                            ? { baseAvailable: true, message: '' }
                            : {
                                baseAvailable: false,
                                message: '该会话尚未命中对应类型的全局 session_list，因此暂时无法保存会话差异配置。请先在对应的全局配置中加入该会话。',
                            }
                    );
                    configData = (sessionData && (sessionData.effective || sessionData.override)) || {};
                } else {
                    setSessionConfigState({ baseAvailable: true, message: '' });
                    configData = await api.getConfig();
                }

                // 如果不是最新请求，丢弃过期响应，避免会话串改。
                if (requestSeq !== loadConfigSeqRef.current) return;

                // 配置加载始终以服务端返回值为准，草稿不参与初始化覆盖。
                setConfig(configData);
            } catch (e) {
                if (requestSeq === loadConfigSeqRef.current) {
                    console.error('加载配置失败', e);
                    setConfig(null);
                }
            } finally {
                if (requestSeq === loadConfigSeqRef.current) {
                    setSessionLoading(false);
                    setLoading(false);
                }
            }
        }, [api, schema]);

        // 首次进入页面：先拿 Schema，再准备会话列表。
        useEffect(() => {
            let disposed = false;
            (async () => {
                setLoading(true);
                try {
                    const schemaData = await api.getConfigSchema();
                    if (disposed) return;
                    setSchema(schemaData);
                    await loadSessions();
                } catch (e) {
                    console.error('初始化配置页失败', e);
                } finally {
                    if (!disposed) setLoading(false);
                }
            })();
            return () => {
                disposed = true;
            };
        }, [api, loadSessions]);

        return {
            schema: schema,
            config: config,
            setConfig: setConfig,
            loading: loading,
            sessions: sessions,
            sessionLoading: sessionLoading,
            sessionConfigState: sessionConfigState,
            loadConfig: loadConfig,
            loadSessions: loadSessions,
        };
    }

    window.useConfigLoader = useConfigLoader;
})();
