(() => {
/**
 * 文件职责：服务提供商与模型选择控件。
 *
 * 对外数据契约与后端保持一致，仍然只输出字符串：
 * - select_provider      -> "provider_id" 或 "provider_id::model"
 * - select_providers     -> ["provider_id::model", ...]
 * 可视化编辑（providers 下拉、模型联动、回退链上下移动）只发生在 UI 层，
 * 这样 AstrBot 原生面板、我们自己改的配置文件、运行时解析读到的都是同一份结构。
 *
 * 之所以不把回退链做成对象数组：顺序即优先级，纯字符串列表在文本编辑器、
 * 手改配置文件、任何前端里语义都唯一确定，而对象数组/多选控件无法表达稳定顺序。
 */

const {
    Box, TextField, Typography, Paper, Chip, MenuItem, Button, IconButton, Tooltip,
} = MaterialUI;
const { useState, useEffect } = React;

// 值与模型的组合分隔符，与后端 model_scene.REF_SEPARATOR 必须保持一致。
const REF_SEPARATOR = '::';
// 模拟对话模型列表可能一次都请求不到；部分兼容端点不提供模型接口，只能手填。
const MODEL_MANUAL_HINT = '可直接输入模型名，留空表示使用该服务提供商的默认模型';

// Provider 清单在页面生命周期内只请求一次：控件数量多，逐字段请求会重复开销。
const providerStore = {
    items: null,
    specialOptions: [],
    promise: null,
};

function loadProvidersOnce() {
    if (providerStore.items) return Promise.resolve(providerStore);
    if (providerStore.promise) return providerStore.promise;

    const api = window.ApiClient;
    if (!api || typeof api.getProviders !== 'function') {
        providerStore.items = [];
        return Promise.resolve(providerStore);
    }

    providerStore.promise = api.getProviders()
        .then((data) => {
            providerStore.items = (data && data.providers) || [];
            providerStore.specialOptions = (data && data.special_options) || [];
            return providerStore;
        })
        .catch((err) => {
            console.error('加载服务提供商列表失败', err);
            if (window.Toast) {
                window.Toast.warning((err && err.message) || '服务提供商列表加载失败，仅能手动填写', {
                    title: '服务提供商',
                    key: 'config-provider-load',
                });
            }
            providerStore.items = [];
            return providerStore;
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

// 反向拼装。model 为空时只输出 provider_id，避免产生 "pid::" 这类冗余写法。
function joinRef(providerId, model) {
    const pid = (providerId || '').trim();
    const mid = (model || '').trim();
    if (!mid) return pid;
    if (!pid) return `${REF_SEPARATOR}${mid}`;
    return `${pid}${REF_SEPARATOR}${mid}`;
}

// 统一的输入框外观，与其它配置项保持一致的圆角与边框。
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

/**
 * 服务提供商 + 模型联动选择。
 *
 * 值形如 "provider_id::model"。选择 provider 后模型下拉会切到该 provider 的
 * 模型列表，同时保留自由输入：很多 OpenAI 兼容端点不提供模型枚举接口，
 * 强制下拉会让这些用户完全无法配置。
 */
function ProviderSelectField({ schema, fieldKey, value, onChange, depth }) {
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

    const { providerId, model } = splitRef(value);
    const current = providers.find((item) => item.id === providerId);
    const modelOptions = (current && current.models) || [];
    // 已保存的模型中可能有已被删除的项，补一个占位避免下拉显示空白。
    const modelMissing = Boolean(model) && modelOptions.length > 0 && !modelOptions.includes(model);
    const providerMissing = Boolean(providerId) && providers.length > 0 && !current;

    const emit = (nextProviderId, nextModel) => onChange(joinRef(nextProviderId, nextModel));

    return (
        <Box sx={{ py: 1.75, borderBottom: depth === 0 ? 'none' : '1px solid', borderColor: 'divider' }}>
            <FieldHeading schema={schema} fieldKey={fieldKey} />
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.5 }}>
                <TextField
                    select
                    fullWidth
                    size="small"
                    label="服务提供商"
                    value={providerId}
                    onChange={(e) => emit(e.target.value, '')}
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
                            {item.name === item.id ? item.id : `${item.name}（${item.id}）`}
                        </MenuItem>
                    ))}
                </TextField>

                {modelOptions.length > 0 ? (
                    <TextField
                        select
                        fullWidth
                        size="small"
                        label="模型"
                        value={model}
                        onChange={(e) => emit(providerId, e.target.value)}
                        helperText={MODEL_MANUAL_HINT}
                        variant="outlined"
                        SelectProps={{
                            // 允许自由输入：兼容端点常常给不出完整模型列表。
                            SelectDisplayProps: { 'data-free-input': 'true' }
                        }}
                        sx={FIELD_SX}
                    >
                        <MenuItem value="">
                            <em>使用默认模型</em>
                        </MenuItem>
                        {modelMissing && (
                            <MenuItem key="missing_model" value={model}>
                                {`${model}（不在已知列表）`}
                            </MenuItem>
                        )}
                        {modelOptions.map((name) => (
                            <MenuItem key={name} value={name}>{name}</MenuItem>
                        ))}
                    </TextField>
                ) : (
                    <TextField
                        fullWidth
                        size="small"
                        label="模型"
                        value={model}
                        disabled={!providerId}
                        onChange={(e) => emit(providerId, e.target.value)}
                        placeholder={(current && current.default_model) || '使用默认模型'}
                        helperText={MODEL_MANUAL_HINT}
                        variant="outlined"
                        sx={FIELD_SX}
                    />
                )}
            </Box>
            {current && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.75 }}>
                    该提供商默认模型：{current.default_model || '未知'}
                    {modelOptions.length === 0 && ' ｜ 未获取到模型列表，可手动填写'}
                </Typography>
            )}
        </Box>
    );
}

/**
 * 回退链可视化编辑器。
 *
 * 对外输出 string[]：每行一个候选。UI 提供 provider 下拉、模型联动与
 * 上移 / 下移按钮，让"顺序即优先级"这件事可被直观操作，
 * 而不是让用户在一行行文本里手动调换。
 */
function ProviderChainField({ schema, fieldKey, value, onChange, depth }) {
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

    // 容错：非数组、或包含非字符串项时统一归一为空数组后按行渲染。
    const chain = Array.isArray(value) ? value.map((item) => String(item || '')) : [];

    const updateAt = (index, nextRef) => {
        const next = chain.slice();
        // 空行代表"还没选完"，原样保留占位，避免用户刚点开下拉就被删除。
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
                    const current = providers.find((item) => item.id === providerId);
                    const modelOptions = (current && current.models) || [];
                    const providerMissing = Boolean(providerId) && providers.length > 0 && !current;
                    const modelMissing = Boolean(model) && modelOptions.length > 0 && !modelOptions.includes(model);

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
                                            {item.name === item.id ? item.id : `${item.name}（${item.id}）`}
                                        </MenuItem>
                                    ))}
                                </TextField>

                                {modelOptions.length > 0 ? (
                                    <TextField
                                        select
                                        fullWidth
                                        size="small"
                                        label="模型"
                                        value={model}
                                        onChange={(e) => updateAt(index, joinRef(providerId, e.target.value))}
                                        variant="outlined"
                                        sx={FIELD_SX}
                                    >
                                        <MenuItem value="">
                                            <em>使用默认模型</em>
                                        </MenuItem>
                                        {modelMissing && (
                                            <MenuItem key="missing_model" value={model}>
                                                {`${model}（不在已知列表）`}
                                            </MenuItem>
                                        )}
                                        {modelOptions.map((name) => (
                                            <MenuItem key={name} value={name}>{name}</MenuItem>
                                        ))}
                                    </TextField>
                                ) : (
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
                                )}
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

            {providers.length === 0 && (
                <Typography variant="caption" color="warning.main" sx={{ display: 'block', mt: 1 }}>
                    未读取到任何服务提供商，请先在 AstrBot 中配置对话模型。
                </Typography>
            )}
        </Box>
    );
}

window.ProviderSelectField = ProviderSelectField;
window.ProviderChainField = ProviderChainField;
window.ProviderSelectUtil = Object.freeze({
    splitRef: splitRef,
    joinRef: joinRef,
    loadProvidersOnce: loadProvidersOnce,
    REF_SEPARATOR: REF_SEPARATOR,
});
})();
