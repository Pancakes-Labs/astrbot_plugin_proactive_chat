(() => {
/**
 * 文件职责：单个配置字段渲染组件，按 Schema 类型动态生成输入控件。
 */

 const { Box, TextField, Switch, Typography, Accordion, AccordionSummary, AccordionDetails, Paper, Chip, Slider, MenuItem, Button, Dialog, DialogTitle, DialogContent, DialogActions } = MaterialUI;
 const { useState, useEffect } = React;
 const { stripLeadingEmoji, CONFIG_ICONS } = window.ConfigShared;

 // 人格列表在页面生命周期内只请求一次：字段数量多，逐字段请求会造成重复开销。
 const personaStore = {
     items: null,
     specialOptions: [],
     promise: null,
 };

 function loadPersonasOnce() {
     if (personaStore.items) return Promise.resolve(personaStore);
     if (personaStore.promise) return personaStore.promise;

     const api = window.ApiClient;
     if (!api || typeof api.getPersonas !== 'function') {
         personaStore.items = [];
         return Promise.resolve(personaStore);
     }

     personaStore.promise = api.getPersonas()
         .then((data) => {
             personaStore.items = (data && data.personas) || [];
             personaStore.specialOptions = (data && data.special_options) || [];
             return personaStore;
         })
         .catch((err) => {
             console.error('加载人格列表失败', err);
             // 人格下拉会因此退化为仅剩特殊选项，提示一次即可（store 保证只请求一次）。
             if (window.Toast) {
                 window.Toast.warning((err && err.message) || '人格列表加载失败，已仅保留特殊选项', {
                     title: '人格列表',
                     key: 'config-persona-load',
                 });
             }
             personaStore.items = [];
             return personaStore;
         })
         .finally(() => {
             personaStore.promise = null;
         });

     return personaStore.promise;
 }

 // 人格正文预览：复用通知页 / 文档页同一套 Markdown 渲染与清洗能力，
 // 让用 Markdown 书写的人格提示词也能获得标题、列表、代码块等排版。
 function PersonaPromptPreview({ content }) {
     const raw = (content === undefined || content === null) ? '' : String(content);

     const rendered = React.useMemo(() => {
         if (!raw.trim()) return { isMarkdown: false, html: '' };
         const util = window.MarkdownRenderUtil;
         if (!util || typeof util.renderMarkdownToHtml !== 'function') {
             return { isMarkdown: false, html: '' };
         }
         return {
             // 纯文本人格也允许按 Markdown 渲染：没有语法时输出等价段落，不会变形。
             isMarkdown: true,
             html: util.renderMarkdownToHtml(raw),
         };
     }, [raw]);

     if (!rendered.isMarkdown) {
         // Markdown 工具缺失时退回等宽纯文本，至少保证正文完整可读。
         return (
             <Typography
                 component="pre"
                 sx={{
                     m: 0,
                     fontFamily: 'inherit',
                     fontSize: '0.875rem',
                     lineHeight: 1.75,
                     whiteSpace: 'pre-wrap',
                     wordBreak: 'break-word',
                 }}
             >
                 {raw || '该人格没有可预览的正文内容'}
             </Typography>
         );
     }

     return (
         <Box
             className="notification-md persona-prompt-md"
             // HTML 已在 MarkdownRenderUtil 中完成 sanitize，这里只负责挂载。
             dangerouslySetInnerHTML={{ __html: rendered.html }}
         />
     );
 }

 // 人格选择器：AstrBot 的 select_persona 在这里用等价下拉实现，
 // 特殊项（跟随当前人格 / 不使用人格）与真实人格合并展示。
 // 人格提示词正文改由弹窗查看，避免长文本撑开表单布局。
 function PersonaSelectField({ schema, fieldKey, localValue, onChange, depth = 1 }) {
     const [personas, setPersonas] = useState(personaStore.items || []);
     const [specialOptions, setSpecialOptions] = useState(personaStore.specialOptions || []);
     const [previewOpen, setPreviewOpen] = useState(false);

     useEffect(() => {
         let disposed = false;
         loadPersonasOnce().then((store) => {
             if (disposed) return;
             setPersonas(store.items || []);
             setSpecialOptions(store.specialOptions || []);
         });
         return () => { disposed = true; };
     }, []);

     const currentValue = localValue !== undefined && localValue !== null
         ? String(localValue)
         : (schema.default !== undefined ? String(schema.default) : '');

     // 已保存的人格可能已被删除，这里补一个占位项，避免下拉框显示空白。
     const knownValues = new Set([
         ...specialOptions.map((item) => item.value),
         ...personas.map((item) => item.name),
     ]);
     const valueMissing = currentValue !== '' && !knownValues.has(currentValue);
     const currentPersona = personas.find((item) => item.name === currentValue);
     const currentSpecial = specialOptions.find((item) => item.value === currentValue);
     const currentLabel = (currentSpecial || {}).label || '';
     // 只有选中真实人格时才提供正文预览入口。
     const canPreview = Boolean(currentPersona && currentPersona.prompt);

     return (
         <Box sx={rowGridSx(depth)}>
             {/* 描述固定 50%，下拉框占 20%，与其他普通字段共用同一套栅格。 */}
             <DescriptionSection schema={schema} fieldKey={fieldKey} />

             {/* 列方向时 justifyContent 需重置，否则 flex-end 会让控件贴到底部。 */}
             <Box sx={{
                 ...CONFIG_CONTROL_CELL_SX,
                 flexDirection: 'column',
                 alignItems: 'stretch',
                 justifyContent: 'flex-start',
                 gap: 0.75
             }}>
                 <TextField
                     select
                     fullWidth
                     size="small"
                     value={currentValue}
                     onChange={(e) => onChange(e.target.value)}
                     variant="outlined"
                     SelectProps={{
                         MenuProps: {
                             PaperProps: {
                                 sx: { borderRadius: 1.5, mt: 0.5, boxShadow: 'var(--md-menu-shadow)' }
                             }
                         }
                     }}
                     sx={{
                         '& .MuiOutlinedInput-root': {
                             borderRadius: 1.5,
                             bgcolor: 'background.paper',
                             '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                         },
                         '& .MuiSelect-select': {
                             overflow: 'hidden',
                             textOverflow: 'ellipsis'
                         }
                     }}
                 >
                     {specialOptions.map((option) => (
                         <MenuItem key={`special_${option.value}`} value={option.value}>
                             {option.label}
                         </MenuItem>
                     ))}
                     {valueMissing && (
                         <MenuItem key="missing" value={currentValue}>
                             {`${currentValue}（人格不存在）`}
                         </MenuItem>
                     )}
                     {personas.map((persona) => (
                         <MenuItem key={persona.name} value={persona.name}>
                             {persona.name}
                         </MenuItem>
                     ))}
                 </TextField>

                 {/* 状态行：特殊选项说明或人格正文预览入口，保持单行不撑高表单。 */}
                 <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, justifyContent: 'flex-start', flexWrap: 'wrap' }}>
                     {currentSpecial && (
                         <Chip size="small" label={currentLabel} variant="outlined" />
                     )}
                     {canPreview && (
                         <Button
                             size="small"
                             variant="text"
                             onClick={() => setPreviewOpen(true)}
                             sx={{ minWidth: 'auto', px: 1, py: 0, fontSize: '0.75rem' }}
                         >
                             查看人格内容
                         </Button>
                     )}
                 </Box>
             </Box>

             <Dialog
                 open={previewOpen}
                 onClose={() => setPreviewOpen(false)}
                 maxWidth="md"
                 fullWidth
                 PaperProps={{ sx: { borderRadius: 3 } }}
             >
                 <DialogTitle sx={{ fontWeight: 800, pb: 1 }}>
                     {`人格内容 · ${currentPersona ? currentPersona.name : fieldKey}`}
                 </DialogTitle>
                 <DialogContent dividers>
                     <PersonaPromptPreview content={currentPersona ? currentPersona.prompt : ''} />
                 </DialogContent>
                 <DialogActions sx={{ px: 3, py: 1.5 }}>
                     <Button onClick={() => setPreviewOpen(false)}>关闭</Button>
                 </DialogActions>
             </Dialog>
         </Box>
     );
 }

 // 配置行统一栅格：描述 5fr / 滑块 3fr / 控件 2fr。
 // 必须用 fr 而不是「百分比 + gap」：子项宽度之和会超过容器宽度，
 // 嵌套配置组里就会表现为输入框右侧被裁切看不全。
 const CONFIG_ROW_COLUMNS = '5fr 3fr 2fr';

 function rowGridSx(depth) {
    return {
        display: 'grid',
        gridTemplateColumns: CONFIG_ROW_COLUMNS,
        alignItems: 'center',
        columnGap: 2,
        rowGap: 0.75,
        minWidth: 0,
        py: 1.5,
        borderBottom: depth === 0 ? 'none' : '1px solid',
        borderColor: 'divider',
        '&:last-child': { borderBottom: 'none' }
    };
 }

 // 控件统一落在第三列并右对齐，保证所有行的输入框 / 开关左右边缘严格对齐。
 // 注意不要写 width: 100%，否则 grid 子项会用整行宽度反向撑破列宽。
 const CONFIG_CONTROL_CELL_SX = {
    gridColumn: '3 / 4',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'flex-end',
    minWidth: 0
 };

 // 滑块独占第二列，宽度由 fr 自适应，不会挤压后面的输入框。
 const CONFIG_SLIDER_CELL_SX = {
    gridColumn: '2 / 3',
    display: 'flex',
    alignItems: 'center',
    minWidth: 0
 };

 // 复合控件（如免打扰时间条）跨第二、三列，右边缘与普通控件保持一致。
 const CONFIG_WIDE_CELL_SX = {
    gridColumn: '2 / 4',
    display: 'flex',
    flexDirection: 'column',
    gap: 1,
    minWidth: 0
 };

 // 把字段描述区抽成模块级组件，避免在 ConfigField 渲染体内重复创建组件类型。
 function DescriptionSection({ schema, fieldKey }) {
    return (
        <Box sx={{ gridColumn: '1 / 2', minWidth: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center', wordBreak: 'break-word' }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.9rem', lineHeight: 1.3 }}>
                {schema.description || fieldKey}
            </Typography>
            {schema.hint && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, lineHeight: 1.3 }}>
                    {schema.hint}
                </Typography>
            )}
        </Box>
    );
}

// 免打扰时段控件常量：后端只认 "开始-结束" 的多段字符串，这里负责可视化转换。
const QUIET_HOURS_FIELDS = new Set(['quiet_hours']);
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, index) => index);
// 常用预设顺序：23-07 在前，便于跨天场景一键套用。
const QUIET_HOURS_PRESETS = [
    { start: 23, end: 7, label: '23-07' },
    { start: 2, end: 6, label: '02-06' },
    { start: 1, end: 7, label: '01-07' },
];
// 多时段分隔符与后端保持一致：中英文逗号与顿号都接受。
const QUIET_RANGE_SPLIT_REGEX = /[,，、]/;

function formatHourLabel(hour) {
    // 24 保留为当天结束标记，刻度线上需要显示 24:00
    return `${String(hour).padStart(2, '0')}:00`;
}

// 解析多段 "23-7, 12-14" 配置；空值视为无时段，非法片段统一标记出来。
function parseQuietHours(raw) {
    const text = raw === undefined || raw === null ? '' : String(raw).trim();
    if (!text) {
        return { ranges: [], valid: true, empty: true };
    }

    const ranges = [];
    let valid = true;
    QUIET_RANGE_SPLIT_REGEX.lastIndex = 0;
    text.split(QUIET_RANGE_SPLIT_REGEX).forEach((chunk) => {
        const part = chunk.trim();
        if (!part) return;
        const matched = part.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
        if (!matched) {
            valid = false;
            return;
        }
        const start = Number(matched[1]);
        const end = Number(matched[2]);
        if (start < 0 || start > 23 || end < 0 || end > 24 || start === end) {
            valid = false;
            return;
        }
        ranges.push({ start, end });
    });

    return { ranges, valid, empty: false };
}

// 把区间展开成 24 格布尔数组，跨天区间会同时点亮尾部与头部。
function buildHourGrid(ranges) {
    const grid = new Array(24).fill(false);
    ranges.forEach(({ start, end }) => {
        if (start < end) {
            for (let hour = start; hour < end; hour += 1) grid[hour] = true;
            return;
        }
        // 跨天：例如 23-7 覆盖 23 点与 0-6 点
        for (let hour = start; hour < 24; hour += 1) grid[hour] = true;
        for (let hour = 0; hour < end; hour += 1) grid[hour] = true;
    });
    return grid;
}

// 把 24 格布尔数组还原成紧凑的区间字符串，相邻整点自动合并成一段。
// 关键点：从第一个未覆盖的整点开始环形扫描，跨天段才能自然合并为 23-7 这种形式。
function gridToRangesText(grid) {
    const covered = grid.filter(Boolean).length;
    if (covered === 0) return '';
    if (covered === 24) return '0-24';

    const pivot = grid.findIndex((value) => !value);
    const ranges = [];
    let runStart = null;

    for (let step = 0; step < 24; step += 1) {
        const hour = (pivot + step) % 24;
        if (grid[hour]) {
            if (runStart === null) runStart = hour;
            continue;
        }
        if (runStart !== null) {
            ranges.push({ start: runStart, end: hour });
            runStart = null;
        }
    }

    if (runStart !== null) {
        // 扫描到末尾仍在覆盖中：终点回绕到 pivot，pivot 为 0 时表示当天结束
        ranges.push({ start: runStart, end: pivot === 0 ? 24 : pivot });
    }

    return ranges.map((item) => `${item.start}-${item.end}`).join(',');
}

// 生成可读摘要：跨天段标注次日，便于用户确认语义。
function describeQuietRanges(ranges) {
    if (!ranges.length) return '当前未覆盖任何时段，全天均可发送主动消息';
    const parts = ranges.map(({ start, end }) => (
        start < end
            ? `${formatHourLabel(start)} - ${formatHourLabel(end)}`
            : `${formatHourLabel(start)} - 次日 ${formatHourLabel(end)}`
    ));
    return `${parts.join('、')}（共 ${ranges.length} 段）`;
}

// 免打扰时段可视化控件：可涂抹的 24 格时间条 + 多时段支持。
function QuietHoursField({ schema, fieldKey, value, onChange, depth = 1 }) {
    const parsed = parseQuietHours(value !== undefined ? value : schema.default);
    const grid = buildHourGrid(parsed.ranges);
    const coveredHours = grid.filter(Boolean).length;

    // 涂抹状态：null 表示未按下，否则记录本次拖拽要写入的目标值
    const paintRef = React.useRef(null);
    // 拖拽期间 value 可能还没回流到渲染，这里用 ref 累积，避免连续涂抹互相覆盖
    const gridRef = React.useRef(grid);

    // 外部值变化（切换会话 / 撤销 / 恢复默认）时重置拖拽基线，否则会拿旧格子继续涂抹
    React.useEffect(() => {
        gridRef.current = buildHourGrid(parseQuietHours(value !== undefined ? value : schema.default).ranges);
    }, [value, schema.default]);

    const commitGrid = (nextGrid) => {
        gridRef.current = nextGrid;
        onChange(gridToRangesText(nextGrid));
    };

    const applyCell = (hour, targetValue) => {
        if (gridRef.current[hour] === targetValue) return;
        const next = gridRef.current.slice();
        next[hour] = targetValue;
        commitGrid(next);
    };

    const handleCellPointerDown = (hour) => (event) => {
        // 阻止文本选中；触屏上必须释放隐式指针捕获，
        event.preventDefault();
        if (typeof event.currentTarget.releasePointerCapture === 'function') {
            try {
                event.currentTarget.releasePointerCapture(event.pointerId);
            } catch (e) {
                // 部分浏览器在未捕获时抛错，忽略即可。
            }
        }
        // 基线与渲染保持一致
        const baselineValue = value !== undefined ? value : schema.default;
        gridRef.current = buildHourGrid(parseQuietHours(baselineValue).ranges);
        const targetValue = !gridRef.current[hour];
        paintRef.current = targetValue;
        applyCell(hour, targetValue);
    };

    const handleCellPointerEnter = (hour) => () => {
        if (paintRef.current === null) return;
        applyCell(hour, paintRef.current);
    };

    // 指针离开时间条即结束本次涂抹
    const endPaint = () => {
        paintRef.current = null;
    };

    React.useEffect(() => {
        window.addEventListener('pointerup', endPaint);
        window.addEventListener('pointercancel', endPaint);
        return () => {
            window.removeEventListener('pointerup', endPaint);
            window.removeEventListener('pointercancel', endPaint);
        };
    }, []);

    // 预设按「叠加切换」处理：已存在则移除该段，不存在则补上，
    // 这样用户可以在保留自己勾选的时段基础上快速增删常用段。
    const applyPreset = (preset) => {
        const base = gridRef.current.slice();
        const presetGrid = buildHourGrid([{ start: preset.start, end: preset.end }]);
        const alreadyApplied = presetGrid.every((covered, hour) => !covered || base[hour]);
        const next = base.map((covered, hour) => {
            if (!presetGrid[hour]) return covered;
            return !alreadyApplied;
        });
        commitGrid(next);
    };

    const clearAll = () => onChange('');

    return (
        <Box
            sx={{
                py: 1.75,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}
        >
            {/* 免打扰是复合可视化控件（可涂抹时间条），跨第 2-3 列占满剩余宽度，
                这样它的右边缘与其它输入框保持一致，普通控件仍固定在第三列 20%。 */}
            <Box sx={{ display: 'grid', gridTemplateColumns: CONFIG_ROW_COLUMNS, columnGap: 2, alignItems: 'flex-start', minWidth: 0 }}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} />
                <Box sx={CONFIG_WIDE_CELL_SX}>
                    {/* 时间条主体：24 个可涂抹格子，未覆盖的格子也保留可见分段 */}
                    <Box
                        onPointerLeave={endPaint}
                        sx={{
                            display: 'flex',
                            gap: '2px',
                            p: '3px',
                            borderRadius: 1.5,
                            bgcolor: 'action.hover',
                            border: '1px solid',
                            borderColor: 'divider',
                            userSelect: 'none',
                            touchAction: 'none'
                        }}
                    >
                        {HOUR_OPTIONS.map((hour) => {
                            const active = grid[hour];
                            const hourEnd = hour + 1;
                            // 每 6 小时加一条更明显的分隔线，形成 0/6/12/18/24 四段视觉分区
                            const majorDivider = hour % 6 === 0 && hour !== 0;
                            return (
                                <Box
                                    key={hour}
                                    role="button"
                                    tabIndex={0}
                                    aria-label={`${formatHourLabel(hour)} 至 ${formatHourLabel(hourEnd)}`}
                                    aria-pressed={active}
                                    title={`${formatHourLabel(hour)} - ${formatHourLabel(hourEnd)}`}
                                    onPointerDown={handleCellPointerDown(hour)}
                                    onPointerEnter={handleCellPointerEnter(hour)}
                                    onKeyDown={(event) => {
                                        if (event.key === 'Enter' || event.key === ' ') {
                                            event.preventDefault();
                                            applyCell(hour, !active);
                                        }
                                    }}
                                    sx={{
                                        flex: 1,
                                        height: 26,
                                        borderRadius: '4px',
                                        cursor: 'pointer',
                                        position: 'relative',
                                        bgcolor: active ? 'primary.main' : 'background.paper',
                                        border: '1px solid',
                                        borderColor: active ? 'primary.dark' : 'divider',
                                        transition: 'background-color 0.12s ease, border-color 0.12s ease',
                                        ml: majorDivider ? '7px' : 0,
                                        // 主分隔线：在图示上更醒目地区分 6 小时区间
                                        '&::before': majorDivider ? {
                                            content: '""',
                                            position: 'absolute',
                                            left: '-5px',
                                            top: '-5px',
                                            bottom: '-5px',
                                            width: '2px',
                                            borderRadius: '1px',
                                            bgcolor: 'text.secondary',
                                            opacity: 0.85
                                        } : undefined,
                                        '&:hover': {
                                            borderColor: 'primary.main'
                                        },
                                        '&:focus-visible': {
                                            outline: '2px solid',
                                            outlineColor: 'primary.main',
                                            outlineOffset: '1px'
                                        }
                                    }}
                                />
                            );
                        })}
                    </Box>

                    {/* 时间刻度：绝对定位到 0/6/12/18/24 对应位置，与主分隔线对齐 */}
                    <Box sx={{ position: 'relative', height: 15, mt: '-0.25rem' }}>
                        {[0, 6, 12, 18, 24].map((mark) => {
                            const emphasis = mark % 12 === 0;
                            const isFirst = mark === 0;
                            const isLast = mark === 24;
                            return (
                                <Typography
                                    key={mark}
                                    variant="caption"
                                    color={emphasis ? 'text.secondary' : 'text.disabled'}
                                    sx={{
                                        position: 'absolute',
                                        top: 0,
                                        left: isLast ? undefined : `${(mark / 24) * 100}%`,
                                        right: isLast ? 0 : undefined,
                                        transform: isFirst || isLast ? 'none' : 'translateX(-50%)',
                                        fontSize: '0.7rem',
                                        fontWeight: emphasis ? 700 : 400,
                                        whiteSpace: 'nowrap'
                                    }}
                                >
                                    {formatHourLabel(mark)}
                                </Typography>
                            );
                        })}
                    </Box>

                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, flexWrap: 'wrap' }}>
                        <Typography variant="caption" color={parsed.valid ? 'text.secondary' : 'warning.main'}>
                            {parsed.valid
                                ? `${describeQuietRanges(parsed.ranges)}${coveredHours > 0 ? ` · 共 ${coveredHours} 小时` : ''}`
                                : `当前值 “${String(value)}” 含非法片段，仅展示可解析的部分`}
                        </Typography>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                            <Typography variant="caption" color="text.secondary">常用：</Typography>
                            {QUIET_HOURS_PRESETS.map((preset) => {
                                const active = parsed.ranges.some(
                                    (range) => range.start === preset.start && range.end === preset.end
                                );
                                return (
                                    <Chip
                                        key={preset.label}
                                        size="small"
                                        label={preset.label}
                                        color={active ? 'primary' : 'default'}
                                        variant={active ? 'filled' : 'outlined'}
                                        onClick={() => applyPreset(preset)}
                                        sx={{ height: 22, fontSize: '0.7rem' }}
                                    />
                                );
                            })}
                            <Chip
                                size="small"
                                label="清空"
                                variant="outlined"
                                onClick={clearAll}
                                disabled={coveredHours === 0}
                                sx={{ height: 22, fontSize: '0.7rem' }}
                            />
                        </Box>
                    </Box>
                </Box>
            </Box>
        </Box>
    );
}

function ConfigField({ fieldKey, schema, value, onChange, depth = 0, path = '', expandedKeys = [], onToggleExpand = () => {}, sessionType = '' }) {
    // 隐藏字段完全不渲染：hidden 为本项目写法，invisible 与 AstrBot 面板保持一致，
    // 两者都用于后端保留字段或不希望用户直接编辑的项。
    if (schema.hidden || schema.invisible) return null;

    // 对输入控件使用一层本地态，便于处理 Slider / 文本输入中的过渡值。
    const [localValue, setLocalValue] = useState(value);

    useEffect(() => {
        // 当外部配置值变化（如切换会话 / 回滚 / 重新加载）时，同步重置局部编辑态。
        setLocalValue(value);
    }, [value]);

    const handleChange = (newValue) => {
        // 所有字段都通过统一出口把最新值同时写入本地态与父级配置对象。
        setLocalValue(newValue);
        onChange(newValue);
    };

    const rawTitle = schema.description || fieldKey;
    const titleText = stripLeadingEmoji(rawTitle) || rawTitle;
    const icon = CONFIG_ICONS[fieldKey] || '⚙️';
    const currentPath = path ? `${path}.${fieldKey}` : fieldKey;

    // AstrBot 的 _special 控件类型：select_persona 用同源人格列表渲染。
    if (schema._special === 'select_persona') {
        return (
            <PersonaSelectField
                schema={schema}
                fieldKey={fieldKey}
                localValue={localValue}
                onChange={handleChange}
                depth={depth}
            />
        );
    }

    // 服务提供商选择：对外仍是字符串，可视化编辑器只存在于 UI 层。
    if (schema._special === 'select_provider') {
        const ProviderSelectField = window.ProviderSelectField;
        if (ProviderSelectField) {
            return (
                <ProviderSelectField
                    schema={schema}
                    fieldKey={fieldKey}
                    value={localValue !== undefined ? localValue : (schema.default || '')}
                    onChange={handleChange}
                    depth={depth}
                />
            );
        }
    }

    // 回退链：对外是 string[]，UI 提供上下移动与 provider 联动。
    if (schema._special === 'select_providers') {
        const ProviderChainField = window.ProviderChainField;
        if (ProviderChainField) {
            return (
                <ProviderChainField
                    schema={schema}
                    fieldKey={fieldKey}
                    value={localValue !== undefined ? localValue : (schema.default || [])}
                    onChange={handleChange}
                    depth={depth}
                />
            );
        }
    }

    // 免打扰时段改为可视化控件，内部仍输出后端可直接解析的 "开始-结束" 字符串。
    if (QUIET_HOURS_FIELDS.has(fieldKey) && (!schema.type || schema.type === 'string')) {
        return (
            <QuietHoursField
                schema={schema}
                fieldKey={fieldKey}
                value={localValue}
                onChange={handleChange}
                depth={depth}
            />
        );
    }

    // 提示词模板列表使用专用结构化编辑器：需要类型/适用范围/占位符快捷插入。
    // 放在通用 list 之前，避免被“每行一项”的文本框吞掉结构化字段。
    if (schema.type === 'template_list') {
        const TemplateListField = window.TemplateListField;
        if (TemplateListField) {
            return (
                <TemplateListField
                    schema={schema}
                    fieldKey={fieldKey}
                    value={localValue !== undefined ? localValue : (schema.default || [])}
                    onChange={handleChange}
                    depth={depth}
                    // 会话差异页只展示对本会话类型有效的模板，全局页传空串不过滤。
                    sessionType={sessionType}
                />
            );
        }
    }

    // object + items 表示一个可折叠的配置分组，内部再递归渲染子字段。
    if (schema.type === 'object' && schema.items) {
        const isExpanded = expandedKeys.includes(currentPath);

        return (
            <Paper
                elevation={depth === 0 ? 2 : 0}
                sx={{
                    my: depth === 0 ? 1 : 0.75,
                    overflow: 'hidden',
                    border: depth === 0 ? 1.5 : 1,
                    borderColor: depth === 0 ? 'primary.main' : 'primary.light',
                    borderRadius: 2,
                    background: depth === 0
                        ? 'linear-gradient(135deg, rgb(var(--md-primary-rgb) / 0.04) 0%, rgb(var(--md-primary-rgb) / 0.01) 100%)'
                        : 'transparent'
                }}
            >
                <Accordion
                    expanded={isExpanded}
                    onChange={() => onToggleExpand(currentPath)}
                    elevation={0}
                    sx={{
                        '&:before': { display: 'none' },
                        bgcolor: 'transparent'
                    }}
                >
                    <AccordionSummary
                        expandIcon={
                            <Box sx={{ fontSize: '14px', lineHeight: 1 }}>
                                {isExpanded ? '▲' : '▼'}
                            </Box>
                        }
                        sx={{
                            px: 2,
                            py: 1,
                            minHeight: '48px !important',
                            bgcolor: depth === 0
                                ? 'rgb(var(--md-primary-rgb) / 0.06)'
                                : depth === 1
                                    ? 'rgb(var(--md-primary-rgb) / 0.02)'
                                    : 'transparent',
                            '&:hover': {
                                bgcolor: depth === 0
                                    ? 'rgb(var(--md-primary-rgb) / 0.1)'
                                    : 'rgb(var(--md-primary-rgb) / 0.05)'
                            },
                            transition: 'all 0.2s ease',
                            '& .MuiAccordionSummary-expandIconWrapper': {
                                transform: 'none !important'
                            },
                            '& .MuiAccordionSummary-expandIconWrapper.Mui-expanded': {
                                transform: 'none !important'
                            }
                        }}
                    >
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flex: 1 }}>
                            <Box sx={{ fontSize: depth === 0 ? '20px' : '16px' }}>{icon}</Box>
                            <Box sx={{ flex: 1 }}>
                                <Typography
                                    variant="subtitle2"
                                    sx={{
                                        fontWeight: 700,
                                        color: depth === 0 ? 'primary.main' : depth > 0 ? 'primary.dark' : 'text.primary',
                                        fontSize: depth === 0 ? '0.95rem' : '0.875rem'
                                    }}
                                >
                                    {titleText}
                                </Typography>
                                {schema.hint && depth === 0 && (
                                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25 }}>
                                        {schema.hint}
                                    </Typography>
                                )}
                            </Box>
                            {depth === 0 && (
                                <Chip
                                    label={`${Object.keys(schema.items).length}项`}
                                    size="small"
                                    color="primary"
                                    variant="outlined"
                                    sx={{ height: 22, fontSize: '0.7rem' }}
                                />
                            )}
                            {depth > 0 && (
                                <Typography variant="caption" sx={{ color: 'primary.main', fontWeight: 600, mr: 0.5 }}>
                                    {isExpanded ? '收起' : '展开'}
                                </Typography>
                            )}
                        </Box>
                    </AccordionSummary>
                    <AccordionDetails sx={{ px: 3, py: 0, bgcolor: 'background.default', minWidth: 0 }}>
                        <Box sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                            {Object.entries(schema.items).map(([key, subSchema]) => (
                                <ConfigField
                                    key={key}
                                    fieldKey={key}
                                    schema={subSchema}
                                    value={localValue ? localValue[key] : undefined}
                                    onChange={(newValue) => handleChange(Object.assign({}, localValue, { [key]: newValue }))}
                                    depth={depth + 1}
                                    path={currentPath}
                                    expandedKeys={expandedKeys}
                                    onToggleExpand={onToggleExpand}
                                    // 必须向下透传：模板列表嵌在 prompt_settings 这一层内部，
                                    // 漏传会让会话差异页拿不到会话类型，过滤逻辑整体失效。
                                    sessionType={sessionType}
                                />
                            ))}
                        </Box>
                    </AccordionDetails>
                </Accordion>
            </Paper>
        );
    }

    // 布尔配置映射为开关组件，适合表示 enable / always_send_text 这类 on-off 项。
    if (schema.type === 'bool' || schema.type === 'boolean') {
        return (
            <Box sx={rowGridSx(depth)}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} />
                {/* 开关放在第三列最右端，与其它输入框的右边缘严格对齐。 */}
                <Box sx={CONFIG_CONTROL_CELL_SX}>
                    <Switch
                        checked={localValue !== undefined ? localValue : (schema.default || false)}
                        onChange={(e) => handleChange(e.target.checked)}
                        sx={{
                            width: 52,
                            height: 32,
                            padding: 0,
                            '& .MuiSwitch-switchBase': {
                                padding: 0,
                                margin: '4px',
                                transitionDuration: '300ms',
                                '&.Mui-checked': {
                                    transform: 'translateX(20px)',
                                    color: 'var(--md-sys-color-on-primary)',
                                    '& + .MuiSwitch-track': {
                                        backgroundColor: 'primary.main',
                                        opacity: 1,
                                        border: 0,
                                    },
                                },
                            },
                            '& .MuiSwitch-thumb': {
                                boxSizing: 'border-box',
                                width: 24,
                                height: 24,
                                boxShadow: 'var(--md-switch-thumb-shadow)',
                            },
                            '& .MuiSwitch-track': {
                                borderRadius: 16,
                                backgroundColor: 'var(--md-switch-track)',
                                opacity: 1,
                                transition: 'background-color 300ms',
                            },
                        }}
                    />
                </Box>
            </Box>
        );
    }

    // 数值字段支持“滑杆 + 数字输入框”的双通道编辑，兼顾直观性与精确输入。
    if (['integer', 'int', 'number', 'float', 'double'].includes(schema.type)) {
        const sliderConfig = schema.slider;
        const hasRange = sliderConfig !== undefined;

        const min = sliderConfig && sliderConfig.min !== undefined ? sliderConfig.min : schema.minimum;
        const max = sliderConfig && sliderConfig.max !== undefined ? sliderConfig.max : schema.maximum;
        const step = (sliderConfig && sliderConfig.step !== undefined)
            ? sliderConfig.step
            : (schema.type === 'integer' || schema.type === 'int' ? 1 : 0.1);
        const fallbackNumber = schema.default !== undefined ? schema.default : (min !== undefined ? min : 0);
        const parsedLocalNumber = Number(localValue);
        const sliderValueRaw = Number.isFinite(parsedLocalNumber) ? parsedLocalNumber : Number(fallbackNumber);
        const sliderValue = Math.min(
            max !== undefined ? max : sliderValueRaw,
            Math.max(min !== undefined ? min : sliderValueRaw, sliderValueRaw)
        );

        return (
            <Box sx={rowGridSx(depth)}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} />

                {hasRange && (
                    <Box sx={CONFIG_SLIDER_CELL_SX}>
                        <Slider
                            value={sliderValue}
                            onChange={(e, newValue) => setLocalValue(newValue)}
                            onChangeCommitted={(e, newValue) => handleChange(newValue)}
                            min={min}
                            max={max}
                            step={step}
                            size="small"
                            valueLabelDisplay="auto"
                            sx={{ color: 'primary.main' }}
                        />
                    </Box>
                )}

                <Box sx={CONFIG_CONTROL_CELL_SX}>
                    <TextField
                        fullWidth
                        size="small"
                        type="number"
                        value={localValue !== undefined ? localValue : (schema.default || 0)}
                        onChange={(e) => {
                            const v = e.target.value;
                            // 允许输入空值或负号，避免编辑过程中被强制回填。
                            if (v === '' || v === '-') {
                                setLocalValue(v);
                                return;
                            }
                            const num = schema.type === 'integer' || schema.type === 'int' ? parseInt(v, 10) : parseFloat(v);
                            handleChange(isNaN(num) ? 0 : num);
                        }}
                        inputProps={{
                            min: min,
                            max: max,
                            step: step
                        }}
                        variant="outlined"
                        sx={{
                            '& .MuiOutlinedInput-root': {
                                borderRadius: 1.5,
                                bgcolor: 'background.paper',
                                '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                            }
                        }}
                    />
                </Box>
            </Box>
        );
    }

    // 列表字段使用“每行一项”的多行文本输入，适合编辑 session_list / split_words 等数组数据。
    if (schema.type === 'list' || schema.type === 'array') {
        return (
            <Box sx={{
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <Box sx={{ mb: 1 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.9rem' }}>
                        {schema.description || fieldKey}
                    </Typography>
                    {schema.hint && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                            {schema.hint}
                        </Typography>
                    )}
                </Box>
                <TextField
                    fullWidth
                    multiline
                    rows={3}
                    size="small"
                    value={Array.isArray(localValue) ? localValue.join('\n') : ''}
                    onChange={(e) => handleChange(e.target.value.split('\n'))}
                    placeholder="每行一项"
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            borderRadius: 1.5,
                            bgcolor: 'background.paper',
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        );
    }

    // 若 Schema 给出了 options，则优先渲染为下拉选择框，而不是自由文本输入。
    if (schema.options && Array.isArray(schema.options)) {
        const optionLabels = Array.isArray(schema.labels) ? schema.labels : [];
        const optionLabelMap = new Map(
            schema.options.map((option, index) => [option, optionLabels[index] || option])
        );
        const currentValue = localValue !== undefined ? localValue : (schema.default || '');

        return (
            <Box sx={rowGridSx(depth)}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} />
                <Box sx={CONFIG_CONTROL_CELL_SX}>
                    <TextField
                        select
                        fullWidth
                        size="small"
                        value={currentValue}
                        onChange={(e) => handleChange(e.target.value)}
                        variant="outlined"
                        SelectProps={{
                            renderValue: (selected) => optionLabelMap.get(selected) || selected,
                            MenuProps: {
                                PaperProps: {
                                    sx: {
                                        borderRadius: 1.5,
                                        mt: 0.5,
                                        boxShadow: 'var(--md-menu-shadow)'
                                    }
                                }
                            }
                        }}
                        sx={{
                            '& .MuiOutlinedInput-root': {
                                borderRadius: 1.5,
                                bgcolor: 'background.paper',
                                '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                            },
                            '& .MuiSelect-select': {
                                textAlign: 'center',
                                pr: '32px !important' // 给箭头图标留出空间，确保视觉居中
                            }
                        }}
                    >
                        {schema.options.map((option) => (
                            <MenuItem key={option} value={option} sx={{ justifyContent: 'center' }}>
                                {optionLabelMap.get(option) || option}
                            </MenuItem>
                        ))}
                    </TextField>
                </Box>
            </Box>
        );
    }

    // 只有真正需要大文本块的字段才允许全宽，其余一律走 5fr/3fr/2fr 横排栅格。
    const shouldBeMultiline =
        schema.type === 'text' ||
        fieldKey.includes('prompt') ||
        fieldKey.includes('template');
    const multilineRows =
        fieldKey.includes('prompt') || schema.type === 'text'
            ? 8
            : 4;

    // 长文本保持上下全宽布局；其余字段使用统一栅格。
    if (shouldBeMultiline) {
        return (
            <Box sx={{
                py: 1.75,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <Box sx={{ mb: 1.25 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.95rem', lineHeight: 1.35 }}>
                        {schema.description || fieldKey}
                    </Typography>
                    {schema.hint && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, lineHeight: 1.5 }}>
                            {schema.hint}
                        </Typography>
                    )}
                </Box>
                <TextField
                    fullWidth
                    multiline
                    minRows={multilineRows}
                    maxRows={fieldKey.includes('prompt') || schema.type === 'text' ? 18 : 10}
                    value={localValue !== undefined ? localValue : (schema.default || '')}
                    onChange={(e) => handleChange(e.target.value)}
                    placeholder={fieldKey.includes('prompt') ? '请输入该会话/全局场景下的 Prompt 内容…' : ''}
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            alignItems: 'flex-start',
                            borderRadius: 2,
                            bgcolor: 'background.paper',
                            fontFamily: 'inherit',
                            lineHeight: 1.65,
                            '& textarea': {
                                lineHeight: 1.7,
                                fontSize: '0.95rem',
                                resize: 'vertical'
                            },
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        );
    }

    return (
        <Box sx={rowGridSx(depth)}>
            <DescriptionSection schema={schema} fieldKey={fieldKey} />
            <Box sx={CONFIG_CONTROL_CELL_SX}>
                <TextField
                    fullWidth
                    size="small"
                    value={localValue !== undefined ? localValue : (schema.default || '')}
                    onChange={(e) => handleChange(e.target.value)}
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            borderRadius: 1.5,
                            bgcolor: 'background.paper',
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        </Box>
    );
}

window.ConfigField = ConfigField;
})();
