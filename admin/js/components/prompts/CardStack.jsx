(() => {
/**
 * 文件职责：卡盒盒口里摊开的迷你卡扇。
 *
 * 固定渲染 PEEK_SLOTS 个槽位，用 class 切换可见性，而不是按数量动态挂载节点：
 * 张数减少时槽位是「沉回盒里」，不是节点被直接删掉。
 *
 * 扇形几何由本组件按张数反解后注入 CSS：
 *   --pv-peek-card-w  卡宽（相对开口宽度的百分比）
 *   --pv-peek-step    相邻卡中心距（单位：卡宽）
 *   --pv-peek-half    可见中心 = (count-1)/2，整扇以此居中
 *   --pv-peek-count   可见张数，供明暗梯度与离场节奏使用
 * 卡宽与步进保证最外两张卡的外缘贴住开口内沿，所以 1~8 张都能铺满盒口。
 * total 只用于盒口空着时兜底几何，不参与可见张数的计算。
 */

const { Box } = MaterialUI;
const { PEEK_SLOTS } = window.PromptUtils;

// 相邻卡中心距的目标比例（单位：卡宽）。
const SPREAD = 0.62;
// 卡宽区间：下限保证 8 张时不比原来更窄，上限避免少张时读成宽纸牌。
const CARD_W_MIN = 19;
const CARD_W_MAX = 24;
// 步进上限：1~2 张时避免摊得过散。
const STEP_MAX = 1.6;

/** 按张数反解扇形几何：卡宽先按刚好铺满求并夹到区间内，步进再由剩余空隙反解。 */
function geomOf(n) {
    const spreadWidth = n > 1 ? 100 / (1 + (n - 1) * SPREAD) : CARD_W_MAX;
    const cardW = Math.min(CARD_W_MAX, Math.max(CARD_W_MIN, spreadWidth));
    const step = n > 1
        ? Math.min(STEP_MAX, (100 - cardW) / ((n - 1) * cardW))
        : 0;
    return { half: (n - 1) / 2, cardW, step, count: n };
}

function CardStack({ remaining, tone, max, total }) {
    const { useRef } = React;
    const slots = max || PEEK_SLOTS;
    const count = Math.max(0, Math.min(Number(remaining) || 0, slots));

    // 盒口空着时（卡都在墙上）按盒内总数反解的几何兜底。
    const emptyCount = Math.min(slots, Math.max(1, Number(total) || slots));

    // count 归零时冻结上一次的几何，离场期间只做垂直下沉。
    const frozenRef = useRef(null);
    if (count > 0) frozenRef.current = geomOf(count);
    const geo = frozenRef.current || geomOf(emptyCount);

    return (
        <Box
            className={`pv-peek-stack accent-${tone}`}
            style={{
                '--pv-peek-half': geo.half,
                '--pv-peek-count': geo.count,
                '--pv-peek-card-w': `${geo.cardW}%`,
                '--pv-peek-step': geo.step,
            }}
        >
            {Array.from({ length: slots }, (unused, k) => (
                <Box
                    key={k}
                    className={`pv-peek${k >= count ? ' is-away' : ''}`}
                    style={{ '--k': k, '--p': k }}
                    aria-hidden="true"
                >
                    <span className="pv-peek-hole" />
                    <span className="pv-peek-no" />
                    <span className="pv-peek-dot" />
                    <span className="pv-peek-sec is-intent" />
                    <span className="pv-peek-sec is-final" />
                    <span className="pv-peek-foot" />
                </Box>
            ))}
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.CardStack = CardStack;
})();
