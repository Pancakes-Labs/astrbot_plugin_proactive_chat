(() => {
/**
 * 文件职责：提示词卡片的精修左栏。
 *
 * 只有进入抽屉才允许改名称 / 适用范围 / 正文，卡面保持纯展示。
 *
 * 光标插入占位符的实现与配置页 TemplateListField 保持一致。
 */

const {
    Box, TextField, Chip, MenuItem,
} = MaterialUI;
const { useState, useCallback } = React;
const {
    HEADER_INTENT, HEADER_FINAL, checkTextIssues,
    placeholderEntries, formatPercent,
} = window.PromptUtils;

// 与 ConfigShared.TEMPLATE_SCOPES 同源，这里只取需要的三个选项。
const SCOPE_OPTIONS = [
    { value: '', label: '通用（私聊 + 群聊）' },
    { value: 'friend', label: '仅私聊' },
    { value: 'group', label: '仅群聊' },
];

function PromptEditor({ item, probability, share, onPatch }) {
    // 用 state 而不是 ref 持有 textarea 节点：节点挂载时机不确定，
    // ref 为空时点击占位符会退化成追加到末尾。
    const [textEl, setTextEl] = useState(null);
    const issues = checkTextIssues(item.text);
    const tokens = placeholderEntries();

    // 在光标处插入：拿不到节点时退化为追加到末尾，保证按钮永远可用。
    const insertAtCursor = useCallback((token) => {
        if (!textEl) {
            onPatch({ text: `${item.text}${token}` });
            return;
        }
        const start = textEl.selectionStart ?? item.text.length;
        const end = textEl.selectionEnd ?? item.text.length;
        const next = `${item.text.slice(0, start)}${token}${item.text.slice(end)}`;
        onPatch({ text: next });
        // 插入后把光标放到占位符之后，方便继续输入。
        requestAnimationFrame(() => {
            const pos = start + token.length;
            textEl.focus();
            textEl.setSelectionRange(pos, pos);
        });
    }, [item.text, onPatch, textEl]);

    // 段头补全：已存在就不重复插入，避免出现两个 [最终指令]。
    const appendHeader = useCallback((header) => {
        if (item.text.includes(header)) return;
        const needsBreak = item.text && !item.text.endsWith('\n');
        onPatch({ text: `${item.text}${needsBreak ? '\n' : ''}${header}\n` });
    }, [item.text, onPatch]);

    return (
        <Box className="pv-editor-pane">
            {/* 基础信息：名称 / 范围 / 权重 / 概率收进一张浅色小卡，
                头部只留展示信息，一屏内的输入框数量降下来。 */}
            <Box className="pv-editor-card">
                <Box className="pv-editor-card-head">基础信息</Box>
                <Box className="pv-editor-fields">
                    <TextField
                        size="small"
                        label="用途名称"
                        value={item.name}
                        onChange={(event) => onPatch({ name: event.target.value })}
                        placeholder="例如：当下碎片分享"
                        fullWidth
                    />
                    <TextField
                        select
                        size="small"
                        label="适用范围"
                        value={item.scope}
                        onChange={(event) => onPatch({ scope: event.target.value })}
                        fullWidth
                    >
                        {SCOPE_OPTIONS.map((option) => (
                            <MenuItem key={option.value || 'any'} value={option.value}>
                                {option.label}
                            </MenuItem>
                        ))}
                    </TextField>
                </Box>

                <Box className="pv-editor-stats">
                    <TextField
                        size="small"
                        type="number"
                        label="权重"
                        value={item.weight}
                        onChange={(event) => onPatch({
                            weight: Math.max(1, parseInt(event.target.value, 10) || 1),
                        })}
                        inputProps={{ min: 1, max: 9999 }}
                        sx={{ width: 104 }}
                    />
                    {/* 抽中率是这一池里被抽到的概率，占比是本盒内的权重份额。
                        两者常相等，只有口径不同时才并列显示。 */}
                    <span className="pv-editor-kpi">
                        {/* probability === null 表示这是一张还没入表的草稿。 */}
                        {probability === null || probability === undefined ? (
                            <React.Fragment>
                                <em>—</em>
                                <small>保存后计算</small>
                            </React.Fragment>
                        ) : Array.isArray(probability) ? (
                            <React.Fragment>
                                <em>{formatPercent(probability[0])}</em>
                                <small>私聊抽中率</small>
                                <em>{formatPercent(probability[1])}</em>
                                <small>群聊抽中率</small>
                            </React.Fragment>
                        ) : (
                            <React.Fragment>
                                <em>{formatPercent(probability)}</em>
                                <small>抽中率</small>
                            </React.Fragment>
                        )}
                    </span>
                    {!Array.isArray(probability)
                        && Number(share) > 0
                        && Math.abs((Number(probability) || 0) - Number(share)) >= 0.005 && (
                        <span className="pv-editor-kpi is-muted">
                            <em>{formatPercent(share)}</em>
                            <small>盒内占比</small>
                        </span>
                    )}
                </Box>
            </Box>

            {/* 正文：不吃固定行数，撑满左栏剩余高度，缩放窗口时不会出现内层双滚动。 */}
            <Box className="pv-editor-card is-grow">
                <Box className="pv-editor-card-head">
                    提示词正文
                    <span className="pv-editor-count">{item.text.length} 字</span>
                </Box>
                <TextField
                    fullWidth
                    multiline
                    size="small"
                    value={item.text}
                    inputRef={(el) => { if (el) setTextEl(el); }}
                    onChange={(event) => onPatch({ text: event.target.value })}
                    placeholder={`${HEADER_INTENT}\n（这一轮想做什么）\n\n${HEADER_FINAL}\n（给模型的最终要求）`}
                    className="pv-editor-textarea"
                    InputProps={{ className: 'mono' }}
                />
            </Box>

            {/* 占位符与段头合并成一条工具带：省掉两行各自的小标题。 */}
            <Box className="pv-editor-toolbar">
                <span className="pv-editor-toolbar-label">插入</span>
                {tokens.map(([token, label]) => (
                    <Chip
                        key={token}
                        className="pv-token"
                        size="small"
                        variant="outlined"
                        label={label}
                        title={token}
                        onClick={() => insertAtCursor(token)}
                    />
                ))}
                <span className="pv-editor-toolbar-sep" aria-hidden="true" />
                <Chip
                    className="pv-token is-header"
                    size="small"
                    variant="outlined"
                    color="primary"
                    label={`+ ${HEADER_INTENT}`}
                    onClick={() => appendHeader(HEADER_INTENT)}
                />
                <Chip
                    className="pv-token is-header"
                    size="small"
                    variant="outlined"
                    color="primary"
                    label={`+ ${HEADER_FINAL}`}
                    onClick={() => appendHeader(HEADER_FINAL)}
                />
            </Box>

            {issues.length > 0 && (
                <Box className="pv-editor-issues">
                    {issues.map((issue) => (
                        <span key={issue} className="pv-issue">⚠ {issue}</span>
                    ))}
                </Box>
            )}
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.PromptEditor = PromptEditor;
})();
