(() => {
/**
 * 文件职责：提示词管理页的卡牌。
 *
 * 卡面是纯展示层：没有开关、滑块、删除按钮和输入框，
 * 一切修改都要点击卡片，让它从卡片自身位置长成模态。
 *
 * 造型是一张挂在钉子上的标签卡：顶部中央一个穿透圆孔，
 * 钉子由外层 pv-card-slot 绘制，两者同轴。
 *
 * 动画只有平移与缩放，不含旋转。
 */

const { Box, Typography } = MaterialUI;
const { formatPercent, scopeLabel, splitTemplateSections } = window.PromptUtils;

// 卡面只留两段：前置说明会长到把正文挤没，它在模态里可以完整看。
const SECTION_LIMIT = 2;

// 概率与占比差距小于该阈值时视为同一个数，只显示一个。
const DUAL_THRESHOLD = 0.005;

/**
 * 把正文渲染成气泡段：优先取 [这次我想] / [最终指令]。
 * 拆不出段头时退化成「正文」单段，splitTemplateSections 内部已兜底。
 */
function SectionBubbles({ text }) {
    const sections = splitTemplateSections(text);
    if (!sections.length) {
        return (
            <Box className="pv-sec is-empty-text">
                <span className="pv-sec-label">正文</span>
                正文为空
            </Box>
        );
    }
    return (
        <Box className="pv-card-sections">
            {sections.slice(0, SECTION_LIMIT).map((section) => (
                <Box
                    key={section.key}
                    // 段型决定配色：这次我想走主色，最终指令走绿。两个类显式给出。
                    className={`pv-sec is-${section.key}`}
                >
                    <span className="pv-sec-label">{section.label}</span>
                    <span className="pv-sec-text">{section.text}</span>
                </Box>
            ))}
        </Box>
    );
}

/**
 * 概率读数。
 *
 * probability：number 是本盒池内的真实抽中率；数组是两个池各一份。
 * share 是盒内权重占比，与抽中率常相等，此时不再重复显示。
 */
function ProbabilityValue({ probability, share }) {
    if (Array.isArray(probability)) {
        return (
            <Box className="pv-card-dual">
                <span className="pv-card-dual-item">
                    <small>私聊</small>
                    {formatPercent(probability[0])}
                </span>
                <span className="pv-card-dual-item">
                    <small>群聊</small>
                    {formatPercent(probability[1])}
                </span>
            </Box>
        );
    }

    const rate = Number(probability) || 0;
    const hasShare = Number.isFinite(Number(share)) && share !== null && share !== undefined;
    const shareValue = hasShare ? Number(share) : 0;
    // share 缺失时不能当成 0，否则会显示「占比 0%」这种假读数。
    const differs = hasShare && Math.abs(rate - shareValue) >= DUAL_THRESHOLD;

    return (
        <Box className="pv-card-rate">
            <span className="pv-card-rate-main">{formatPercent(rate)}</span>
            <span className="pv-card-rate-label">抽中率</span>
            {differs && (
                <span className="pv-card-rate-share" title="该卡在本卡盒内的权重占比，分母只算本盒有效卡">
                    占比 {formatPercent(shareValue)}
                </span>
            )}
        </Box>
    );
}

function PromptCard({
    item, index, order, total, tone, probability, share,
    invalidReason, leaving, ready, instant, flyX, flyY, cardRef, onOpen,
}) {
    const disabled = !item.enabled;
    // 停用或正文无效时该条目不进池，概率没有意义。
    const showProbability = !invalidReason;

    // 三种渲染态互斥：
    //   is-ready    正常飞入
    //   is-instant  记忆恢复，直接以落位态出现，不重播动画
    //   is-measuring 尚未测到位移，先按落位态占位
    let motionClass = ready ? 'is-ready' : 'is-measuring';
    if (instant && !leaving) motionClass = 'is-instant';

    return (
        <Box
            ref={cardRef}
            className={[
                'pv-card',
                `accent-${tone}`,
                disabled ? 'is-disabled' : '',
                invalidReason ? 'is-invalid' : '',
                leaving ? 'is-leaving' : '',
                motionClass,
            ].filter(Boolean).join(' ')}
            style={{
                // 动画延迟用 order（当前可见列表里的位次）。
                '--i': order,
                '--n': total,
                '--fly-x': `${flyX || 0}px`,
                '--fly-y': `${flyY || 0}px`,
            }}
            onClick={onOpen}
            role="button"
            tabIndex={0}
            aria-label={`打开 ${item.name || '未命名模板'}`}
            onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onOpen();
                }
            }}
        >
            {/* 穿透孔：与 slot 里的钉身同轴。 */}
            <span className="pv-card-hole" aria-hidden="true" />

            <Box className="pv-card-head">
                <span className="pv-card-no">{String(index + 1).padStart(2, '0')}</span>
                <Box className="pv-card-title-wrap">
                    <Typography component="h4" className="pv-card-name">
                        {item.name || '未命名模板'}
                    </Typography>
                </Box>
                <span className="pv-card-scope">{scopeLabel(item.scope)}</span>
                <span className="pv-card-dot" title={invalidReason || '这条卡正在参与抽样'} />
            </Box>

            <Box className="pv-card-body">
                <SectionBubbles text={item.text} />
            </Box>

            {/* 警示放在渐隐区之外：这条信息如果被 mask 淡掉就失去意义了。 */}
            {invalidReason && (
                <span className="pv-card-idle-reason">⚠ {invalidReason}</span>
            )}

            <Box className="pv-card-foot">
                {showProbability
                    ? <ProbabilityValue probability={probability} share={share} />
                    : <span className="pv-card-rate-main">—</span>}
                <span className="pv-card-open">打开 →</span>
            </Box>
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.PromptCard = PromptCard;
window.PromptComponents.SectionBubbles = SectionBubbles;
})();
