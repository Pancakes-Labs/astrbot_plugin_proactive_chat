(() => {
/**
 * 文件职责：提示词管理页的实木卡盒。
 *
 * 盒子平放在货架层板上；层板与木托由 CSS 在 .pv-shelf 上绘制，
 * 组件本身只负责盒体，不画架子。
 */

const { Box } = MaterialUI;

const SEGMENT_PALETTE = [
    '#6750A4', '#8B5CF6', '#EC4899', '#0F766E', '#2563EB',
    '#F59E0B', '#059669', '#DC2626', '#7C3AED', '#0891B2',
];

/** 能量光谱：把整池占比拼成一条彩色横条，只供抽样透视抽屉使用。 */
function ProbabilitySpectrum({ segments, tall }) {
    const list = Array.isArray(segments) ? segments : [];
    if (!list.length) {
        return <Box className={tall ? 'pv-spectrum-empty is-tall' : 'pv-spectrum-empty'} />;
    }
    return (
        <Box className={tall ? 'pv-spectrum is-tall' : 'pv-spectrum'}>
            {list.map((seg, index) => (
                <Box
                    key={seg.uiKey}
                    className="pv-spectrum-seg"
                    style={{
                        width: `${Math.max(seg.ratio * 100, 0.8)}%`,
                        '--pv-seg-color': SEGMENT_PALETTE[index % SEGMENT_PALETTE.length],
                    }}
                />
            ))}
        </Box>
    );
}

/**
 * 单个卡盒。
 *
 * remaining 由父层用 deck.total - 可见张数算出：父层是唯一状态源，
 * 盒子自己不知道卡牌飞到了哪里。
 */
function DeckBox({
    deck, open, closing, mouthRef, onClick, remaining,
}) {
    const tone = deck.tone;
    const total = deck.total;
    const empty = total === 0;
    const ratio = total > 0 ? deck.enabled / total : 0;
    // 盒口里实际有卡时才关掉转角柔化：空盒保留完整的开口渐变。
    const hasCards = Math.max(0, Number(remaining) || 0) > 0;
    const { CardStack } = window.PromptComponents;

    /* 键盘与点击都挂在盒面上：盒口区域要留给迷你卡的悬停，
       根节点若可点会把迷你卡的悬停一并吃掉。 */
    const faceProps = closing ? {} : {
        onClick,
        role: 'button',
        tabIndex: 0,
        'aria-pressed': open,
        'aria-label': `${deck.title}，共 ${total} 张，盒内剩余 ${remaining} 张`,
        onKeyDown: (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onClick();
            }
        },
    };

    return (
        /* 用 div + role 而非 button：盒内是多层 div 结构，
           button 只允许 phrasing content，嵌套块级元素在部分浏览器会被重排。 */
        <Box
            className={[
                'pv-box',
                `accent-${tone}`,
                open ? 'is-open' : '',
                closing ? 'is-closing' : '',
                empty ? 'is-empty' : '',
                hasCards ? 'has-cards' : '',
            ].filter(Boolean).join(' ')}
        >
            <Box className="pv-box-stage">
                <Box className="pv-box-floor" />

                {/* 盒口段层级自下而上（z 值见 prompts.css）：
                      pv-box-lip       包边实木底板，闭合整圈框      z:0
                      stage::after     外轮廓环，圆角整圈描边        z:1
                      pv-box-inner     盒内腔体，唯一的深色区域      z:1
                      pv-box-jamb      左右侧壁，自绘最外 1px 描边  z:2
                      pv-box-halo      悬停描边，压侧壁、在卡之下    z:2
                      peek-stack       迷你卡扇，盖过侧壁            z:3
                      pv-box-rim       盒口前挡板，自绘最外 1px 描边 z:4
                      pv-box-mouth     挡板内侧的接触阴影            z:5
                      pv-box-face      木纹正面                      z:6 */}
                <Box className="pv-box-lip" />
                <Box className="pv-box-inner" />
                <Box className="pv-box-jamb" />
                {/* 悬停描边：在卡扇之前绘制，同 z 值时卡片在其上。 */}
                <Box className="pv-box-halo" />
                <CardStack remaining={remaining} tone={tone} total={total} />
                <Box className="pv-box-rim" />
                <Box className="pv-box-mouth" ref={mouthRef} />

                <Box className="pv-box-face" {...faceProps}>
                    <Box className="pv-box-plate">
                        <span className="pv-box-emoji">{deck.emoji}</span>
                        <span className="pv-box-title">{deck.title}</span>
                        <span className="pv-box-figure">{total}</span>
                    </Box>

                    {/* 单色启用率进度：比彩虹光谱有信息量，也不会花。 */}
                    <Box
                        className="pv-box-meter"
                        title={`${deck.enabled} / ${total} 张已启用`}
                    >
                        <Box
                            className="pv-box-meter-fill"
                            style={{ width: `${Math.round(ratio * 100)}%` }}
                        />
                    </Box>
                </Box>
            </Box>
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.DeckBox = DeckBox;
window.PromptComponents.ProbabilitySpectrum = ProbabilitySpectrum;
window.PromptComponents.SEGMENT_PALETTE = SEGMENT_PALETTE;
})();
