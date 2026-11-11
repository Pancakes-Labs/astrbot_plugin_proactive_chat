(() => {
/**
 * 文件职责：提示词模板列表编辑器。
 *
 * 每条模板就是一句主动消息正文：用途名称 / 适用范围 / 正文 / 启用 / 权重。
 * 输出格式不在这里配置，它由 prompt_settings.output_format 单独控制，
 * 因此本编辑器不再提供「类型」下拉，也不需要校验流水占位符。
 */

const {
    Box, TextField, Switch, Typography, Paper, Chip, MenuItem, Button, IconButton, Divider,
} = MaterialUI;
const { useState } = React;
const {
    PLACEHOLDER_LABELS, TEMPLATE_SCOPES, TEMPLATE_EMPTY_HINT, findUnknownPlaceholders,
} = window.ConfigShared;

// 正文默认值：给出可直接使用的示例，避免用户面对空字段不知所措。
const DEFAULT_TEMPLATE_TEXT = '请以你的人设，自然地发起一次主动消息。';

// AstrBot 管理面板按 __template_key 匹配模板定义，缺失会显示「未指定模板」
// 并拒绝保存。这个字段对本插件没有业务含义，但必须原样透传，不能丢。
const TEMPLATE_KEY_FIELD = '__template_key';
const DEFAULT_TEMPLATE_KEY = 'prompt_template';

// 每条模板必须有唯一 id：会话差异页要靠 id 区分「本页可见」与「被隐藏」的条目，
// 空 id 会让多条新增模板互相串位。
function createTemplateId() {
    return `tpl_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function createEmptyTemplate() {
    return {
        id: createTemplateId(),
        name: '',
        text: DEFAULT_TEMPLATE_TEXT,
        enabled: true,
        weight: 100,
        scope: '',
        [TEMPLATE_KEY_FIELD]: DEFAULT_TEMPLATE_KEY,
    };
}

// 归一化：把非对象项与缺失字段补齐，保证受控输入始终拿到完整结构。
// 旧配置可能残留 role 字段，这里不读出也不写回，保存后自然被清除。
function normalizeItem(item) {
    if (!item || typeof item !== 'object') return createEmptyTemplate();
    return {
        // 保留模板选择键：编辑后写回配置时若丢掉它，AstrBot 面板会认不出该条目。
        [TEMPLATE_KEY_FIELD]: item[TEMPLATE_KEY_FIELD] || DEFAULT_TEMPLATE_KEY,
        id: item.id || '',
        name: item.name || '',
        text: typeof item.text === 'string' ? item.text : '',
        enabled: item.enabled !== false,
        weight: Number.isFinite(Number(item.weight)) ? Number(item.weight) : 100,
        scope: item.scope || '',
    };
}

function TemplateCard({ index, item, onPatch, onRemove, sessionType = '' }) {
    const [textRef, setTextRef] = useState(null);
    const data = normalizeItem(item);
    const unknown = findUnknownPlaceholders(data.text);
    const isEmpty = !data.text.trim();

    // 在光标处插入占位符：用户不必记忆英文键名，点击即可得到正确写法。
    const insertPlaceholder = (token) => {
        const el = textRef;
        if (!el) {
            onPatch({ text: `${data.text}${token}` });
            return;
        }
        const start = el.selectionStart ?? data.text.length;
        const end = el.selectionEnd ?? data.text.length;
        const next = `${data.text.slice(0, start)}${token}${data.text.slice(end)}`;
        onPatch({ text: next });
        // 插入后把光标放到占位符之后，方便继续输入。
        requestAnimationFrame(() => {
            const pos = start + token.length;
            el.focus();
            el.setSelectionRange(pos, pos);
        });
    };

    return (
        <Paper
            elevation={0}
            sx={{
                p: 2,
                mb: 1.5,
                border: 1,
                borderColor: 'divider',
                borderRadius: 2,
                bgcolor: 'background.paper',
            }}
        >
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.5 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
                    模板 {index + 1}
                </Typography>
                <Chip
                    size="small"
                    label={data.enabled ? '已启用' : '已停用'}
                    color={data.enabled ? 'primary' : 'default'}
                    variant="outlined"
                />
                <IconButton size="small" onClick={onRemove} title="删除该模板">
                    <Typography component="span" sx={{ fontSize: '1rem' }}>✕</Typography>
                </IconButton>
            </Box>

            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.5, mb: 1.5 }}>
                <TextField
                    fullWidth
                    size="small"
                    label="用途名称"
                    value={data.name}
                    onChange={(e) => onPatch({ name: e.target.value })}
                    placeholder="例如：日常寒暄 / 话题延续"
                />
                <TextField
                    select
                    fullWidth
                    size="small"
                    label="适用范围"
                    value={data.scope}
                    onChange={(e) => onPatch({ scope: e.target.value })}
                    // 会话差异页新增的模板必定只服务当前会话，锁定范围可避免
                    // 用户选成一个对当前会话永远不生效的值，白写一段提示词。
                    disabled={Boolean(sessionType)}
                    helperText={
                        sessionType
                            ? `会话差异页固定为「${sessionType === 'group' ? '仅群聊' : '仅私聊'}」`
                            : '「通用」会同时用于私聊与群聊'
                    }
                >
                    {TEMPLATE_SCOPES.map((option) => (
                        <MenuItem key={option.value || 'any'} value={option.value}>
                            {option.label}
                        </MenuItem>
                    ))}
                </TextField>
            </Box>

            <TextField
                fullWidth
                size="small"
                type="number"
                label="权重"
                value={data.weight}
                onChange={(e) => onPatch({ weight: parseInt(e.target.value, 10) || 100 })}
                helperText="多条模板同时启用时按权重随机选取"
                sx={{ mb: 1.5 }}
            />

            <TextField
                fullWidth
                multiline
                minRows={5}
                maxRows={16}
                size="small"
                label="提示词正文"
                value={data.text}
                inputRef={(el) => setTextRef(el)}
                onChange={(e) => onPatch({ text: e.target.value })}
                placeholder="在这里书写主动消息的提示词…"
                sx={{ mb: 1 }}
            />

            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75, mb: 1 }}>
                <Typography variant="caption" color="text.secondary" sx={{ mr: 0.5 }}>
                    点击插入占位符：
                </Typography>
                {Object.entries(PLACEHOLDER_LABELS).map(([token, label]) => (
                    <Chip
                        key={token}
                        size="small"
                        variant="outlined"
                        label={label}
                        onClick={() => insertPlaceholder(token)}
                        title={token}
                        sx={{ cursor: 'pointer' }}
                    />
                ))}
            </Box>

            {unknown.length > 0 && (
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mb: 1 }}>
                    <Typography variant="caption" sx={{ color: 'warning.main' }}>
                        检测到无法识别的占位符（会原样发给模型）：
                    </Typography>
                    {unknown.map((token) => (
                        <Chip key={token} size="small" color="warning" label={token} />
                    ))}
                </Box>
            )}

            {isEmpty && (
                <Typography variant="caption" sx={{ color: 'warning.main', display: 'block', mb: 1 }}>
                    {TEMPLATE_EMPTY_HINT}
                </Typography>
            )}

            <Divider sx={{ my: 1.5 }} />

            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <Switch
                    size="small"
                    checked={data.enabled}
                    onChange={(e) => onPatch({ enabled: e.target.checked })}
                />
                <Typography variant="body2" color="text.secondary">
                    启用该模板
                </Typography>
            </Box>
        </Paper>
    );
}

function TemplateListField({ schema, value, onChange, depth = 1, sessionType = '' }) {
    const allItems = Array.isArray(value) ? value : [];
    // 会话差异页只呈现对本会话类型有效的模板。
    // 不这样过滤的话，编辑群聊会话时会看到「仅私聊」的模板并误改它，
    // 保存后 compute_diff 会把这个改动记成该群聊的覆写，凭空多出一条私聊模板副本。
    const items = sessionType
        ? allItems.filter((item) => {
            const scope = normalizeItem(item).scope;
            return !scope || scope === sessionType;
        })
        : allItems;

    // 被筛掉的模板只是不在本页展示，必须保持原样回写，不能当作已删除。
    const hiddenCount = allItems.length - items.length;
    const applyItems = (nextVisible) => {
        if (!sessionType || hiddenCount <= 0) {
            onChange(nextVisible);
            return;
        }
        const visibleIds = new Set(items.map((item) => normalizeItem(item).id));
        // 合并时保持原有顺序：按 allItems 顺序穿插，避免保存后模板顺序被打乱。
        const payload = [];
        const pool = [...nextVisible];
        allItems.forEach((item) => {
            const id = normalizeItem(item).id;
            if (!visibleIds.has(id)) {
                // 非本会话范围的模板：原样保留。
                payload.push(item);
                return;
            }
            const idx = pool.findIndex((cand) => normalizeItem(cand).id === id);
            if (idx >= 0) {
                // 仍存在：用编辑后的新版覆盖。
                payload.push(pool.splice(idx, 1)[0]);
            }
            // 找不到说明用户删掉了它，直接跳过，不能回退成原条目。
        });
        // 剩余的是本页新增项，追加到末尾。
        payload.push(...pool);
        onChange(payload);
    };

    const patchAt = (index, patch) => {
        const next = items.map((item, i) => (i === index ? { ...normalizeItem(item), ...patch } : item));
        applyItems(next);
    };

    const removeAt = (index) => {
        applyItems(items.filter((_, i) => i !== index));
    };

    const addItem = () => {
        // 会话差异页新增模板时直接锁定为本会话类型，省掉用户再选一次，
        // 也避免新增出一条对当前会话根本不会生效的模板。
        const created = createEmptyTemplate();
        if (sessionType) created.scope = sessionType;
        applyItems([...items, created]);
    };

    return (
        <Box sx={{ py: 1.5 }}>
            <Box sx={{ mb: 1.5 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, fontSize: '0.9rem' }}>
                    {schema.description || '提示词模板列表'}
                </Typography>
                {schema.hint && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                        {schema.hint}
                    </Typography>
                )}
                {sessionType && hiddenCount > 0 && (
                    <Box
                        sx={{
                            mt: 1,
                            px: 1.25,
                            py: 0.75,
                            borderRadius: 1.5,
                            bgcolor: 'action.hover',
                        }}
                    >
                        <Typography variant="caption" color="text.secondary">
                            只显示对本会话有效的模板，另有 {hiddenCount} 条其他范围的模板已隐藏，保存时会原样保留。
                        </Typography>
                    </Box>
                )}
            </Box>

            {items.length === 0 && (
                <Paper
                    elevation={0}
                    sx={{ p: 2, mb: 1.5, border: 1, borderStyle: 'dashed', borderColor: 'divider', borderRadius: 2 }}
                >
                    <Typography variant="body2" color="text.secondary">
                        当前没有模板，本次主动消息将只使用插件内置的场景说明与输出约束。
                    </Typography>
                </Paper>
            )}

            {items.map((item, index) => (
                <TemplateCard
                    key={index}
                    sessionType={sessionType}
                    index={index}
                    item={item}
                    onPatch={(patch) => patchAt(index, patch)}
                    onRemove={() => removeAt(index)}
                />
            ))}

            <Button variant="outlined" size="small" onClick={addItem}>
                + 新增模板
            </Button>
        </Box>
    );
}

window.TemplateListField = TemplateListField;
})();
