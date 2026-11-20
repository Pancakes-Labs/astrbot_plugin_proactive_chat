(() => {
/**
 * 文件职责：卡池透视（右侧档案抽屉）。
 *
 * 它回答三个具体问题，而不是堆数据：
 *   1. 这个池子里有谁（含并入的通用卡）
 *   2. 谁最容易中、谁几乎抽不到
 *   3. 改完权重真的生效了吗（抽样实测对照）
 */

const {
    Box, Typography, Button, LinearProgress, IconButton,
} = MaterialUI;
const { useMemo } = React;
const { formatPercent, DECK_ORDER, DECK_META } = window.PromptUtils;
const { ProbabilitySpectrum, SEGMENT_PALETTE } = window.PromptComponents;

// 判定几乎抽不到的门槛：约 20 轮才轮到一次。
const RARE_RATIO = 0.05;

function sortedSegments(deck) {
    return (deck.poolSegments || deck.segments || [])
        .slice()
        .sort((a, b) => b.ratio - a.ratio);
}

/** 概览 KPI：池内张数 / 有效权重 / 最大占比。 */
function OverviewBlock({ deck, rows }) {
    const weightTotal = rows.reduce((sum, row) => sum + (Number(row.weight) || 0), 0);
    const top = rows[0];

    return (
        <Box className="pv-pool-kpi">
            <Box className="pv-pool-kpi-cell">
                <span className="pv-pool-kpi-value">{rows.length}</span>
                <span className="pv-pool-kpi-label">池内可抽</span>
            </Box>
            <Box className="pv-pool-kpi-cell">
                <span className="pv-pool-kpi-value">{deck.total}</span>
                <span className="pv-pool-kpi-label">盒内持有</span>
            </Box>
            <Box className="pv-pool-kpi-cell">
                <span className="pv-pool-kpi-value">{weightTotal}</span>
                <span className="pv-pool-kpi-label">有效权重</span>
            </Box>
            <Box className="pv-pool-kpi-cell">
                <span className="pv-pool-kpi-value">
                    {top ? formatPercent(top.ratio) : '—'}
                </span>
                <span className="pv-pool-kpi-label">最高占比</span>
            </Box>
        </Box>
    );
}

// 偏差超过该阈值就标黄：2000 轮下正常统计波动约 1pt 量级。
const DRIFT_WARN = 0.02;

/**
 * 抽样对照：理论轨道叠实际轨道。
 */
function SamplingBars({ result }) {
    return (
        <Box className="pv-dual">
            {result.message && (
                <Typography variant="caption" color="warning.main" sx={{ display: 'block', mb: 0.5 }}>
                    {result.message}
                </Typography>
            )}
            {(result.results || []).map((row, index) => {
                const drift = Math.abs((row.actual || 0) - (row.expected || 0));
                const warn = drift >= DRIFT_WARN;
                return (
                    <Box className="pv-dual-row" key={`${row.id}-${index}`}>
                        <span className="pv-dual-name">{row.name || '未命名'}</span>
                        <Box className="pv-dual-track">
                            <Box
                                className="pv-dual-theory"
                                sx={{ width: `${Math.max((row.expected || 0) * 100, 0.5)}%` }}
                            />
                            <Box
                                className="pv-dual-actual"
                                style={{ '--pv-accent': SEGMENT_PALETTE[index % SEGMENT_PALETTE.length] }}
                                sx={{ width: `${Math.max((row.actual || 0) * 100, 0.5)}%` }}
                            />
                        </Box>
                        <span className={`pv-dual-num${warn ? ' is-warn' : ''}`}>
                            {formatPercent(row.actual)}
                            <small>
                                期望 {formatPercent(row.expected)} · 偏差 {(drift * 100).toFixed(1)}pt
                            </small>
                        </span>
                    </Box>
                );
            })}
        </Box>
    );
}

/**
 * 诊断结论。
 *
 * 只说能落到具体条目上的事实，不说空话。
 * 没有任何条目时整块隐藏，不占高度。
 */
function DiagnosisBlock({ deck, rows, dirty }) {
    const notes = useMemo(() => {
        const list = [];
        if (deck.total > deck.valid) {
            list.push({
                tone: 'warn',
                text: `盒内有 ${deck.total - deck.valid} 张不会参与抽样（已停用或正文为空）`,
            });
        }
        const rare = rows.filter((row) => row.ratio > 0 && row.ratio < RARE_RATIO);
        if (rare.length) {
            list.push({
                tone: 'warn',
                text: `「${rare[0].name}」等 ${rare.length} 张占比低于 ${Math.round(RARE_RATIO * 100)}%，平均 20 轮以上才轮到一次`,
            });
        }
        if (rows.length === 1) {
            list.push({ tone: 'info', text: '池内只有 1 张可用卡，抽签结果恒定' });
        }
        if (rows.length === 0) {
            list.push({
                tone: 'warn',
                text: '池内没有可抽模板，将仅使用内置场景层与约束层',
            });
        }
        if (dirty) {
            list.push({ tone: 'info', text: '存在未保存改动，以上按草稿计算' });
        }
        return list;
    }, [deck, rows, dirty]);

    if (!notes.length) return null;

    return (
        <Box className="pv-pool-notes">
            {notes.map((note, index) => (
                <Box className={`pv-pool-note is-${note.tone}`} key={`note-${index}`}>
                    <span className="pv-pool-note-dot" />
                    {note.text}
                </Box>
            ))}
        </Box>
    );
}

/**
 * 三池对比：还没打开任何卡盒时抽屉里的默认视图。
 *
 * 只并排三条占总量的光谱与关键读数，不列明细 —— 明细点标签进单池就有。
 */
function CompareAll({ decks, onSwitch }) {
    return (
        <Box className="pv-pool-compare">
            {DECK_ORDER.map((key) => {
                const deck = decks[key];
                const rows = sortedSegments(deck);
                const top = rows[0];
                return (
                    <Box className="pv-pool-compare-row" key={key}>
                        <button
                            type="button"
                            className="pv-pool-compare-head"
                            onClick={() => onSwitch(key)}
                        >
                            <span className="pv-pool-compare-name">
                                {deck.emoji} {deck.title}
                            </span>
                            <span className="pv-pool-compare-meta">
                                {rows.length} 张可抽 · {deck.enabled}/{deck.total} 启用
                            </span>
                        </button>
                        <ProbabilitySpectrum segments={deck.poolSegments || deck.segments} />
                        <span className="pv-pool-compare-top">
                            {top ? `最易中：${top.name} ${formatPercent(top.ratio)}` : '没有可抽卡'}
                        </span>
                    </Box>
                );
            })}
        </Box>
    );
}

function PoolDrawer({
    open, deckKey, decks, dirty,
    simulateResult, simulating, onSimulate, onSwitch, onClose,
}) {
    const deck = deckKey ? decks[deckKey] : null;
    const rows = deck ? sortedSegments(deck) : [];
    const simTarget = deckKey && deckKey !== 'any' ? deckKey : '';

    return (
        <Box className={`pv-pool-layer${open ? ' is-open' : ''}`} aria-hidden={!open}>
            <Box className="pv-pool-mask" onClick={onClose} />

            <Box className="pv-pool-drawer" role="dialog" aria-modal="false" aria-label="卡池透视">
                {/* 档案标签：当前池的标签前移照亮，点其他标签换池不关抽屉。 */}
                <Box className="pv-pool-tabs">
                    {DECK_ORDER.map((key) => (
                        <button
                            type="button"
                            key={key}
                            className={`pv-pool-tab${key === deckKey ? ' is-active' : ''}`}
                            onClick={() => onSwitch(key)}
                        >
                            {DECK_META[key].short}
                        </button>
                    ))}
                    <Box sx={{ flex: 1 }} />
                    <IconButton size="small" onClick={onClose} aria-label="关闭卡池透视">
                        <Typography component="span" sx={{ fontSize: '0.8rem' }}>✕</Typography>
                    </IconButton>
                </Box>

                <Box className="pv-pool-head">
                    <Typography component="h3" className="pv-pool-title">卡池透视</Typography>
                    <Typography variant="body2" className="pv-pool-sub">
                        {deck ? `${deck.title}这一池，谁会先开口` : '三个池子的抽样结构对比'}
                    </Typography>
                </Box>

                <Box className="pv-pool-body">
                    {!deck ? (
                        <CompareAll decks={decks} onSwitch={onSwitch} />
                    ) : (
                        <React.Fragment>
                            <OverviewBlock deck={deck} rows={rows} />

                            <Box className="pv-pool-block">
                                <Typography component="h4" className="pv-pool-block-title">理论占比</Typography>
                                <ProbabilitySpectrum segments={deck.poolSegments || deck.segments} tall />
                                <Typography variant="caption" className="pv-pool-hint">
                                    {deck.key === 'any'
                                        ? '通用卡没有独立候选池，这里按盒内权重占比展示。'
                                        : `真实候选池 ${rows.length} 套，已含并入本池的通用卡。`}
                                </Typography>
                                {rows.length > 0 && (
                                    <Box className="pv-legend">
                                        {rows.map((row, index) => (
                                            <Box key={row.uiKey} className="pv-legend-row">
                                                <Box
                                                    className="pv-legend-dot"
                                                    style={{ '--pv-seg-color': SEGMENT_PALETTE[index % SEGMENT_PALETTE.length] }}
                                                />
                                                <span className="pv-legend-name">{row.name}</span>
                                                <span className="pv-legend-pct">{formatPercent(row.ratio)}</span>
                                            </Box>
                                        ))}
                                    </Box>
                                )}
                            </Box>

                            <Box className="pv-pool-block">
                                <Box className="pv-pool-block-head">
                                    <Typography component="h4" className="pv-pool-block-title">抽样对照</Typography>
                                    <Button
                                        size="small"
                                        variant="outlined"
                                        onClick={() => onSimulate(simTarget)}
                                        disabled={simulating || !simTarget || rows.length === 0}
                                    >
                                        {simulating ? '抽取中…' : '跑 2000 次'}
                                    </Button>
                                </Box>
                                {simulating && <LinearProgress sx={{ mt: 1 }} />}
                                {simulateResult ? (
                                    <SamplingBars result={simulateResult} />
                                ) : (
                                    <Typography variant="caption" className="pv-pool-hint">
                                        {deck.key === 'any'
                                            ? '通用卡没有独立池，无法单独预演。'
                                            : '跑一次真实加权抽取，对照实际分布与期望值。'}
                                    </Typography>
                                )}
                            </Box>

                            <DiagnosisBlock deck={deck} rows={rows} dirty={dirty} />
                        </React.Fragment>
                    )}
                </Box>
            </Box>
        </Box>
    );
}

window.PromptComponents = window.PromptComponents || {};
window.PromptComponents.PoolDrawer = PoolDrawer;
})();
