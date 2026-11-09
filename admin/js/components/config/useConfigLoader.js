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
        // 会话模式下展示的“最终生效人格”预览，避免用户只能在日志里确认结果。
        const [sessionPersona, setSessionPersona] = useState(null);
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
                // 会话列表是下拉框数据源，失败需要明确提示，否则用户只会看到空列表。
                if (window.Toast) {
                    window.Toast.warning((e && e.message) || '会话列表加载失败', {
                        title: '会话列表',
                        key: 'config-sessions-load',
                    });
                }
                return [];
            }
        }, [api]);

        // 返回 true 表示本次响应被采纳；返回 false 表示请求已过期或失败。
        // 调用方据此决定是否同步派生状态（展开态、脏标记），避免旧响应覆盖新上下文。
        const loadConfig = React.useCallback(async (currentMode, currentSession) => {
            // 所有配置加载都以当前 Schema 为前提，避免出现“无结构定义的盲编辑”。
            if (!schema) return false;

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
                        return false;
                    }
                    setSessionLoading(true);
                    // 切换会话后必须先清掉上一轮的人格，否则加载期间标签会显示
                    // 上一个会话的人格；新请求失败时也会残留旧值。
                    if (requestSeq === loadConfigSeqRef.current) {
                        setSessionPersona(null);
                    }
                    // 人格预览与配置读取互不阻塞：预览失败不应影响配置页可用性。
                    const personaPromise = api.getSessionPersona(currentSession)
                        .catch(() => null);
                    const sessionData = await api.getSessionConfig(currentSession);
                    const personaData = await personaPromise;
                    if (requestSeq === loadConfigSeqRef.current) {
                        setSessionPersona(personaData);
                    }
                    const hasBaseConfig = Boolean(sessionData && sessionData.base);
                    configData = (sessionData && (sessionData.effective || sessionData.override)) || {};
                    // 会话状态必须在序号检查之后再写：否则迟到的旧会话响应会覆盖新会话的
                    // baseAvailable，把新会话的保存按钮错误地置为不可用。
                    if (requestSeq !== loadConfigSeqRef.current) return false;
                    setSessionConfigState(
                        hasBaseConfig
                            ? { baseAvailable: true, message: '' }
                            : {
                                baseAvailable: false,
                                message: '该会话尚未命中对应类型的全局 session_list，因此暂时无法保存会话差异配置。请先在对应的全局配置中加入该会话。',
                            }
                    );
                } else {
                    setSessionConfigState({ baseAvailable: true, message: '' });
                    setSessionPersona(null);
                    configData = await api.getConfig();
                }

                // 如果不是最新请求，丢弃过期响应，避免会话串改。
                if (requestSeq !== loadConfigSeqRef.current) return false;

                // 配置加载始终以服务端返回值为准，草稿不参与初始化覆盖。
                setConfig(configData);
                return true;
            } catch (e) {
                if (requestSeq === loadConfigSeqRef.current) {
                    console.error('加载配置失败', e);
                    setConfig(null);
                    // 请求失败时人格预览同样不可信，清掉避免展示过期信息。
                    if (currentMode === 'session') {
                        setSessionPersona(null);
                    }
                    // 页面此时只剩“无法加载配置”的静态空态，用 toast 说明具体原因。
                    if (window.Toast) {
                        window.Toast.error((e && e.message) || '配置加载失败，请稍后重试', {
                            title: '配置加载失败',
                            key: 'config-load-error',
                        });
                    }
                }
                return false;
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
                    if (!disposed && window.Toast) {
                        window.Toast.error((e && e.message) || '配置结构加载失败', {
                            title: '配置页初始化失败',
                            key: 'config-init-error',
                        });
                    }
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
            sessionPersona: sessionPersona,
            loadConfig: loadConfig,
            loadSessions: loadSessions,
        };
    }

    window.useConfigLoader = useConfigLoader;
})();
