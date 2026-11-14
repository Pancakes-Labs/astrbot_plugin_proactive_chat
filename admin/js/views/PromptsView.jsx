(() => {
/**
 * 文件职责：提示词管理专用页。
 *
 * 与配置页的差异：配置页只能逐条孤立编辑，看不到「权重合起来是什么效果」。
 * 本页把跨条目信息摊开——实时概率分布、抽样预演、完整 Prompt 预览、
 * 导入导出与出厂重置，这些都是配置页结构上做不到的事情。
 *
 * 数据来源与配置页同源，不存在两份事实。两个页面各自保存后都会广播 WebSocket 更新，
 * 本页监听 config / prompts 事件自动重载，避免停留在过期快照上。
 */

const {
    Box, Typography, Paper, Chip, Button, IconButton, TextField, Switch, MenuItem,
    Slider, Divider, Tooltip, Tabs, Tab, CircularProgress, Alert, Dialog,
    DialogTitle, DialogContent, DialogActions, LinearProgress,
} = MaterialUI;
const { useState, useEffect, useMemo, useCallback } = React;

// 视图内使用的会话类型选项。
const SESSION_TYPES = [
    { value: 'friend', label: '私聊', icon: '💬' },
    { value: 'group', label: '群聊', icon: '👥' },
];

// 上下文来源选项，需与后端 source_mode 取值一致。
const SOURCE_MODES = [
    { value: 'conversation_history', label: '对话历史' },
    { value: 'platform_message_history', label: '平台流水' },
    { value: 'hybrid', label: '混合' },
];

// 模板正文建议的段头，与后端 prompt_presets 保持一致。
const HEADER_INTENT = '[这次我想]';
const HEADER_FINAL = '[最终指令]';

const SCOPE_LABELS = { '': '通用', friend: '仅私聊', group: '仅群聊' };

function createTemplateId() {
    return `tpl_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function createEmptyTemplate(scope) {
    return {
        id: createTemplateId(),
        name: '',
        text: `${HEADER_INTENT}\n\n\n${HEADER_FINAL}\n`,
        enabled: true,
        weight: 100,
        scope: scope || '',
        __template_key: 'prompt_template',
    };
}

// 页面内唯一键自增源。
let __uiKeySeed = 0;

function nextUiKey() {
    __uiKeySeed += 1;
    return `ui_${__uiKeySeed}`;
}

// 归一化：保证受控输入始终拿到完整结构，未知扩展字段原样保留。
// __ui_key 仅存在于前端状态，保存前由 stripUiKey 剥离。
function normalizeItem(item) {
    if (!item || typeof item !== 'object') {
        const created = createEmptyTemplate();
        return { ...created, __ui_key: nextUiKey() };
    }
    const extra = {};
    Object.keys(item).forEach((key) => {
        if (['id', 'name', 'text', 'enabled', 'weight', 'scope', '__template_key', '__ui_key'].includes(key)) return;
        extra[key] = item[key];
    });
    return {
        ...extra,
        __template_key: item.__template_key || 'prompt_template',
        __ui_key: item.__ui_key || nextUiKey(),
        id: item.id || '',
        name: item.name || '',
        text: typeof item.text === 'string' ? item.text : '',
        enabled: item.enabled !== false,
        weight: Number.isFinite(Number(item.weight)) ? Number(item.weight) : 100,
        scope: item.scope || '',
    };
}

// 提交前剥掉页面专用字段，避免污染配置。
function stripUiKey(items) {
    return (items || []).map(({ __ui_key, ...rest }) => rest);
}

// 与后端 _is_usable_template() 保持一致的排除项。
// 含流水载荷的模板是「包裹聊天记录」的外壳，不是开场白，后端不会拿它来抽样；
// 前端若把它计入分母，显示的概率就会低于真实抽中概率。
const PLATFORM_HISTORY_PAYLOAD = '{{platform_history_lines}}';

// 与后端 build_candidate_pool 同源的可用性判定。
function isUsableTemplate(item) {
    const text = String(item && item.text ? item.text : '');
    if (!text.trim()) return false;
    return !text.includes(PLATFORM_HISTORY_PAYLOAD);
}

// 计算某个池内每条模板的显示概率（前端预估，仅用于实时反馈）。
function poolProbabilities(templates, sessionType) {
    const enabled = templates.filter(
        (item) => item.enabled && isUsableTemplate(item)
            && (!item.scope || item.scope === sessionType),
    );
    // 与本会话类型精确匹配时只用这批，否则退回通用模板。
    const scoped = enabled.filter((item) => item.scope === sessionType);
    const pool = scoped.length ? scoped : enabled.filter((item) => !item.scope);

    const total = pool.reduce((sum, item) => sum + Number(item.weight || 0), 0);
    const map = {};
    pool.forEach((item) => {
        map[item.__ui_key] = total > 0
            ? Number(item.weight || 0) / total
            : 1 / (pool.length || 1);
    });
    return { map, pool, total };
}

// 正文格式检查：与后端 save_templates 的告警规则保持一致。
function checkTextIssues(text) {
    const issues = [];
    const body = String(text || '');
    if (!body.trim()) {
        issues.push('正文为空，这条模板不会参与抽样。');
        return issues;
    }
    if (body.includes(PLATFORM_HISTORY_PAYLOAD)) {
        issues.push(`正文含 ${PLATFORM_HISTORY_PAYLOAD}，那是包裹聊天记录的载荷，`
            + '不是开场白，该条不会参与抽样。');
    }
    if (!body.trimStart().startsWith('[') && body.includes(HEADER_FINAL)) {
        issues.push(`正文不以 [ 开头却内含 ${HEADER_FINAL}，实际注入时可能出现重复标题。`);
    }
    const unknown = (body.match(/\{\{[^{}]*\}\}/g) || [])
        .filter((token) => !['{{current_time}}', '{{unanswered_count}}',
            '{{conversation_type}}', '{{platform_history_lines}}',
            '{{platform_history_last_time}}'].includes(token));
    if (unknown.length) {
        issues.push(`无法识别的占位符：${Array.from(new Set(unknown)).join('、')}`);
    }
    return issues;
}

// 概率条：横条 + 百分比，宽度即概率。
// 通用模板在两个池里的分母不同，真实概率是两个值，因此支持传入数组并
// 并列展示（如「12% / 18%」）——只显示最大值会让用户高估实际抽中率。
function ProbabilityBar({ value, color }) {
    const values = Array.isArray(value) ? value : [value];
    // 条形长度取最大值，仅用于视觉对比；文字始终列出全部数值。
    const percent = Math.max(...values.map((v) => (v || 0) * 100)) || 0;
    const label = values
        .map((v) => `${Math.round((v || 0) * 1000) / 10}%`)
        .join(' / ');
    return (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%' }}>
            <Box sx={{ flex: 1, height: 6, borderRadius: 3, bgcolor: 'action.hover', overflow: 'hidden' }}>
                <Box
                    sx={{
                        width: `${Math.max(percent, 1.5)}%`,
                        height: '100%',
                        borderRadius: 3,
                        bgcolor: color || 'primary.main',
                        transition: 'width .28s ease',
                    }}
                />
            </Box>
            <Typography
                variant="caption"
                className="mono"
                sx={{ minWidth: 46, textAlign: 'right', fontWeight: 700, whiteSpace: 'nowrap' }}
            >
                {label}
            </Typography>
        </Box>
    );
}

// 单条模板编辑器。
function TemplateCard({ item, index, probability, onChange, onRemove }) {
    const [expanded, setExpanded] = useState(false);
    const data = normalizeItem(item);
    const issues = checkTextIssues(data.text);
    // 正文行数用于决定默认展开高度，短模板不占满屏。
    const lineCount = data.text.split('\n').length;

    return (
        <Paper
            elevation={0}
            sx={{
                mb: 1.5,
                border: 1,
                borderColor: data.enabled ? 'divider' : 'action.disabledBackground',
                borderRadius: 2.5,
                overflow: 'hidden',
                opacity: data.enabled ? 1 : 0.62,
                transition: 'border-color .2s ease, opacity .2s ease',
            }}
        >
            {/* 头部：序号 / 名称 / 范围 / 权重概率 / 操作 */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, p: 1.75 }}>
                <Typography
                    variant="caption"
                    className="mono"
                    sx={{ opacity: 0.45, minWidth: 20, fontWeight: 700 }}
                >
                    {String(index + 1).padStart(2, '0')}
                </Typography>

                <TextField
                    size="small"
                    variant="standard"
                    value={data.name}
                    placeholder="未命名模板"
                    onChange={(e) => onChange({ name: e.target.value })}
                    InputProps={{ disableUnderline: true }}
                    sx={{ flex: 1, minWidth: 90 }}
                />

                <Chip
                    size="small"
                    label={SCOPE_LABELS[data.scope] || '通用'}
                    variant="outlined"
                    sx={{ height: 22, fontSize: '0.72rem' }}
                />

                <Box sx={{ width: 130 }}>
                    <ProbabilityBar value={probability} />
                </Box>

                <Tooltip title={data.enabled ? '停用该模板' : '启用该模板'}>
                    <Switch
                        size="small"
                        checked={data.enabled}
                        onChange={(e) => onChange({ enabled: e.target.checked })}
                    />
                </Tooltip>

                <IconButton size="small" onClick={() => setExpanded((v) => !v)} title="展开 / 收起">
                    <Typography component="span" sx={{ fontSize: '0.85rem' }}>
                        {expanded ? '▾' : '▸'}
                    </Typography>
                </IconButton>

                <IconButton size="small" onClick={onRemove} title="删除该模板">
                    <Typography component="span" sx={{ fontSize: '0.9rem' }}>✕</Typography>
                </IconButton>
            </Box>

            {/* 副行：权重滑块 + 适用范围 */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, px: 1.75, pb: 1.5 }}>
                <Typography variant="caption" color="text.secondary" sx={{ minWidth: 30 }}>
                    权重
                </Typography>
                <Slider
                    size="small"
                    min={1}
                    max={100}
                    value={data.weight}
                    onChange={(e, value) => onChange({ weight: value })}
                    sx={{ maxWidth: 240 }}
                />
                <Typography variant="caption" className="mono" sx={{ minWidth: 26, fontWeight: 700 }}>
                    {data.weight}
                </Typography>

                <TextField
                    select
                    size="small"
                    variant="standard"
                    value={data.scope}
                    onChange={(e) => onChange({ scope: e.target.value })}
                    InputProps={{ disableUnderline: true }}
                    sx={{ minWidth: 92 }}
                >
                    <MenuItem value="">通用</MenuItem>
                    <MenuItem value="friend">仅私聊</MenuItem>
                    <MenuItem value="group">仅群聊</MenuItem>
                </TextField>
            </Box>

            {/* 正文编辑区：默认折叠，只显示前几行预览 */}
            {expanded ? (
                <Box sx={{ px: 1.75, pb: 1.75 }}>
                    <TextField
                        fullWidth
                        multiline
                        size="small"
                        value={data.text}
                        onChange={(e) => onChange({ text: e.target.value })}
                        minRows={Math.min(Math.max(lineCount, 4), 14)}
                        maxRows={24}
                        InputProps={{ className: 'mono', sx: { fontSize: '0.83rem', lineHeight: 1.7 } }}
                    />
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.6, mt: 1 }}>
                        <Typography variant="caption" color="text.secondary" sx={{ mr: 0.5 }}>
                            插入占位符：
                        </Typography>
                        {['{{current_time}}', '{{unanswered_count}}',
                            '{{conversation_type}}', '{{platform_history_last_time}}'].map((token) => (
                            <Chip
                                key={token}
                                size="small"
                                variant="outlined"
                                label={token.replace(/[{}]/g, '')}
                                onClick={() => onChange({ text: `${data.text}${token}` })}
                                sx={{ height: 22, fontSize: '0.7rem', cursor: 'pointer' }}
                            />
                        ))}
                    </Box>
                </Box>
            ) : (
                <Box
                    sx={{ px: 1.75, pb: 1.75, cursor: 'pointer' }}
                    onClick={() => setExpanded(true)}
                >
                    <Typography
                        variant="body2"
                        color="text.secondary"
                        sx={{
                            fontSize: '0.8rem',
                            lineHeight: 1.65,
                            display: '-webkit-box',
                            WebkitLineClamp: 2,
                            WebkitBoxOrient: 'vertical',
                            overflow: 'hidden',
                        }}
                    >
                        {data.text.replace(/\n+/g, ' · ').slice(0, 120) || '（正文为空）'}
                    </Typography>
                </Box>
            )}

            {issues.length > 0 && (
                <Box sx={{ px: 1.75, pb: 1.5 }}>
                    {issues.map((issue) => (
                        <Typography
                            key={issue}
                            variant="caption"
                            sx={{ display: 'block', color: 'warning.main', lineHeight: 1.6 }}
                        >
                            ⚠ {issue}
                        </Typography>
                    ))}
                </Box>
            )}
        </Paper>
    );
}

function PromptsView() {
    const api = useApi();
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [templates, setTemplates] = useState([]);
    const [dirty, setDirty] = useState(false);

    // 左侧列表筛选。
    const [filterType, setFilterType] = useState('all');
    const [filterState, setFilterState] = useState('all');

    // 右侧面板。
    const [simulateResult, setSimulateResult] = useState(null);
    const [simulating, setSimulating] = useState(false);
    const [preview, setPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);
    const [previewType, setPreviewType] = useState('friend');
    const [previewMode, setPreviewMode] = useState('conversation_history');
    const [previewUnanswered, setPreviewUnanswered] = useState(0);
    const [importOpen, setImportOpen] = useState(false);
    const [importText, setImportText] = useState('');

    // dirty 放进 ref：事件监听不需要因为 dirty 变化而重新订阅。
    const dirtyRef = React.useRef(false);
    useEffect(() => {
        dirtyRef.current = dirty;
    }, [dirty]);

    // 模板同样放进 ref：保存回调用它比对"期间是否又被编辑过"。
    const templatesRef = React.useRef(templates);
    useEffect(() => {
        templatesRef.current = templates;
    }, [templates]);

    // 加载序号：首屏加载与多次广播可能并发，只有最新请求能写状态。
    // 旧响应晚到时若直接落库，会把保存后的新模板回滚成过期快照。
    const loadSeqRef = React.useRef(0);

    // 让所有进行中的加载失效：保存、导入、重置后调用。
    const invalidateLoads = useCallback(() => {
        loadSeqRef.current += 1;
    }, []);

    // silent=true 用于后台同步：不切 loading、不清草稿。
    // 有未保存改动时不会被后台同步冲掉，否则用户输入会凭空消失。
    const load = useCallback(async (silent) => {
        loadSeqRef.current += 1;
        const seq = loadSeqRef.current;
        if (!silent) setLoading(true);
        if (!silent) setError('');
        try {
            const data = await api.listPrompts();
            // 已有更新的请求或已被作废：丢弃这次结果。
            if (seq !== loadSeqRef.current) return;
            if (silent && dirtyRef.current) return;
            setTemplates((data.templates || []).map(normalizeItem));
            setDirty(false);
        } catch (e) {
            if (seq !== loadSeqRef.current) return;
            // 后台同步失败不打断当前编辑，只在首屏加载时展示错误。
            if (!silent) setError(e.message || '加载提示词失败');
        } finally {
            if (!silent && seq === loadSeqRef.current) setLoading(false);
        }
    }, [api]);

    useEffect(() => {
        load(false);
    }, [load]);

    // 配置页保存或本页保存后后端都会广播 update，这里主动同步。
    useEffect(() => {
        const onUpdated = (event) => {
            const reason = event && event.detail ? event.detail.reason : '';
            // 只关心与提示词相关的广播，避免计时器刷新造成无谓请求。
            if (reason && !['config', 'prompts', 'session-config'].includes(reason)) return;
            load(true);
        };
        window.addEventListener('proactive:data-updated', onUpdated);
        return () => window.removeEventListener('proactive:data-updated', onUpdated);
    }, [load]);

    // 两个池的概率分布：前端实时计算，用户拖滑块能立刻看到变化。
    const friendPool = useMemo(() => poolProbabilities(templates, 'friend'), [templates]);
    const groupPool = useMemo(() => poolProbabilities(templates, 'group'), [templates]);

    // 列表筛选：按适用范围与启用状态。
    const visibleItems = useMemo(() => {
        return templates.filter((item) => {
            // 通用模板（scope 为空）在私聊/群聊筛选下都应当可见；
            // 通用标签页则只显示通用模板，与标签上的计数保持同一口径。
            if (filterType === 'any') {
                if (item.scope) return false;
            } else if (filterType !== 'all') {
                if (item.scope && item.scope !== filterType) return false;
            }
            if (filterState === 'enabled' && !item.enabled) return false;
            if (filterState === 'disabled' && item.enabled) return false;
            return true;
        });
    }, [templates, filterType, filterState]);

    const patchAt = (index, patch) => {
        setTemplates((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)));
        setDirty(true);
    };

    const removeAt = (index) => {
        setTemplates((prev) => prev.filter((_, i) => i !== index));
        setDirty(true);
    };

    const addItem = () => {
        const scope = filterType === 'friend' || filterType === 'group' ? filterType : '';
        // 新建项同样需要页面内唯一键，否则概率表与卡片 key 会缺失。
        setTemplates((prev) => [
            ...prev,
            normalizeItem(createEmptyTemplate(scope)),
        ]);
        setDirty(true);
    };

    const save = async () => {
        setSaving(true);
        setError('');
        // 保存会改变服务端数据，先作废进行中的加载，避免旧快照覆盖结果。
        invalidateLoads();
        // 记录提交时的快照：请求返回后若用户又改过，不能用旧响应覆盖新输入。
        const submitted = templates;
        try {
            const data = await api.updatePrompts(stripUiKey(submitted));
            setTemplates((prev) => (prev === submitted
                ? (data.templates || []).map(normalizeItem)
                : prev));
            if (templatesRef.current === submitted) setDirty(false);
            if (window.Toast) {
                const warnings = data.warnings || [];
                if (warnings.length) {
                    window.Toast.warning(warnings[0], {
                        title: `已保存，但有 ${warnings.length} 条提示`,
                        description: warnings.slice(1, 3).join('\n'),
                    });
                } else {
                    window.Toast.success('提示词已保存', { title: '保存成功' });
                }
            }
        } catch (e) {
            setError(e.message || '保存失败');
        } finally {
            setSaving(false);
        }
    };

    const simulate = async (sessionType) => {
        setSimulating(true);
        try {
            const data = await api.simulatePrompts({
                session_type: sessionType,
                rounds: 2000,
                // 带上未保存的草稿：否则预演结果与左侧概率面板对不上。
                templates: dirtyRef.current
                    ? stripUiKey(templatesRef.current)
                    : undefined,
            });
            setSimulateResult(data);
        } catch (e) {
            setError(e.message || '预演失败');
        } finally {
            setSimulating(false);
        }
    };

    const runPreview = async () => {
        setPreviewing(true);
        try {
            const data = await api.previewPrompt({
                session_type: previewType,
                source_mode: previewMode,
                unanswered_count: previewUnanswered,
                // 与预演一致：有草稿时按草稿渲染，预览才反映用户眼前的改动。
                templates: dirtyRef.current
                    ? stripUiKey(templatesRef.current)
                    : undefined,
            });
            setPreview(data);
        } catch (e) {
            setError(e.message || '预览失败');
        } finally {
            setPreviewing(false);
        }
    };

    const doExport = async () => {
        try {
            const data = await api.exportPrompts();
            const blob = new Blob([JSON.stringify(data, null, 2)], {
                type: 'application/json',
            });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `proactive-prompts-${new Date().toISOString().slice(0, 10)}.json`;
            link.click();
            URL.revokeObjectURL(url);
            if (window.Toast) window.Toast.success('模板已导出为 JSON 文件');
        } catch (e) {
            setError(e.message || '导出失败');
        }
    };

    const doImport = async () => {
        let parsed;
        try {
            parsed = JSON.parse(importText);
        } catch (e) {
            setError('导入内容不是合法的 JSON');
            return;
        }
        // 导入会替换全部模板，先作废进行中的加载，避免旧快照回写。
        invalidateLoads();
        try {
            const data = await api.importPrompts(parsed);
            setTemplates((data.templates || []).map(normalizeItem));
            setDirty(false);
            setImportOpen(false);
            setImportText('');
            if (window.Toast) {
                window.Toast.success(`已导入 ${data.imported} 条模板`);
            }
        } catch (e) {
            setError(e.message || '导入失败');
        }
    };

    const doReset = async () => {
        if (!window.confirm('将把所有模板恢复为出厂预设，当前改动会被覆盖（迁移备份文件仍保留）。确定继续？')) {
            return;
        }
        invalidateLoads();
        try {
            const data = await api.resetPrompts();
            setTemplates((data.templates || []).map(normalizeItem));
            setDirty(false);
            if (window.Toast) {
                window.Toast.success(`已恢复 ${data.restored} 条出厂模板`);
            }
        } catch (e) {
            setError(e.message || '恢复失败');
        }
    };

    if (loading) {
        return (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
                <CircularProgress size={28} />
            </Box>
        );
    }

    return (
        <Box sx={{ pb: 4 }}>
            {/* 页头 */}
            <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2, mb: 2, flexWrap: 'wrap' }}>
                <Box sx={{ flex: 1, minWidth: 260 }}>
                    <Typography variant="h6" sx={{ fontWeight: 800, letterSpacing: '-0.5px' }}>
                        💬 提示词管理
                    </Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                        每套模板只负责一种开口动机。每轮按权重随机抽一套，
                        让 bot 在不同时间有不同的开场姿态。
                    </Typography>
                </Box>
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    <Button size="small" variant="outlined" onClick={() => setImportOpen(true)}>
                        导入
                    </Button>
                    <Button size="small" variant="outlined" onClick={doExport}>
                        导出
                    </Button>
                    <Button size="small" variant="outlined" color="warning" onClick={doReset}>
                        恢复出厂
                    </Button>
                    <Button
                        size="small"
                        variant="contained"
                        onClick={save}
                        disabled={saving || !dirty}
                    >
                        {saving ? '保存中…' : dirty ? '保存改动' : '已保存'}
                    </Button>
                </Box>
            </Box>

            {error && (
                <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>
                    {error}
                </Alert>
            )}

            <Box sx={{ display: 'flex', gap: 2, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                {/* 左：模板列表 */}
                <Box sx={{ flex: '1 1 520px', minWidth: 0 }}>
                    <Paper elevation={0} sx={{ border: 1, borderColor: 'divider', borderRadius: 2.5, p: 1.75 }}>
                        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mb: 1.5, flexWrap: 'wrap' }}>
                            <Tabs
                                value={filterType}
                                onChange={(e, value) => setFilterType(value)}
                                sx={{ minHeight: 32 }}
                            >
                                <Tab value="all" label={`全部 ${templates.length}`} sx={{ minHeight: 32, py: 0 }} />
                                <Tab
                                    value="friend"
                                    // 计数与列表同口径：该池实际可用的模板 = 专用 + 通用。
                                    label={`私聊 ${templates.filter((t) => !t.scope || t.scope === 'friend').length}`}
                                    sx={{ minHeight: 32, py: 0 }}
                                />
                                <Tab
                                    value="group"
                                    label={`群聊 ${templates.filter((t) => !t.scope || t.scope === 'group').length}`}
                                    sx={{ minHeight: 32, py: 0 }}
                                />
                                <Tab
                                    value="any"
                                    label={`通用 ${templates.filter((t) => !t.scope).length}`}
                                    sx={{ minHeight: 32, py: 0 }}
                                />
                            </Tabs>
                            <Box sx={{ flex: 1 }} />
                            <TextField
                                select
                                size="small"
                                variant="standard"
                                value={filterState}
                                onChange={(e) => setFilterState(e.target.value)}
                                InputProps={{ disableUnderline: true }}
                                sx={{ minWidth: 84 }}
                            >
                                <MenuItem value="all">全部状态</MenuItem>
                                <MenuItem value="enabled">已启用</MenuItem>
                                <MenuItem value="disabled">已停用</MenuItem>
                            </TextField>
                        </Box>

                        <Divider sx={{ mb: 1.5 }} />

                        {visibleItems.length === 0 && (
                            <Box sx={{ py: 5, textAlign: 'center' }}>
                                <Typography variant="body2" color="text.secondary">
                                    当前筛选下没有模板。
                                </Typography>
                            </Box>
                        )}

                        {visibleItems.map((item) => {
                            const index = templates.indexOf(item);
                            // 专用模板只在自己的池里；通用模板同时服务两个池，
                            // 分母不同所以按各自的池内归一化分别展示。
                            const probability = item.scope === 'friend'
                                ? friendPool.map[item.__ui_key]
                                : item.scope === 'group'
                                    ? groupPool.map[item.__ui_key]
                                    : [
                                        friendPool.map[item.__ui_key] || 0,
                                        groupPool.map[item.__ui_key] || 0,
                                    ];
                            return (
                                <TemplateCard
                                    key={item.__ui_key}
                                    item={item}
                                    index={index}
                                    probability={probability}
                                    onChange={(patch) => patchAt(index, patch)}
                                    onRemove={() => removeAt(index)}
                                />
                            );
                        })}

                        <Button fullWidth variant="outlined" size="small" onClick={addItem} sx={{ mt: 0.5 }}>
                            + 新增模板
                        </Button>
                    </Paper>
                </Box>

                {/* 右：概率分布 / 预演 / 预览 */}
                <Box sx={{ flex: '0 1 400px', minWidth: 300, position: 'sticky', top: 12 }}>
                    {/* 概率分布 */}
                    <Paper elevation={0} sx={{ border: 1, borderColor: 'divider', borderRadius: 2.5, p: 1.75, mb: 2 }}>
                        <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 1.25 }}>
                            📊 抽中概率
                        </Typography>
                        {['friend', 'group'].map((sessionType) => {
                            const pool = sessionType === 'friend' ? friendPool : groupPool;
                            const label = sessionType === 'friend' ? '私聊池' : '群聊池';
                            return (
                                <Box key={sessionType} sx={{ mb: 1.75 }}>
                                    <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.6 }}>
                                        <Typography variant="caption" sx={{ fontWeight: 700 }}>
                                            {label}
                                        </Typography>
                                        <Typography variant="caption" color="text.secondary">
                                            {pool.pool.length} 套 · 权重 {pool.total}
                                        </Typography>
                                    </Box>
                                    {pool.pool.length === 0 && (
                                        <Typography variant="caption" color="text.secondary">
                                            该池没有可用模板。
                                        </Typography>
                                    )}
                                    {pool.pool
                                        .slice()
                                        .sort((a, b) => b.weight - a.weight)
                                        .map((item) => (
                                            <Box key={item.__ui_key} sx={{ mb: 0.75 }}>
                                                <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                                                    <Typography variant="caption" noWrap sx={{ maxWidth: 150 }}>
                                                        {item.name || '未命名'}
                                                    </Typography>
                                                    <Typography variant="caption" color="text.secondary" className="mono">
                                                        w={item.weight}
                                                    </Typography>
                                                </Box>
                                                <ProbabilityBar
                                                    value={pool.map[item.__ui_key] || 0}
                                                    color={sessionType === 'friend' ? 'primary.main' : 'success.main'}
                                                />
                                            </Box>
                                        ))}
                                </Box>
                            );
                        })}
                    </Paper>

                    {/* 抽样预演 */}
                    <Paper elevation={0} sx={{ border: 1, borderColor: 'divider', borderRadius: 2.5, p: 1.75, mb: 2 }}>
                        <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 0.5 }}>
                            🎲 抽样预演
                        </Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.25 }}>
                            真实跑 2000 次加权抽取，看看实际分布是否符合预期。
                        </Typography>
                        <Box sx={{ display: 'flex', gap: 1 }}>
                            <Button
                                size="small"
                                variant="outlined"
                                onClick={() => simulate('friend')}
                                disabled={simulating}
                            >
                                摇私聊
                            </Button>
                            <Button
                                size="small"
                                variant="outlined"
                                onClick={() => simulate('group')}
                                disabled={simulating}
                            >
                                摇群聊
                            </Button>
                        </Box>
                        {simulating && <LinearProgress sx={{ mt: 1.25 }} />}
                        {simulateResult && (
                            <Box sx={{ mt: 1.5 }}>
                                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.75 }}>
                                    {simulateResult.session_type === 'friend' ? '私聊' : '群聊'}
                                    {' · '}
                                    {simulateResult.rounds} 次
                                </Typography>
                                {simulateResult.message && (
                                    <Typography variant="caption" color="warning.main">
                                        {simulateResult.message}
                                    </Typography>
                                )}
                                {(simulateResult.results || []).map((row, rowIndex) => (
                                    <Box key={`${row.id}-${rowIndex}`} sx={{ mb: 0.6 }}>
                                        <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                                            <Typography variant="caption" noWrap sx={{ maxWidth: 150 }}>
                                                {row.name || '未命名'}
                                            </Typography>
                                            <Typography variant="caption" className="mono">
                                                {Math.round(row.actual * 1000) / 10}%
                                                <Typography
                                                    component="span"
                                                    variant="caption"
                                                    sx={{ opacity: 0.5, ml: 0.5 }}
                                                >
                                                    （期望 {Math.round(row.expected * 1000) / 10}%）
                                                </Typography>
                                            </Typography>
                                        </Box>
                                        <ProbabilityBar value={row.actual} color="secondary.main" />
                                    </Box>
                                ))}
                            </Box>
                        )}
                    </Paper>

                    {/* 完整 Prompt 预览 */}
                    <Paper elevation={0} sx={{ border: 1, borderColor: 'divider', borderRadius: 2.5, p: 1.75 }}>
                        <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 0.5 }}>
                            👁️ 最终 Prompt 预览
                        </Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.25 }}>
                            服务端用真实装配逻辑渲染，与模型实际收到的内容一致。
                            未指定模板时每次都会重新抽签，抽到不同结果是正常的。
                        </Typography>
                        <Box sx={{ display: 'flex', gap: 1, mb: 1 }}>
                            <TextField
                                select
                                size="small"
                                label="会话"
                                value={previewType}
                                onChange={(e) => setPreviewType(e.target.value)}
                                sx={{ flex: 1 }}
                            >
                                {SESSION_TYPES.map((option) => (
                                    <MenuItem key={option.value} value={option.value}>
                                        {option.icon} {option.label}
                                    </MenuItem>
                                ))}
                            </TextField>
                            <TextField
                                select
                                size="small"
                                label="上下文"
                                value={previewMode}
                                onChange={(e) => setPreviewMode(e.target.value)}
                                sx={{ flex: 1 }}
                            >
                                {SOURCE_MODES.map((option) => (
                                    <MenuItem key={option.value} value={option.value}>
                                        {option.label}
                                    </MenuItem>
                                ))}
                            </TextField>
                        </Box>
                        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mb: 1 }}>
                            <TextField
                                size="small"
                                type="number"
                                label="未回复次数"
                                value={previewUnanswered}
                                onChange={(e) => setPreviewUnanswered(parseInt(e.target.value, 10) || 0)}
                                sx={{ width: 110 }}
                            />
                            <Button
                                size="small"
                                variant="contained"
                                onClick={runPreview}
                                disabled={previewing}
                                sx={{ flex: 1 }}
                            >
                                {previewing ? '渲染中…' : '渲染'}
                            </Button>
                        </Box>

                        {preview && (
                            <Box sx={{ mt: 1.25 }}>
                                <Chip
                                    size="small"
                                    color={preview.template.matched ? 'primary' : 'default'}
                                    label={
                                        (preview.template.name || '无模板可用')
                                        + (preview.sampled ? ' · 本轮抽签' : ' · 已锁定')
                                    }
                                    sx={{ mb: 1 }}
                                />
                                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                                    共 {preview.char_count} 字
                                </Typography>
                                <Box
                                    className="mono"
                                    sx={{
                                        maxHeight: 320,
                                        overflow: 'auto',
                                        fontSize: '0.74rem',
                                        lineHeight: 1.7,
                                        whiteSpace: 'pre-wrap',
                                        wordBreak: 'break-word',
                                        bgcolor: 'action.hover',
                                        borderRadius: 1.5,
                                        p: 1.25,
                                    }}
                                >
                                    {(preview.sections || []).map((section, i) => (
                                        <Box key={`${section.source}-${i}`} sx={{ mb: 1.25 }}>
                                            <Typography
                                                variant="caption"
                                                sx={{
                                                    display: 'block',
                                                    fontWeight: 800,
                                                    mb: 0.4,
                                                    color: section.kind === 'template'
                                                        ? 'primary.main'
                                                        : 'text.disabled',
                                                }}
                                            >
                                                — {section.label} —
                                            </Typography>
                                            <Box>{section.text}</Box>
                                        </Box>
                                    ))}
                                </Box>
                            </Box>
                        )}
                    </Paper>
                </Box>
            </Box>

            {/* 导入对话框 */}
            <Dialog open={importOpen} onClose={() => setImportOpen(false)} maxWidth="md" fullWidth>
                <DialogTitle>导入提示词模板</DialogTitle>
                <DialogContent>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                        粘贴之前导出的 JSON 内容。导入会整表替换当前模板列表。
                    </Typography>
                    <TextField
                        fullWidth
                        multiline
                        minRows={10}
                        maxRows={20}
                        value={importText}
                        onChange={(e) => setImportText(e.target.value)}
                        placeholder='{"format": "astrbot-proactive-chat/prompts", "templates": [...]}'
                        InputProps={{ className: 'mono', sx: { fontSize: '0.78rem' } }}
                    />
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setImportOpen(false)}>取消</Button>
                    <Button variant="contained" onClick={doImport} disabled={!importText.trim()}>
                        导入
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
}

window.PromptsView = PromptsView;
})();
