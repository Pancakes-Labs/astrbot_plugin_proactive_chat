(() => {
/**
 * 文件职责：某个卡盒摊开后的卡牌飞行区。
 *
 * 只负责摆出卡片并让它们从盒口飞进来；搜索、凑整、新建、透视都在页头。
 *
 * 位移测量用 offsetLeft/offsetTop，每张卡都包在自己的 pv-card-slot 里，slot 顶部画一枚钉子。
 */

const { Box, Typography } = MaterialUI;
const { useMemo, useEffect, useRef, useCallback, useLayoutEffect, useState } = React;
const {
    invalidReason, matchKeyword, scopeLabel,
    CARD_RETRACT_DURATION, CARD_RETRACT_STEP, CARD_RETRACT_LEAD,
} = window.PromptUtils;
const { PromptCard } = window.PromptComponents;

/**
 * 沿 offsetParent 链累加到 stage，得到节点在 stage 坐标系里的左上角。
 */
function offsetWithin(node, stage) {
    let x = 0;
    let y = 0;
    let cur = node;
    while (cur && cur !== stage) {
        x += cur.offsetLeft;
        y += cur.offsetTop;
        cur = cur.offsetParent;
    }
    return { x, y };
}

function DeckSection({
    deck, closing, anchor, keyword, instant,
    onClosed, onAdd, onOpenCard,
}) {
    const fieldRef = useRef(null);
    // ui_key -> DOM 节点。用 Map 而不是数组，过滤后顺序变化也能对上。
    const cardRefs = useRef(new Map());
    // ui_key -> { x, y }：卡片中心指向盒口的位移。
    const [fly, setFly] = useState(null);
    // 记忆恢复：本次挂载不播放入场动画。只读一次，后续由父层状态决定。
    const instantRef = useRef(Boolean(instant));

    // 搜索只过滤卡面，不参与概率计算。
    // 同时带上原始序号：卡片钢印与模态序号必须同源。
    const rows = useMemo(() => {
        const all = deck.items.map((item, index) => ({ item, index }));
        if (!keyword || !keyword.trim()) return all;
        return all.filter(({ item }) => matchKeyword(item, keyword));
    }, [deck.items, keyword]);

    // 测量每张卡片中心到盒口的位移。全部在 stage 坐标系里做。
    const measure = useCallback(() => {
        const field = fieldRef.current;
        const stage = field ? field.offsetParent : null;
        if (!field || !stage || !anchor) return;
        const next = {};
        cardRefs.current.forEach((el, key) => {
            if (!el) return;
            const origin = offsetWithin(el, stage);
            const cx = origin.x + el.offsetWidth / 2;
            const cy = origin.y + el.offsetHeight / 2;
            next[key] = {
                x: Math.round(anchor.x - cx),
                y: Math.round(anchor.y - cy),
            };
        });
        setFly(next);
    }, [anchor]);

    // 门控：位移与锚点都就绪后才允许卡片播放飞行。
    const ready = Boolean(fly && anchor && Object.keys(fly).length >= rows.length);

    // 未 ready 时卡片在 CSS 里是 opacity: 0（is-measuring），必须先隐形。
    const [measureFailed, setMeasureFailed] = useState(false);
    useEffect(() => {
        if (ready) {
            setMeasureFailed(false);
            return undefined;
        }
        const frame = requestAnimationFrame(() => {
            const field = fieldRef.current;
            // 元素确实在布局里（有宽度）却仍然测不到位移，才判为失败。
            if (field && field.offsetWidth > 0 && !anchor) setMeasureFailed(true);
        });
        return () => cancelAnimationFrame(frame);
    }, [ready, anchor]);

    // 布局阶段同步测量：setState 在绘制前完成，首帧就是带位移的动画。
    useLayoutEffect(() => {
        measure();
    }, [measure, rows]);

    // 列数随宽度变化（auto-fill），位移必须重算，否则卡片会飞向错误方向。
    useEffect(() => {
        const field = fieldRef.current;
        if (!field || typeof ResizeObserver === 'undefined') return undefined;
        let frame = 0;
        const observer = new ResizeObserver(() => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(measure);
        });
        observer.observe(field);
        return () => {
            cancelAnimationFrame(frame);
            observer.disconnect();
        };
    }, [measure]);

    /**
     * 退场编排。
     *
     * 每张卡的 is-leaving 延迟由 CSS 用 lead + (--n - --i - 1) * step 倒序算出，
     * 这里只等总时长然后交差，lead 必须一起算进来。
     */
    const onClosedRef = useRef(onClosed);
    useEffect(() => { onClosedRef.current = onClosed; });

    useEffect(() => {
        if (!closing) return undefined;
        const count = rows.length;
        const total = count > 0
            ? CARD_RETRACT_LEAD + Math.max(0, count - 1) * CARD_RETRACT_STEP + CARD_RETRACT_DURATION
            : 160;
        const timer = setTimeout(() => {
            if (onClosedRef.current) onClosedRef.current();
        }, total + 40);
        return () => clearTimeout(timer);
    }, [closing, rows.length]);

    return (
        <Box className="pv-field" ref={fieldRef}>
            {/* 挂钩不在这里画 */}

            {rows.length === 0 ? (
                <Box className="pv-empty">
                    <Box className="pv-empty-icon">{keyword ? '🔍' : '📭'}</Box>
                    <Typography variant="body2">
                        {keyword ? '没有匹配的卡牌。' : '这个卡盒还是空的。'}
                    </Typography>
                    {!keyword && (
                        <Box className="pv-empty-action" onClick={onAdd} role="button" tabIndex={0}
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                    event.preventDefault();
                                    onAdd();
                                }
                            }}
                        >
                            + 新建{scopeLabel(deck.scope)}卡牌
                        </Box>
                    )}
                </Box>
            ) : (
                <Box className={`pv-card-grid${closing ? ' is-leaving' : ''}`}>
                    {rows.map(({ item, index }, order) => (
                        <Box
                            className="pv-card-slot"
                            key={item.__ui_key}
                            // 投影画在 slot 上，退场延迟必须与卡片同源。
                            style={{ '--i': order, '--n': rows.length }}
                        >
                            <PromptCard
                                // ref 挂在卡片本身：位移与模态出生点都要它的节点。
                                cardRef={(el) => {
                                    if (el) cardRefs.current.set(item.__ui_key, el);
                                    else cardRefs.current.delete(item.__ui_key);
                                }}
                                item={item}
                                // index 是盒内原始序号（钢印用），order 是可见位次（动画用）。
                                index={index}
                                order={order}
                                total={rows.length}
                                tone={deck.tone}
                                probability={deck.prob[item.__ui_key]}
                                share={(deck.share || {})[item.__ui_key]}
                                invalidReason={invalidReason(item)}
                                leaving={closing}
                                ready={ready}
                                instant={instantRef.current || measureFailed}
                                flyX={fly ? (fly[item.__ui_key] || {}).x : 0}
                                flyY={fly ? (fly[item.__ui_key] || {}).y : 0}
                                onOpen={() => onOpenCard(item, cardRefs.current.get(item.__ui_key))}
                            />
                        </Box>
                    ))}

                    {/* 新建卡位也进 slot，但它既没有孔也不占用挂钩：
                        挂钩层只按 rows 渲染，空位那格墙上本来就没挂东西。 */}
                    <Box className="pv-card-slot is-ghost">
                        <Box
                            className="pv-card pv-card-ghost"
                            role="button"
                            tabIndex={0}
                            aria-label={`新建${scopeLabel(deck.scope)}卡牌`}
                            onClick={onAdd}
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                    event.preventDefault();
                                    onAdd();
                                }
                            }}
                        >
                            <span className="pv-card-ghost-plus">＋</span>
                            <span className="pv-card-ghost-text">新建卡牌</span>
                            <span className="pv-card-ghost-scope">{scopeLabel(deck.scope)}</span>
                        </Box>
                    </Box>
                </Box>
            )}
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.DeckSection = DeckSection;
})();
