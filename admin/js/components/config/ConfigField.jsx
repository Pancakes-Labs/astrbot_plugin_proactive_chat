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
 function PersonaSelectField({ schema, fieldKey, localValue, onChange }) {
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
         <Box sx={{ py: 1.5 }}>
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
             <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.75, flexWrap: 'wrap' }}>
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

 // 把字段描述区抽成模块级组件，避免在 ConfigField 渲染体内重复创建组件类型。
 function DescriptionSection({ schema, fieldKey, flex = 8 }) {
    return (
        <Box sx={{ flex: flex, pr: 2, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
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

function ConfigField({ fieldKey, schema, value, onChange, depth = 0, path = '', expandedKeys = [], onToggleExpand = () => {} }) {
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
            />
        );
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
                    <AccordionDetails sx={{ px: 3, py: 0, bgcolor: 'background.default' }}>
                        <Box sx={{ display: 'flex', flexDirection: 'column' }}>
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
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} flex={8} />
                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end', alignItems: 'center' }}>
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
        // 如果有范围，展示 Slider，布局 5:3:2；否则回退到 8:2。
        const descFlex = hasRange ? 5 : 8;

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
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} flex={descFlex} />

                {hasRange && (
                    <Box sx={{ flex: 3, px: 2, display: 'flex', alignItems: 'center' }}>
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

                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
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
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection schema={schema} fieldKey={fieldKey} flex={8} />
                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
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

    // 其余情况按字符串处理，并依据字段语义决定是否切换为多行文本框。
    const shouldBeMultiline =
        schema.type === 'text' ||
        (schema.hint && schema.hint.length > 100) ||
        fieldKey.includes('prompt') ||
        fieldKey.includes('format') ||
        fieldKey.includes('template') ||
        fieldKey.includes('pattern') ||
        fieldKey.includes('message') ||
        fieldKey.includes('body') ||
        fieldKey.includes('content');
    const multilineRows =
        fieldKey.includes('prompt') || schema.type === 'text'
            ? 8
            : 4;

    // 如果是长文本，保持上下布局；否则使用 8:2 布局。
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
        <Box sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            py: 1.5,
            borderBottom: depth === 0 ? 'none' : '1px solid',
            borderColor: 'divider',
            '&:last-child': { borderBottom: 'none' }
        }}>
            <DescriptionSection schema={schema} fieldKey={fieldKey} flex={8} />
            <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
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
