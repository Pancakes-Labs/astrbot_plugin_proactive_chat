(() => {
/**
 * 文件职责：会话卡片共享 UI 组件。
 *
 * 状态页的计时器卡片与任务页的调度卡片结构高度一致（身份行 / 环形倒计时 /
 * 配置事实行），这里把可复用的表现层组件收敛到一处，避免两份实现各自漂移。
 *
 * 组件只负责渲染，不关心计时语义：文案与百分比由调用方传入。
 */

const { Typography } = MaterialUI;

// 环形进度半径与周长，SVG 与样式表共用同一份数值口径。
const RING_RADIUS = 26;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

// UMO 复制：优先用 clipboard API，非安全上下文退化为 textarea 选区复制。
async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return;
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    // execCommand 失败时只返回 false 而不抛错，必须主动转成异常才能提示用户。
    const succeeded = document.execCommand('copy');
    document.body.removeChild(area);
    if (!succeeded) {
        throw new Error('浏览器拒绝执行复制命令');
    }
}

// 卡片顶部浮层提示：key 由调用方绑定重置序号，保证连续触发时重播动画。
function TimerCardHint({ show, token, text }) {
    if (!show) return null;
    return (
        <div className="tcard-hint" key={`tcard-hint-${token}`}>
            <span className="tcard-hint-dot"></span>
            {text}
        </div>
    );
}

// 身份行：会话名 + 可复制 UMO + 右上角徽标组。
function TimerCardIdentity({ name, umoText, badges }) {
    const [copied, setCopied] = React.useState(false);
    const copiedTimerRef = React.useRef(null);

    React.useEffect(() => {
        return () => {
            if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
        };
    }, []);

    const handleCopy = async () => {
        const text = String(umoText || '');
        if (!text || text === '--') return;
        try {
            await copyText(text);
            setCopied(true);
            if (window.Toast) {
                window.Toast.success('UMO 已复制到剪贴板', { title: name });
            }
            if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
            copiedTimerRef.current = setTimeout(() => setCopied(false), 1800);
        } catch (e) {
            if (window.Toast) {
                window.Toast.error('复制失败，请手动选中 UMO 文本复制');
            }
        }
    };

    return (
        <div className="tcard-head">
            <div className="tcard-identity">
                <Typography variant="body1" className="tcard-name">
                    {name}
                </Typography>
                <button
                    type="button"
                    className={`tcard-umo ${copied ? 'is-copied' : ''}`}
                    onClick={handleCopy}
                    title={`点击复制 UMO\n${umoText}`}
                >
                    <span className="tcard-umo-label">UMO</span>
                    <span className="tcard-umo-value">{umoText}</span>
                    <span className="tcard-umo-action" aria-hidden="true">{copied ? '已复制' : '复制'}</span>
                </button>
            </div>
            <div className="tcard-badges">
                {badges}
            </div>
        </div>
    );
}

// 环形进度 + hero 倒计时：数字放大、单位缩小，脚注承载时间信息。
function TimerCardCountdown({ percent, status, kicker, tokens, emptyText, ringTitle, foot }) {
    const ringOffset = RING_CIRCUMFERENCE * (1 - percent / 100);

    return (
        <div className="tcard-body">
            <div className="tcard-ring" title={ringTitle}>
                <svg viewBox="0 0 64 64" className="tcard-ring-svg" aria-hidden="true">
                    <circle className="tcard-ring-track" cx="32" cy="32" r={RING_RADIUS} />
                    <circle
                        className={`tcard-ring-bar is-${status}`}
                        cx="32"
                        cy="32"
                        r={RING_RADIUS}
                        style={{
                            '--ring-circumference': `${RING_CIRCUMFERENCE}`,
                            '--ring-offset': `${ringOffset}`,
                        }}
                    />
                </svg>
                <span className="tcard-ring-value">
                    {percent}
                    <small>%</small>
                </span>
            </div>

            <div className="tcard-body-main">
                <span className="tcard-kicker">{kicker}</span>
                <div className="tcard-countdown">
                    {tokens && tokens.length > 0 ? (
                        tokens.map((token, index) => (
                            <React.Fragment key={`token-${index}`}>
                                <span className="tcard-countdown-num">{token.value}</span>
                                <span className="tcard-countdown-unit">{token.unit}</span>
                            </React.Fragment>
                        ))
                    ) : (
                        <span className="tcard-countdown-empty">{emptyText}</span>
                    )}
                </div>
                <div className="tcard-foot">{foot}</div>
            </div>
        </div>
    );
}

// 配置事实行：dt 为配置项名，dd 为取值与补充说明。
function TimerCardFacts({ facts }) {
    if (!Array.isArray(facts) || facts.length === 0) return null;
    return (
        <dl className="tcard-facts">
            {facts.map((fact, index) => (
                <div
                    className={`tcard-fact ${fact.warning ? 'is-warning' : ''}`}
                    key={`fact-${index}`}
                >
                    <dt>{fact.label}</dt>
                    <dd title={fact.title}>
                        {fact.personaState ? (
                            <span className={`tcard-fact-persona is-${fact.personaState}`}>
                                {fact.value}
                            </span>
                        ) : (
                            fact.value
                        )}
                        {fact.sub ? <span className="tcard-fact-sub">{fact.sub}</span> : null}
                    </dd>
                </div>
            ))}
        </dl>
    );
}

// 脚注片段：统一「预计 xxx：时间 · 已等待时长」的拼接口径。
function TimerCardFootItem({ label, value, title }) {
    return (
        <span className="tcard-foot-item" title={title}>
            {label}
            {value ? <span className="tcard-foot-time">{value}</span> : null}
        </span>
    );
}

// 操作区：反馈文案栈 + 等宽按钮行，任务页与状态页共用同一套结构。
// feedbacks 中 text 为空的项会被过滤，空态完全不占高度。
function TimerCardActions({ feedbacks, buttons }) {
    if (!buttons) return null;
    const visible = (Array.isArray(feedbacks) ? feedbacks : []).filter(
        (item) => item && item.text
    );

    return (
        <div className="tcard-actions">
            {visible.length > 0 ? (
                <div className="tcard-feedback-stack">
                    {visible.map((item, index) => (
                        <div
                            className={`tcard-feedback ${item.status ? `is-${item.status}` : ''}`}
                            key={`feedback-${index}`}
                        >
                            {item.text}
                        </div>
                    ))}
                </div>
            ) : null}
            <div className="tcard-button-row">{buttons}</div>
        </div>
    );
}

window.TimerCardUI = Object.freeze({
    RING_RADIUS: RING_RADIUS,
    RING_CIRCUMFERENCE: RING_CIRCUMFERENCE,
    TimerCardHint: TimerCardHint,
    TimerCardIdentity: TimerCardIdentity,
    TimerCardCountdown: TimerCardCountdown,
    TimerCardFacts: TimerCardFacts,
    TimerCardFootItem: TimerCardFootItem,
    TimerCardActions: TimerCardActions,
});
})();
