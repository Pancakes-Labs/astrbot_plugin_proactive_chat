(() => {
/**
 * 文件职责：配置编辑器主组件，负责模式切换、草稿保存与底部操作栏。
 * 字段渲染、加载逻辑与草稿管理分别拆分到 ConfigField / useConfigLoader / useConfigDraft。
 */

 const { Box, TextField, Switch, Typography, Button, Paper, Chip, MenuItem, ToggleButton, ToggleButtonGroup } = MaterialUI;
 const { useState, useEffect, useRef } = React;
 const { GLOBAL_CONFIG_KEYS, getAllExpandablePaths, getSessionSchemaEntries } = window.ConfigShared;

 // 递归按 Schema 生成默认值，用于“恢复默认”操作。
 function generateDefaults(schema) {
    const result = {};
    Object.entries(schema || {}).forEach(([key, value]) => {
        if (value && value.type === 'object' && value.items) {
            result[key] = generateDefaults(value.items);
        } else {
            result[key] = value && value.default !== undefined ? value.default : null;
        }
    });
    return result;
 }

 // 保存前递归清理配置：数组中的字符串去首尾空白，并去掉空项，减小脏数据概率。
 function cleanConfig(obj) {
    if (Array.isArray(obj)) {
        return obj
            .map((item) => (typeof item === 'string' ? item.trim() : item))
            .filter((item) => item !== '');
    }
    if (obj && typeof obj === 'object') {
        const next = {};
        Object.keys(obj).forEach((key) => {
            next[key] = cleanConfig(obj[key]);
        });
        return next;
    }
    return obj;
 }

// 把人格摘要压成一行短文案：人格名（来源），缺失时给出明确占位。
function resolvePersonaLabel(persona) {
    if (!persona) return '解析中…';
    if (persona.disabled) return '已禁用（不注入人格）';
    const name = persona.persona_name || persona.persona_id;
    if (!name) return '未解析到可用人格';
    const source = persona.persona_source_label || persona.persona_source;
    return source ? `${name}（${source}）` : name;
}

// 浏览上下文记忆键：记录上次停留在“全局配置”还是某个会话的差异配置，
// 以及会话模式下具体选中的会话，保证切换视图 / 刷新后回到同一处。
const CONFIG_VIEW_CONTEXT_KEY = 'astrbot_plugin_proactive_config_view_context';

function readSavedViewContext() {
    try {
        const raw = localStorage.getItem(CONFIG_VIEW_CONTEXT_KEY);
        if (!raw) return { mode: 'global', session: '' };
        const parsed = JSON.parse(raw);
        const mode = parsed && parsed.mode === 'session' ? 'session' : 'global';
        const session = parsed && typeof parsed.session === 'string' ? parsed.session : '';
        return { mode, session };
    } catch (e) {
        // 缓存损坏时退回全局配置，不影响页面可用性。
        return { mode: 'global', session: '' };
    }
}

function writeSavedViewContext(mode, session) {
    try {
        localStorage.setItem(CONFIG_VIEW_CONTEXT_KEY, JSON.stringify({
            mode: mode === 'session' ? 'session' : 'global',
            session: session || '',
        }));
    } catch (e) {
        // 存储受限时静默降级，浏览上下文记忆属于非关键体验。
    }
}

function ConfigEditor() {
    const api = useApi();
    // 初始值来自本地缓存：首渲染即定位到上次浏览的上下文，避免先闪一下全局配置。
    const initialViewContextRef = useRef(readSavedViewContext());

    // 多会话模式相关状态：global 表示编辑全局配置，session 表示编辑单会话差异配置。
    const [mode, setMode] = useState(initialViewContextRef.current.mode);
    const [selectedSession, setSelectedSession] = useState(initialViewContextRef.current.session);
    const [expandedKeys, setExpandedKeys] = useState([]);
    const [saving, setSaving] = useState(false);
    // 已恢复的浏览上下文，避免恢复态被“默认选中第一个会话”覆盖。
    const savedSessionRef = useRef(initialViewContextRef.current.session);

    const scrollContainerRef = useRef(null);
    const isDirtyRef = useRef(false);

    const loader = useConfigLoader(api);
    const schema = loader.schema;
    const config = loader.config;
    const setConfig = loader.setConfig;
    const sessions = loader.sessions;
    const sessionConfigState = loader.sessionConfigState;

    // 仅追踪“模式 + 当前有效会话”，全局模式下切换选中会话不触发重复加载。
    const loadContextKey = mode === 'session' ? `session:${selectedSession}` : 'global';

    const draft = useConfigDraft({
        mode: mode,
        selectedSession: selectedSession,
        contextKey: loadContextKey,
        config: config,
        schema: schema,
        loading: loader.loading,
        expandedKeys: expandedKeys,
        isDirtyRef: isDirtyRef,
        scrollContainerRef: scrollContainerRef,
    });

    // 默认选中第一个会话，避免会话模式初始为空。
    // 优先恢复上次选中的会话，只有它已不存在（会话被删除）时才回退到第一项。
    // 恢复值来自缓存，因此不能简单地“有选中就跳过”，否则失效会话永远不会被纠正。
    useEffect(() => {
        if (sessions.length === 0) return;
        const exists = selectedSession
            && sessions.some((item) => item && item.session === selectedSession);
        if (exists) return;
        const saved = savedSessionRef.current;
        const savedExists = saved && sessions.some((item) => item && item.session === saved);
        const nextSession = savedExists ? saved : (sessions[0] && sessions[0].session);
        if (nextSession && nextSession !== selectedSession) setSelectedSession(nextSession);
    }, [sessions, selectedSession]);

    // 任何浏览上下文变化都写回缓存，供下次进入配置页时恢复。
    useEffect(() => {
        // 会话模式下尚未确定具体会话时不落盘，否则会存成空会话、丢掉上次选择。
        if (mode === 'session' && !selectedSession) return;
        // 同步“期望恢复的会话”，避免用户手动切换后被旧的恢复目标回拉。
        if (selectedSession) savedSessionRef.current = selectedSession;
        writeSavedViewContext(mode, selectedSession);
    }, [mode, selectedSession]);

    useEffect(() => {
        if (!schema) return;
        // 切换上下文时立即清除脏标记，防止旧草稿被写入新会话的 key。
        isDirtyRef.current = false;
        // 关键：先恢复该上下文的展开态，再发起加载。
        // 若等到 loadConfig resolve 后再 setExpandedKeys，期间 useConfigDraft 的
        // “记忆展开态” effect 会先用初始的空数组覆盖 localStorage，
        // 于是刷新/切换回来后展开状态永远是全部收起。
        setExpandedKeys(draft.restoreExpandedKeys(mode, selectedSession));
        // 过期请求会被 loadConfig 丢弃但仍正常 resolve，必须按采纳标记决定是否写入
        loader.loadConfig(mode, selectedSession).then((applied) => {
            if (!applied) return;
            isDirtyRef.current = false;
        });
        // 依赖只保留上下文标识；loader/draft 每次渲染都是新引用，不能直接作为依赖。
    }, [loadContextKey, schema]);

    const handleToggleExpand = (path) => {
        // 单个折叠面板的展开态切换逻辑集中处理，便于后续扩展批量控制。
        setExpandedKeys((prev) => {
            if (prev.includes(path)) {
                return prev.filter((p) => p !== path);
            }
            return [...prev, path];
        });
    };

    // 派生变量在渲染前统一计算，避免 JSX 中出现过多内联判断。
    const currentSessionType = window.SessionDomain.detectSessionType(selectedSession);
    const sessionSchemaEntries = mode === 'session' ? getSessionSchemaEntries(schema, currentSessionType) : [];
    const schemaEntries = mode === 'session' ? sessionSchemaEntries : Object.entries(schema || {});
    const sessionEnabled = mode === 'session' ? Boolean(config && config.enable) : false;
    const sessionTypeLabel = currentSessionType === 'group' ? '群聊' : '私聊';
    // config 为空时（无可用会话 / 请求失败）没有可提交内容，需一并禁用保存。
    const canSaveConfig = Boolean(config) && (mode !== 'session' || sessionConfigState.baseAvailable);
    const selectedSessionMeta = sessions.find((item) => item.session === selectedSession);
    const hasOverride = Boolean(selectedSessionMeta && selectedSessionMeta.has_override);

    const handleToggleAll = () => {
        // 会话模式只对会话可编辑字段做“全部展开”，不污染全局 Schema 的路径集合。
        const expandableSchema = mode === 'session'
            ? Object.fromEntries(sessionSchemaEntries)
            : schema;
        if (expandedKeys.length > 0) {
            setExpandedKeys([]);
        } else {
            setExpandedKeys(getAllExpandablePaths(expandableSchema || {}));
        }
    };

    const handleSave = async () => {
        // 保存逻辑按 mode 分叉：全局配置直接写配置块，会话模式则保存差异配置。
        setSaving(true);
        try {
            const cleanedConfig = cleanConfig(config);
            const currentMode = mode;
            const currentSession = selectedSession;

            if (currentMode === 'session') {
                if (!currentSession) return;
                const response = await api.updateSessionConfig(currentSession, {
                    mode: 'effective',
                    effective: cleanedConfig,
                });
                isDirtyRef.current = false;
                localStorage.removeItem(draft.getDraftKey(currentMode, currentSession));
                setConfig((response && response.effective) || cleanedConfig);
                if (window.Toast) {
                    window.Toast.success('会话差异配置已保存', {
                        title: '配置管理',
                        key: 'config-save',
                    });
                }
                await loader.loadSessions();
                // 会话模式保存后强制回读服务端 effective，确保会话隔离与覆写状态显示正确。
                await loader.loadConfig(currentMode, currentSession);
            } else {
                // 全局保存：按后端白名单提交所有分组，避免通知 / 遥测配置被静默丢弃。
                const payload = {};
                GLOBAL_CONFIG_KEYS.forEach((key) => {
                    if (cleanedConfig && cleanedConfig[key] !== undefined) {
                        payload[key] = cleanedConfig[key];
                    }
                });
                await api.updateConfig(payload);
                isDirtyRef.current = false;
                setConfig(cleanedConfig);
                localStorage.removeItem(draft.getDraftKey(currentMode, currentSession));
                if (window.Toast) {
                    window.Toast.success('全局配置已保存', {
                        title: '配置管理',
                        key: 'config-save',
                    });
                }
            }
        } catch (e) {
            console.error('保存配置失败', e);
            const message = (e && e.message) || '保存配置失败，请检查后端返回信息';
            if (window.Toast) window.Toast.error(message, { title: '保存配置失败', key: 'config-save' });
            else window.alert(message);
        } finally {
            setSaving(false);
        }
    };

    const handleResetOverride = async () => {
        // 仅会话模式下可用：清空后该会话重新完全继承全局配置。
        if (!selectedSession) return;

        const ok = window.confirm('确定要清空该会话的差异配置吗？\n\n清空后将完全继承全局默认配置。');
        if (!ok) return;

        setSaving(true);
        try {
            await api.resetSessionConfig(selectedSession);
            localStorage.removeItem(draft.getDraftKey(mode, selectedSession));
            if (window.Toast) window.Toast.success('会话差异配置已清空');
            await loader.loadSessions();
            await loader.loadConfig(mode, selectedSession);
        } catch (e) {
            console.error('清空会话差异配置失败', e);
            const message = (e && e.message) || '清空会话差异配置失败';
            if (window.Toast) window.Toast.error(message, { title: '清空失败' });
            else window.alert(message);
        } finally {
            setSaving(false);
        }
    };

    const handleRestoreDefaults = () => {
        const ok = window.confirm('⚠️ 确定要恢复出厂设置吗？\n\n这将覆盖当前所有配置项为默认值（需要点击“保存配置”才能生效）。');
        if (!ok) return;

        if (mode === 'session') {
            const sessionDefaults = { enable: true };
            schemaEntries.forEach(([key, value]) => {
                if (value && value.type === 'object' && value.items) {
                    sessionDefaults[key] = generateDefaults(value.items);
                } else {
                    sessionDefaults[key] = value && value.default !== undefined ? value.default : null;
                }
            });
            isDirtyRef.current = true;
            setConfig(sessionDefaults);
        } else {
            const defaults = generateDefaults(schema);
            isDirtyRef.current = true;
            setConfig(defaults);
        }
        localStorage.removeItem(draft.getDraftKey());
        if (window.Toast) {
            window.Toast.info('已填入默认值，记得点击“保存配置”才会生效', { title: '恢复默认' });
        }
    };

    // 撤销更改：成功后需要明确告诉用户草稿已丢弃，否则容易怀疑是否真的生效。
    // 草稿必须在加载成功后再删除：失败时保留，用户还能找回未保存的修改。
    const handleDiscardChanges = async () => {
        if (!window.confirm('确定要撤销所有未保存的更改吗？\n\n这将重新加载服务器上已保存的配置。')) return;
        const applied = await loader.loadConfig(mode, selectedSession);
        if (!applied) return;
        localStorage.removeItem(draft.getDraftKey());
        isDirtyRef.current = false;
        if (window.Toast) window.Toast.success('未保存的更改已撤销', { title: '配置管理' });
    };

    if (loader.loading) {
        return (
            <Box sx={{ textAlign: 'center', py: 6 }}>
                <Box sx={{ fontSize: '36px', mb: 1.5 }}>⚙️</Box>
                <Typography variant="body1" color="text.secondary">加载配置中...</Typography>
            </Box>
        );
    }

    // 仅 Schema 缺失时整页不可用；config 为空仍保留顶部控制区，否则用户会被困在会话模式。
    if (!schema) {
        return (
            <Box sx={{ textAlign: 'center', py: 6 }}>
                <Box sx={{ fontSize: '36px', mb: 1.5 }}>❌</Box>
                <Typography variant="body1" color="error">无法加载配置</Typography>
            </Box>
        );
    }

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden' }}>
            {/* 顶部控制区：负责在“全局配置”和“会话差异配置”两种编辑模式间切换。 */}
            <Box sx={{ px: 3, pt: 2, pb: 1, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
                    <ToggleButtonGroup
                        exclusive
                        size="small"
                        value={mode}
                        onChange={(e, val) => {
                            if (!val) return;
                            setMode(val);
                        }}
                    >
                        <ToggleButton value="global">全局配置</ToggleButton>
                        <ToggleButton value="session">会话差异配置</ToggleButton>
                    </ToggleButtonGroup>

                    {mode === 'session' && (
                        <>
                            <TextField
                                select
                                size="small"
                                label="目标会话"
                                value={selectedSession}
                                onChange={(e) => setSelectedSession(e.target.value)}
                                sx={{ minWidth: 420, maxWidth: '100%' }}
                            >
                                {sessions.map((item) => {
                                    const displayName = item.session_display_name || item.session_name || item.session;
                                    const hasAlias = Boolean(item.session_name && item.session_name.trim());
                                    const optionText = hasAlias ? `${displayName}（${item.session}）` : displayName;
                                    return (
                                        <MenuItem key={item.session} value={item.session}>
                                            {optionText}
                                        </MenuItem>
                                    );
                                })}
                            </TextField>
                            {hasOverride && (
                                <Chip size="small" color="primary" label="已存在差异覆写" />
                            )}
                            {loader.sessionLoading && (
                                <Typography variant="caption" color="text.secondary">会话配置加载中...</Typography>
                            )}
                        </>
                    )}
                </Box>

                {mode === 'session' && selectedSessionMeta && (
                    // 会话基础信息与生效人格合并为一行，避免多行小字堆叠造成视觉噪音。
                    <Typography variant="caption" color="text.secondary" sx={{ lineHeight: 1.7 }}>
                        当前会话：{selectedSessionMeta.session_display_name || selectedSessionMeta.session_name || selectedSessionMeta.session}
                        {selectedSessionMeta.session_name ? ` ｜ UMO：${selectedSessionMeta.session}` : ''}
                        {' ｜ '}未回复次数：{selectedSessionMeta.unanswered_count !== undefined ? selectedSessionMeta.unanswered_count : 0}
                        {' ｜ '}
                        <Box component="span" sx={{ fontWeight: 700, color: 'primary.main' }}>
                            生效人格：{resolvePersonaLabel(loader.sessionPersona)}
                        </Box>
                    </Typography>
                )}
                {mode === 'session' && !loader.sessionConfigState.baseAvailable && loader.sessionConfigState.message && (
                    <Box className="config-inline-notice is-warning">
                        <Typography variant="caption" className="config-inline-notice-text">
                            {loader.sessionConfigState.message}
                        </Typography>
                    </Box>
                )}
            </Box>

            {/* 主编辑区：根据 schemaEntries 动态递归渲染所有当前可见配置项。 */}
            <Box ref={scrollContainerRef} className="config-editor-scroll">
                {!config ? (
                    <Box sx={{ textAlign: 'center', py: 6 }}>
                        <Box sx={{ fontSize: '36px', mb: 1.5 }}>❌</Box>
                        <Typography variant="body1" color="error">无法加载配置</Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                            请切换目标会话，或返回全局配置后重试
                        </Typography>
                    </Box>
                ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                    {mode === 'session' && config && (
                        <Box sx={{ pb: 0.5 }}>
                            <Paper
                                elevation={0}
                                sx={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'space-between',
                                    gap: 2,
                                    px: 2.5,
                                    py: 1.75,
                                    borderRadius: 2.5,
                                    border: '1px solid',
                                    borderColor: sessionEnabled
                                        ? 'rgb(var(--md-primary-rgb) / 0.22)'
                                        : 'rgb(var(--md-error-rgb) / 0.18)',
                                    background: sessionEnabled
                                        ? 'linear-gradient(135deg, rgb(var(--md-primary-rgb) / 0.08) 0%, rgb(var(--md-primary-rgb) / 0.03) 100%)'
                                        : 'linear-gradient(135deg, rgb(var(--md-error-rgb) / 0.08) 0%, rgb(var(--md-error-rgb) / 0.03) 100%)'
                                }}
                            >
                                <Box sx={{ minWidth: 0 }}>
                                    <Typography variant="subtitle1" sx={{ fontWeight: 800, color: 'text.primary', mb: 0.25 }}>
                                        {sessionTypeLabel}会话启用状态
                                    </Typography>
                                    <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.5 }}>
                                        这是当前会话的独立开关。关闭后，该会话会暂停主动消息，但不会影响同类型的全局配置与其他会话。
                                    </Typography>
                                </Box>
                                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexShrink: 0 }}>
                                    <Chip
                                        size="small"
                                        color={sessionEnabled ? 'success' : 'default'}
                                        label={sessionEnabled ? '已启用' : '已暂停'}
                                        variant={sessionEnabled ? 'filled' : 'outlined'}
                                    />
                                    <Switch
                                        checked={sessionEnabled}
                                        onChange={(e) => {
                                            isDirtyRef.current = true;
                                            setConfig(Object.assign({}, config, { enable: e.target.checked }));
                                        }}
                                    />
                                </Box>
                            </Paper>
                        </Box>
                    )}
                    {schemaEntries.map(([key, subSchema]) => (
                        <Box key={key} sx={{ width: '100%' }}>
                            <ConfigField
                                fieldKey={key}
                                schema={subSchema}
                                value={config[key]}
                                onChange={(newValue) => {
                                    isDirtyRef.current = true;
                                    setConfig(Object.assign({}, config, { [key]: newValue }));
                                }}
                                path=""
                                expandedKeys={expandedKeys}
                                onToggleExpand={handleToggleExpand}
                            />
                        </Box>
                    ))}
                    </Box>
                )}
            </Box>

            {/* 底部操作栏：集中放置折叠控制、恢复默认、撤销、清空覆写与保存操作。 */}
            <Box className="config-action-bar">
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 500 }}>
                        {schemaEntries.length} 个配置组
                    </Typography>
                    <Button
                        onClick={handleToggleAll}
                        size="small"
                        variant="text"
                        sx={{ minWidth: 'auto', px: 1 }}
                    >
                        {expandedKeys.length > 0 ? '全部收起' : '全部展开'}
                    </Button>
                </Box>
                <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    <Button
                        onClick={handleRestoreDefaults}
                        disabled={saving}
                        variant="outlined"
                        color="error"
                        size="medium"
                        startIcon={<span>🗑️</span>}
                        sx={{ minWidth: 100, borderRadius: 2, borderWidth: 1.5, fontSize: '0.875rem', '&:hover': { borderWidth: 1.5 } }}
                    >
                        恢复默认
                    </Button>
                    <Button
                        onClick={handleDiscardChanges}
                        disabled={saving}
                        variant="outlined"
                        size="medium"
                        startIcon={<span>↩️</span>}
                        sx={{ minWidth: 100, borderRadius: 2, borderWidth: 1.5, fontSize: '0.875rem', '&:hover': { borderWidth: 1.5 } }}
                    >
                        撤销更改
                    </Button>
                    {mode === 'session' && (
                        <Button
                            onClick={handleResetOverride}
                            disabled={saving || !selectedSession}
                            variant="outlined"
                            color="warning"
                            size="medium"
                            startIcon={<span>♻️</span>}
                            sx={{ minWidth: 130, borderRadius: 2, borderWidth: 1.5, fontSize: '0.875rem', '&:hover': { borderWidth: 1.5 } }}
                        >
                            清空会话覆写
                        </Button>
                    )}
                    <Button
                        variant="contained"
                        onClick={handleSave}
                        disabled={saving || !canSaveConfig}
                        size="medium"
                        startIcon={<span>💾</span>}
                        sx={{
                            minWidth: 120,
                            borderRadius: 2,
                            fontSize: '0.875rem',
                            boxShadow: '0 2px 8px rgb(var(--md-primary-rgb) / 0.3)',
                            '&:hover': {
                                boxShadow: '0 4px 12px rgb(var(--md-primary-rgb) / 0.4)',
                            }
                        }}
                    >
                        {saving ? '保存中...' : (!canSaveConfig && mode === 'session' ? '当前会话不可保存' : (mode === 'session' ? '保存会话配置' : '保存配置'))}
                    </Button>
                </Box>
            </Box>
        </Box>
    );
}

window.ConfigEditor = ConfigEditor;
})();
