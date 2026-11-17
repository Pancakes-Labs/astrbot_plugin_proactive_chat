(() => {
/**
 * 文件职责：单张卡牌的放大态。
 *
 * 同一个卡片模型的长大版本：同一条花色带、同一个钢印序号、同一种纸色，视觉语言完全一致。
 * 动画从被点击卡片的位置与尺寸长出（--dx/--dy/--k 由调用方注入），关闭时原路缩回那张卡片。
 *
 * 关闭相位由父层持有（closing prop）：
 * 所有关闭路径都调 onClose，父层标记 closing 播退场动画，
 * 动画结束后父层才卸载。组件自己不持有任何定时器。
 *
 * 左栏编辑，右栏始终显示这张卡装配后的完整提示词，所见即所得。
 * 卡面上被刻意去掉的所有操作（启用开关、删除）都收在这里。
 */

const {
    Box, Typography, Button, IconButton, Switch, Tooltip,
} = MaterialUI;
const { useEffect } = React;
const { PromptEditor, PreviewLayers } = window.PromptComponents;
const { scopeLabel } = window.PromptUtils;

function PromptModal({
    item, isDraft, index, probability, share, preview, previewing, saving, closing,
    origin, onPatch, onPreview, onClose, onRemove, onSave,
}) {
    const itemId = item ? item.id : '';

    // 打开就渲染一次，锁定这张卡；之后改正文不会自动重渲染。
    useEffect(() => {
        if (itemId) onPreview(item);
        // 只依赖卡 id：同一张卡内部编辑不触发重新渲染。
    }, [itemId]);

    if (!item) return null;

    const tone = item.scope || 'any';

    const originVars = origin
        ? { '--dx': `${origin.dx}px`, '--dy': `${origin.dy}px`, '--k': origin.k }
        : {};

    return (
        <Box className="pv-modal-root" role="dialog" aria-modal="true">
            <Box
                className={`pv-modal-mask${closing ? ' is-closing' : ''}`}
                onClick={onClose}
            />

            <Box
                className={`pv-modal accent-${tone}${closing ? ' is-closing' : ''}`}
                style={originVars}
            >
                {/* 头部只做身份展示与开关。 */}
                <Box className="pv-modal-head">
                    {/* 新建态不显示序号。 */}
                    <span className={`pv-modal-no${isDraft ? ' is-draft' : ''}`}>
                        {isDraft ? '新' : String((index || 0) + 1).padStart(2, '0')}
                    </span>
                    <Box className="pv-modal-title-wrap">
                        <Typography component="h3" className="pv-modal-title">
                            {item.name || (isDraft ? '新建卡牌' : '未命名模板')}
                        </Typography>
                        <Box className="pv-modal-head-meta">
                            <span className="pv-card-scope">{scopeLabel(item.scope)}</span>
                            <span className={`pv-modal-state${item.enabled ? ' is-on' : ''}`}>
                                {item.enabled ? '参与抽样' : '已停用'}
                            </span>
                            {isDraft && (
                                <span className="pv-modal-draft-hint">尚未保存</span>
                            )}
                        </Box>
                    </Box>

                    {/* 启用开关：卡面上不出现，只在这里。 */}
                    <Tooltip title={item.enabled ? '停用这张卡' : '启用这张卡'}>
                        <Switch
                            size="small"
                            checked={item.enabled}
                            onChange={(event) => onPatch({ enabled: event.target.checked })}
                        />
                    </Tooltip>

                    <Tooltip title="关闭">
                        <IconButton size="small" onClick={onClose}>
                            <Typography component="span" sx={{ fontSize: '0.85rem' }}>✕</Typography>
                        </IconButton>
                    </Tooltip>
                </Box>

                <Box className="pv-modal-body">
                    <Box className="pv-modal-grid">
                        <PromptEditor
                            item={item}
                            probability={probability}
                            share={share}
                            onPatch={onPatch}
                        />

                        <Box className="pv-editor-preview">
                            <Box className="pv-preview-head">
                                <Typography component="h4" className="pv-inspect-title">
                                    📌 当前预览
                                </Typography>
                                <Button
                                    size="small"
                                    variant="outlined"
                                    onClick={() => onPreview(item)}
                                    disabled={previewing}
                                >
                                    {previewing ? '渲染中…' : '重新渲染'}
                                </Button>
                            </Box>
                            <PreviewLayers
                                key={itemId}
                                preview={preview}
                                previewing={previewing}
                                emptyHint="点「重新渲染」查看这张卡装配后的完整提示词。"
                            />
                        </Box>
                    </Box>
                </Box>

                <Box className="pv-modal-foot">
                    {/* 新建态不提供删除。 */}
                    {isDraft ? (
                        <Box sx={{ flex: 1 }} />
                    ) : (
                        <>
                            <Button color="error" variant="text" size="small" onClick={onRemove}>
                                删除这张卡
                            </Button>
                            <Box sx={{ flex: 1 }} />
                        </>
                    )}
                    <Button size="small" onClick={onClose}>
                        {isDraft ? '放弃' : '关闭'}
                    </Button>
                    <Button
                        size="small"
                        variant="contained"
                        onClick={async () => {
                            // 保存失败时保持打开，否则用户以为改动已经生效了。
                            const ok = await onSave();
                            if (ok) onClose();
                        }}
                        disabled={saving}
                    >
                        {saving ? '保存中…' : (isDraft ? '保存并新建' : '保存并关闭')}
                    </Button>
                </Box>
            </Box>
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.PromptDrawer = PromptModal;
})();
