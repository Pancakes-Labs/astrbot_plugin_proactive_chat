(() => {
/**
 * 文件职责：服务提供商与模型选择控件。
 *
 * 数据契约与 _conf_schema.json 严格一致，绝不自行组合编码：
 * - select_provider  -> 纯 provider_id 字符串
 * - select_model     -> 纯 model 字符串，靠同层 provider_id 联动
 * - select_providers -> ["provider_id::model", ...]，只有回退链这一处用组合编码
 *
 * 回退链之所以用组合编码：它本身是"有序候选列表"，配置形态只有一个 string[]，
 * 而场景配置有独立的 provider_id / model 两个字段，二者不能混用同一套写法。
 */

const {
    Box, TextField, Typography, Paper, Chip, MenuItem, Button, IconButton,
    Tooltip, Autocomplete, Switch, FormControlLabel,
} = MaterialUI;
const { useState, useEffect } = React;

// 回退链元素的分隔符，与后端 model_scene.REF_SEPARATOR 必须一致。
const REF_SEPARATOR = '::';

// Provider 清单在页面生命周期内只请求一次：控件数量多，逐字段请求会重复开销。
const providerStore = {
    items: null,
    specialOptions: [],
    promise: null,
};

/**
 * 拉取 Provider 清单。
 *
 * 失败时**不写入 items**：空数组是真值，一旦缓存成 []，后续所有控件都会
 * 认为是"没有可用提供商"而不再重试，用户只能刷新整个页面才能恢复。
 */
function loadProvidersOnce() {
    if (providerStore.items) return Promise.resolve(providerStore);
    if (providerStore.promise) return providerStore.promise;

    const api = window.ApiClient;
    if (!api || typeof api.getProviders !== 'function') {
        return Promise.resolve(providerStore);
    }

    providerStore.promise = api.getProviders()
        .then((data) => {
            const items = (data && data.providers) || [];
            // 仅在拿到非空结果时缓存，空结果视为一次无效响应。
            if (items.length > 0) {
                providerStore.items = items;
                providerStore.specialOptions = (data && data.special_options) || [];
            }
            return { ...providerStore, items };
        })
        .catch((err) => {
            console.error('加载服务提供商列表失败', err);
            if (window.Toast) {
                window.Toast.warning((err && err.message) || '服务提供商列表加载失败，仅能手动填写', {
                    title: '服务提供商',
                    key: 'config-provider-load',
                });
            }
            return { ...providerStore, items: [] };
        })
        .finally(() => {
            providerStore.promise = null;
        });

    return providerStore.promise;
}

// 把 "provider_id::model" 拆成两部分；只有 provider_id 时 model 为空。
function splitRef(raw) {
    const text = raw === undefined || raw === null ? '' : String(raw).trim();
    if (!text) return { providerId: '', model: '' };
    const at = text.indexOf(REF_SEPARATOR);
    if (at < 0) return { providerId: text, model: '' };
    return {
        providerId: text.slice(0, at).trim(),
        model: text.slice(at + REF_SEPARATOR.length).trim(),
    };
}

// 反向拼装。model 为空时只输出 provider_id，避免产生 "pid::" 冗余写法。
function joinRef(providerId, model) {
    const pid = (providerId || '').trim();
    const mid = (model || '').trim();
    if (!mid) return pid;
    if (!pid) return `${REF_SEPARATOR}${mid}`;
    return `${pid}${REF_SEPARATOR}${mid}`;
}

const FIELD_SX = {
    '& .MuiOutlinedInput-root': {
        borderRadius: 1.5,
        bgcolor: 'background.paper',
        '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
    }
};

// 单行描述区，复用配置页统一的标题 + 提示排版。
function FieldHeading({ schema, fieldKey, hint }) {
    return (
        <Box sx={{ mb: 1 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.9rem' }}>
                {schema.description || fieldKey}
            </Typography>
            {(hint || schema.hint) && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, lineHeight: 1.4 }}>
                    {hint || schema.hint}
                </Typography>
            )}
        </Box>
    );
}

function useProviders() {
    const [providers, setProviders] = useState(providerStore.items || []);
    const [specialOptions, setSpecialOptions] = useState(providerStore.specialOptions || []);

    useEffect(() => {
        let disposed = false;
        loadProvidersOnce().then((store) => {
            if (disposed) return;
            setProviders(store.items || []);
            setSpecialOptions(store.specialOptions || []);
        });
        return () => { disposed = true; };
    }, []);

    return { providers, specialOptions };
}

function findProvider(providers, providerId) {
    return providers.find((item) => item.id === providerId) || null;
}

function providerLabel(item) {
    return item.name === item.id ? item.id : `${item.name}（${item.id}）`;
}

/**
 * 服务提供商选择。对外只输出纯 provider_id。
 */
function ProviderSelectField({ schema, fieldKey, value, onChange, depth }) {
    const { providers, specialOptions } = useProviders();
    const providerId = value === undefined || value === null ? '' : String(value).trim();
    const current = findProvider(providers, providerId);
    const providerMissing = Boolean(providerId) && providers.length > 0 && !current;

    return (
        <Box sx={{ py: 1.75, borderBottom: depth === 0 ? 'none' : '1px solid', borderColor: 'divider' }}>
            <FieldHeading schema={schema} fieldKey={fieldKey} />
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                <TextField
                    select
                    fullWidth
                    size="small"
                    label="服务提供商"
                    value={providerId}
                    onChange={(e) => onChange(e.target.value)}
                    variant="outlined"
                    sx={FIELD_SX}
                >
                    {specialOptions.map((option) => (
                        <MenuItem key={`special_${option.value}`} value={option.value}>
                            {option.label}
                        </MenuItem>
                    ))}
                    {providerMissing && (
                        <MenuItem key="missing_provider" value={providerId}>
                            {`${providerId}（提供商不存在）`}
                        </MenuItem>
                    )}
                    {providers.map((item) => (
                        <MenuItem key={item.id} value={item.id}>
                            {providerLabel(item)}
                        </MenuItem>
                    ))}
                </TextField>
                {current && (
                    <Typography variant="caption" color="text.secondary">
                        该提供商默认模型：{current.default_model || '未知'}
                    </Typography>
                )}
                {providers.length === 0 && (
                    <Typography variant="caption" color="warning.main">
                        未读取到任何服务提供商，请先在 AstrBot 中配置对话模型。
                    </Typography>
                )}
            </Box>
        </Box>
    );
}

/**
 * 模型输入控件（受同层 provider_id 驱动）。
 *
 * 用 Autocomplete freeSolo：既能从已知列表里选，也能直接手打任意模型名。
 * 很多 OpenAI 兼容端点不提供模型枚举接口，强制下拉会让这些用户无法配置。
 */
function ModelAutocomplete({ value, options, placeholder, helperText, onChange }) {
    return (
        <Autocomplete
            freeSolo
            autoSelect
            options={options}
            value={value || ''}
            onChange={(event, next) => onChange(typeof next === 'string' ? next : (next || ''))}
            onInputChange={(event, next, reason) => {
                // 只有用户实际输入才回写，避免选项切换时被重置成空。
                if (reason === 'input') onChange(next);
            }}
            renderInput={(params) => (
                <TextField
                    {...params}
                    size="small"
                    label="模型"
                    placeholder={placeholder}
                    helperText={helperText}
                    variant="outlined"
                    sx={FIELD_SX}
                />
            )}
        />
    );
}

/**
 * 场景级模型选择：读取同层 provider_id 作为联动依据。
 */
function ModelSelectField({ schema, fieldKey, value, onChange, depth, siblings }) {
    const { providers } = useProviders();
    const parent = siblings && typeof siblings === 'object' ? siblings : {};
    const providerId = parent.provider_id ? String(parent.provider_id).trim() : '';
    const current = findProvider(providers, providerId);
    const modelOptions = (current && current.models) || [];
    const model = value === undefined || value === null ? '' : String(value).trim();

    return (
        <Box sx={{ py: 1.75, borderBottom: depth === 0 ? 'none' : '1px solid', borderColor: 'divider' }}>
            <FieldHeading schema={schema} fieldKey={fieldKey} />
            <ModelAutocomplete
                value={model}
                options={modelOptions}
                placeholder={(current && current.default_model) || '留空则使用提供商默认模型'}
                helperText={
                    !providerId
                        ? '尚未选择服务提供商，此处填写的模型将应用于该会话当前使用的提供商'
                        : (modelOptions.length === 0
                            ? '未获取到模型列表，可直接手动填写模型名'
                            : '可从列表选择，也可直接输入列表外的模型名')
                }
                onChange={onChange}
            />
        </Box>
    );
}

/**
 * 回退链可视化编辑器。对外输出 string[]。
 */
function ProviderChainField({ schema, fieldKey, value, onChange, depth }) {
    const { providers, specialOptions } = useProviders();
    // 每行的"手填模型"开关状态：列表外的模型必须能录入。
    const [manualRows, setManualRows] = useState({});

    const chain = Array.isArray(value) ? value.map((item) => String(item || '')) : [];

    const updateAt = (index, nextRef) => {
        const next = chain.slice();
        next[index] = nextRef;
        onChange(next);
    };

    const removeAt = (index) => {
        const next = chain.slice();
        next.splice(index, 1);
        onChange(next);
    };

    const moveAt = (index, delta) => {
        const target = index + delta;
        if (target < 0 || target >= chain.length) return;
        const next = chain.slice();
        const [item] = next.splice(index, 1);
        next.splice(target, 0, item);
        onChange(next);
    };

    const addRow = () => onChange([...chain, '']);

    return (
        <Box sx={{ py: 1.75, borderBottom: depth === 0 ? 'none' : '1px solid', borderColor: 'divider' }}>
            <FieldHeading
                schema={schema}
                fieldKey={fieldKey}
                hint="按从上到下的顺序尝试。主模型不可用时，第一个成功的候选即被采用。"
            />

            {chain.length === 0 && (
                <Paper
                    elevation={0}
                    sx={{
                        p: 2,
                        mb: 1.5,
                        borderRadius: 2,
                        border: '1px dashed',
                        borderColor: 'divider',
                        textAlign: 'center',
                        color: 'text.secondary'
                    }}
                >
                    <Typography variant="caption">
                        尚未配置回退模型。主模型调用失败时将直接放弃本次主动消息。
                    </Typography>
                </Paper>
            )}

            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                {chain.map((raw, index) => {
                    const { providerId, model } = splitRef(raw);
                    const current = findProvider(providers, providerId);
                    const modelOptions = (current && current.models) || [];
                    const providerMissing = Boolean(providerId) && providers.length > 0 && !current;
                    const modelInList = Boolean(model) && modelOptions.includes(model);
                    // 有列表但当前值不在列表里，或模型列表压根拿不到，都要走手填分支。
                    const manual = manualRows[index] !== undefined
                        ? manualRows[index]
                        : (modelOptions.length === 0 || (Boolean(model) && !modelInList));

                    const setManual = (next) => setManualRows((prev) => ({ ...prev, [index]: next }));

                    return (
                        <Paper
                            key={`chain_${index}`}
                            elevation={0}
                            sx={{
                                p: 1.25,
                                borderRadius: 2,
                                border: '1px solid',
                                borderColor: 'divider',
                                bgcolor: 'background.paper'
                            }}
                        >
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                                <Chip
                                    size="small"
                                    label={`#${index + 1}`}
                                    color={index === 0 ? 'primary' : 'default'}
                                    variant={index === 0 ? 'filled' : 'outlined'}
                                    sx={{ height: 22, fontSize: '0.7rem' }}
                                />
                                <Typography variant="caption" color="text.secondary">
                                    {index === 0 ? '首选回退' : '次级回退'}
                                </Typography>
                                <Box sx={{ flex: 1 }} />
                                <Tooltip title="上移">
                                    <span>
                                        <IconButton
                                            size="small"
                                            disabled={index === 0}
                                            onClick={() => moveAt(index, -1)}
                                        >
                                            <Box sx={{ fontSize: 14, lineHeight: 1 }}>▲</Box>
                                        </IconButton>
                                    </span>
                                </Tooltip>
                                <Tooltip title="下移">
                                    <span>
                                        <IconButton
                                            size="small"
                                            disabled={index === chain.length - 1}
                                            onClick={() => moveAt(index, 1)}
                                        >
                                            <Box sx={{ fontSize: 14, lineHeight: 1 }}>▼</Box>
                                        </IconButton>
                                    </span>
                                </Tooltip>
                                <Tooltip title="删除">
                                    <IconButton
                                        size="small"
                                        color="error"
                                        onClick={() => removeAt(index)}
                                    >
                                        <Box sx={{ fontSize: 14, lineHeight: 1 }}>✕</Box>
                                    </IconButton>
                                </Tooltip>
                            </Box>

                            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.25 }}>
                                <TextField
                                    select
                                    fullWidth
                                    size="small"
                                    label="服务提供商"
                                    value={providerId}
                                    onChange={(e) => updateAt(index, joinRef(e.target.value, ''))}
                                    variant="outlined"
                                    sx={FIELD_SX}
                                >
                                    {specialOptions.map((option) => (
                                        <MenuItem key={`special_${option.value}`} value={option.value}>
                                            {option.label}
                                        </MenuItem>
                                    ))}
                                    {providerMissing && (
                                        <MenuItem key="missing_provider" value={providerId}>
                                            {`${providerId}（提供商不存在）`}
                                        </MenuItem>
                                    )}
                                    {providers.map((item) => (
                                        <MenuItem key={item.id} value={item.id}>
                                            {providerLabel(item)}
                                        </MenuItem>
                                    ))}
                                </TextField>

                                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
                                    {manual ? (
                                        <TextField
                                            fullWidth
                                            size="small"
                                            label="模型"
                                            value={model}
                                            disabled={!providerId}
                                            onChange={(e) => updateAt(index, joinRef(providerId, e.target.value))}
                                            placeholder={(current && current.default_model) || '使用默认模型'}
                                            variant="outlined"
                                            sx={FIELD_SX}
                                        />
                                    ) : (
                                        <TextField
                                            select
                                            fullWidth
                                            size="small"
                                            label="模型"
                                            value={model}
                                            disabled={!providerId}
                                            onChange={(e) => updateAt(index, joinRef(providerId, e.target.value))}
                                            variant="outlined"
                                            sx={FIELD_SX}
                                        >
                                            <MenuItem value="">
                                                <em>使用默认模型</em>
                                            </MenuItem>
                                            {modelOptions.map((name) => (
                                                <MenuItem key={name} value={name}>{name}</MenuItem>
                                            ))}
                                        </TextField>
                                    )}
                                    {modelOptions.length > 0 && (
                                        <FormControlLabel
                                            sx={{ m: 0, '& .MuiFormControlLabel-label': { fontSize: '0.7rem' } }}
                                            control={
                                                <Switch
                                                    size="small"
                                                    checked={manual}
                                                    onChange={(e) => setManual(e.target.checked)}
                                                />
                                            }
                                            label="手动填写模型名"
                                        />
                                    )}
                                </Box>
                            </Box>
                        </Paper>
                    );
                })}
            </Box>

            <Button
                size="small"
                variant="outlined"
                onClick={addRow}
                startIcon={<span>＋</span>}
                sx={{ mt: 1.25, borderRadius: 2 }}
            >
                添加回退模型
            </Button>
        </Box>
    );
}

window.ProviderSelectField = ProviderSelectField;
window.ModelSelectField = ModelSelectField;
window.ProviderChainField = ProviderChainField;
window.ProviderSelectUtil = Object.freeze({
    splitRef: splitRef,
    joinRef: joinRef,
    loadProvidersOnce: loadProvidersOnce,
    REF_SEPARATOR: REF_SEPARATOR,
});
})();
