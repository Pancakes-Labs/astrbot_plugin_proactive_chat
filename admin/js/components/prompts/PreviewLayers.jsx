(() => {
/**
 * 文件职责：完整 Prompt 的分层渲染。
 *
 * 服务端返回的 sections 是一条平铺长文，固定层（系统任务 / 会话场景 /
 * 输出风格 / 安全约束）动辄上千字，会把本轮抽中模板淹没。
 * 这里把固定层折成一行小胶囊，只让模板层与动态上下文常驻展开。
 *
 * 全站只有这一份渲染逻辑，卡盒体检面板与精修抽屉都复用它。
 */

const { Box, Typography, LinearProgress, Chip } = MaterialUI;
const { useState, useEffect } = React;

// 这些 kind 是运行期固定注入的约束层，默认收起。
function isCollapsible(section) {
    return section && section.kind === 'contract';
}

function PreviewLayers({ preview, previewing, emptyHint }) {
    const [expanded, setExpanded] = useState({});
    const sections = (preview && preview.sections) || [];

    // 换一次渲染结果就重置折叠状态：留着上一张卡的展开项会让人以为看错了卡。
    const signature = preview
        ? `${preview.session_type}|${preview.template && preview.template.id}|${sections.length}`
        : '';
    useEffect(() => {
        setExpanded({});
    }, [signature]);

    const toggle = (index) => {
        setExpanded((prev) => ({ ...prev, [index]: !prev[index] }));
    };

    if (previewing && !sections.length) {
        return <LinearProgress sx={{ mt: 1 }} />;
    }

    if (!sections.length) {
        return (
            <Typography variant="caption" color="text.secondary">
                {emptyHint || '还没有渲染结果。'}
            </Typography>
        );
    }

    return (
        <Box className="pv-layers">
            <Box className="pv-layers-meta">
                <Chip
                    size="small"
                    className="pv-layers-hit"
                    color={preview.template && preview.template.matched ? 'primary' : 'default'}
                    label={(preview.template && preview.template.name) || '无模板可用'}
                />
                <span className="pv-layers-sub">
                    {preview.sampled ? '本轮现场抽签' : '已锁定当前卡'}
                    {' · '}
                    {preview.char_count} 字
                </span>
                {previewing && <LinearProgress sx={{ flex: 1, height: 3, borderRadius: 2 }} />}
            </Box>

            <Box className="pv-layers-body">
                {sections.map((section, index) => {
                    const collapsible = isCollapsible(section);
                    const isHit = section.kind === 'template' || section.kind === 'extra';
                    const open = isHit || Boolean(expanded[index]);
                    return (
                        <Box
                            key={`${section.source}-${index}`}
                            className={`pv-layer${isHit ? ' is-hit' : ''}${open ? ' is-open' : ''}`}
                        >
                            <button
                                type="button"
                                className="pv-layer-head"
                                onClick={() => (collapsible ? toggle(index) : undefined)}
                                aria-expanded={open}
                            >
                                <span className="pv-layer-caret">
                                    {collapsible ? (open ? '▾' : '▸') : '·'}
                                </span>
                                <span className="pv-layer-label">{section.label}</span>
                                <span className="pv-layer-words">{section.text.length} 字</span>
                            </button>
                            {open && (
                                <Box className="pv-layer-body mono">{section.text}</Box>
                            )}
                        </Box>
                    );
                })}
            </Box>
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.PreviewLayers = PreviewLayers;
})();
