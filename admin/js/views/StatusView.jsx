(() => {
/**
 * 文件职责：状态页视图，负责服务状态、计时器可视化与手动刷新交互展示。
 */

 const { Box, Typography, Button } = MaterialUI;

function StatusMetricRow({ label, value, emphasize = false, status = '' }) {
    return (
        <div className="status-metric-row">
            <Typography variant="body2" className="status-metric-label">
                {label}
            </Typography>
            <Typography
                variant="body2"
                className={`status-metric-value ${emphasize ? 'is-emphasize' : ''} ${status ? `is-${status}` : ''}`}
            >
                {value}
            </Typography>
        </div>
    );
}

function StatusTimerCard({ timer, displayTimezone, nowMs, snapshotAtMs, snapshotServerMs, resetHint, resetHintToken, actions }) {
    // 派生信息统一交给领域函数计算，视图只负责结构拼装。
    const meta = React.useMemo(
        () => window.ProgressDomain.resolveStatusTimerCard(
            timer, nowMs, displayTimezone, snapshotAtMs, snapshotServerMs
        ),
        [timer, nowMs, displayTimezone, snapshotAtMs, snapshotServerMs]
    );
    const UI = window.TimerCardUI;

    return (
        <div className={`card tcard ${meta.accentClass} ${meta.status === 'urgent' ? 'is-urgent' : ''} ${meta.status === 'expired' ? 'is-expired' : ''} ${resetHint ? 'is-resetting' : ''}`}>
            <UI.TimerCardHint
                show={resetHint}
                token={resetHintToken}
                text="群聊刚刚有新消息，沉默计时器已重新开始计时"
            />

            <UI.TimerCardIdentity
                name={meta.sessionDisplayName}
                umoText={meta.umoText}
                badges={(
                    <React.Fragment>
                        <span className={`tcard-badge ${meta.accentClass}`}>{meta.kindBadgeLabel}</span>
                        <span className={`task-status-pill is-${meta.status}`}>{meta.statusLabel}</span>
                    </React.Fragment>
                )}
            />

            <UI.TimerCardCountdown
                percent={meta.progressPercent}
                status={meta.status}
                kicker={meta.heroLabel}
                tokens={meta.countdownTokens}
                emptyText={meta.statusLabel}
                ringTitle={`${meta.progressLabel}，已等待 ${meta.elapsedText}`}
                foot={(
                    <React.Fragment>
                        <UI.TimerCardFootItem
                            label="预计创建时间："
                            value={meta.targetClockText}
                            title={`目标时间 ${meta.targetText}`}
                        />
                        <span className="tcard-foot-dot">·</span>
                        <UI.TimerCardFootItem label={`已等待 ${meta.elapsedText}`} />
                    </React.Fragment>
                )}
            />

            <UI.TimerCardFacts
                facts={[
                    {
                        label: '生效人格',
                        value: meta.personaText,
                        sub: meta.personaSourceText,
                        title: meta.personaTitle,
                        personaState: meta.personaState,
                    },
                    { label: '上下文来源', value: meta.sourceModeLabel },
                    { label: meta.triggerConfigLabel, value: meta.triggerConfigValue },
                    {
                        label: '未回复次数',
                        value: meta.unansweredValueText,
                        sub: meta.unansweredIsLimited
                            ? `上限达到 ${meta.max_unanswered_times} 次后暂停`
                            : '未设上限',
                        title: meta.unansweredHintText,
                        warning: meta.unansweredIsWarning,
                    },
                ]}
            />

            {actions}
        </div>
    );
}

function StatusView({ onRefresh }) {
    const { state } = useAppContext();
    const api = useApi();
    const status = state.status || {};
    // 全局共享时钟，避免每个视图各起一个每秒 interval。
    const nowMs = useNow();
    // resetHintMap 记录哪些群沉默卡片当前需要显示“刚刚被重置”的短暂提示。
    // 值从 true 改为递增序号，让连续重置能触发 DOM 重建并重播动画。
    const [resetHintMap, setResetHintMap] = React.useState({});
    const resetHintSeqRef = React.useRef(0);
    // 手动刷新按钮的视觉反馈状态机：idle -> loading -> success / error。
    const [manualRefreshState, setManualRefreshState] = React.useState({ phase: 'idle', finishedAt: 0 });
    // 运维快捷操作状态机：reload 重载插件、restart 重启 AstrBot。
    const [reloadState, setReloadState] = React.useState({ phase: 'idle' });
    const [restartState, setRestartState] = React.useState({ phase: 'idle' });
    // 计时器卡片操作反馈：按“类型-会话”记录，与任务页的 job.id 键互不干扰。
    const timerFeedback = useFeedbackMap(3200);
    const previousTimerSnapshotRef = React.useRef({});
    const resetHintTimersRef = React.useRef({});
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';

    React.useEffect(() => {
        return () => {
            // 组件卸载时清理所有“重置提示”的超时器，避免状态在卸载后回写。
            Object.values(resetHintTimersRef.current).forEach((timerId) => {
                clearTimeout(timerId);
            });
        };
    }, []);

    // 状态快照的本地到达时刻：后端只给整秒值，需要配合本地时钟继续递增。
    const snapshotAtMs = Number(state.statusReceivedAt !== undefined ? state.statusReceivedAt : 0);
    // 后端构建快照的服务器时刻（秒）：用于补偿构建与传输耗时，修正刷新后凭空少几秒的问题。
    const snapshotServerMs = window.TimeDomain
        ? window.TimeDomain.toEpochMs(status.timer_snapshot_at)
        : null;
    // 统一把后端状态字段规整为前端友好的基础数字类型。
    const uptimeSeconds = Number(status.uptime_seconds !== undefined ? status.uptime_seconds : 0);
    // 运行时长在两次快照之间按本地流逝时间补足，避免整秒值半天不动。
    const liveUptimeSeconds = uptimeSeconds + (
        snapshotAtMs > 0 && nowMs > snapshotAtMs
            ? Math.max(0, Math.floor((nowMs - snapshotAtMs) / 1000))
            : 0
    );
    const jobsCount = Number(status.jobs_count !== undefined ? status.jobs_count : 0);
    const sessionsCount = Number(status.sessions_count !== undefined ? status.sessions_count : 0);
    const autoTriggerTimers = Number(status.auto_trigger_timers !== undefined ? status.auto_trigger_timers : 0);
    const groupTimers = Number(status.group_timers !== undefined ? status.group_timers : 0);
    const schedulerRunning = Boolean(status.scheduler_running);
    const pluginRunning = Boolean(status.running);
    // 合并并去重所有会话计时器，保证一个会话最多出现一张最关键卡片。
    // 依赖状态对象上的原始数组字段，缺失时也不会因空数组字面量导致每次重算。
    const timerCards = React.useMemo(
        () => window.ProgressDomain.dedupeTimerCards([
            ...(Array.isArray(status.group_timer_cards) ? status.group_timer_cards : []),
            ...(Array.isArray(status.auto_trigger_cards) ? status.auto_trigger_cards : []),
        ]),
        [status.group_timer_cards, status.auto_trigger_cards]
    );

    const timerSections = React.useMemo(
        () => [
            {
                key: 'group_silence',
                title: '群沉默计时器',
                description: '群内一旦有新消息就会重新计时，适合观察沉默窗口是否被活跃消息重置。',
                emptyText: '当前没有正在运行的群沉默计时器。',
                cards: timerCards.filter((timer) => timer.timer_kind === 'group_silence'),
            },
            {
                key: 'auto_trigger',
                title: '自动触发计时器',
                description: '用于观察会话的自动触发状况，并按群聊 / 私聊显示不同标签。通常情况下，插件运行一段时间后，这里将不会出现新的卡片。',
                emptyText: '当前没有正在运行的自动触发计时器。',
                cards: timerCards.filter((timer) => timer.timer_kind === 'auto_trigger'),
            },
        ],
        [timerCards]
    );

    // 依赖计时器原始字段摘要，值不变时不会重复执行状态写入。
    const timerSnapshotKey = timerCards
        .map((timer) => `${timer.timer_kind}-${timer.session_id}-${timer.remaining_seconds}-${timer.target_time}`)
        .join('|');

    React.useEffect(() => {
        const nextSnapshot = {};
        const nextHints = {};
        const activeKeys = new Set();
        let hintChanged = false;

        timerCards.forEach((timer) => {
            const key = `${timer.timer_kind}-${timer.session_id}`;
            // 剩余秒数缺失时保持 null，避免 Number(null) 变成 0 影响重置判定。
            const rawRemaining = timer.remaining_seconds;
            const currentRemaining = (rawRemaining === null || rawRemaining === undefined || rawRemaining === '')
                ? null
                : Math.max(0, Number(rawRemaining));
            const currentTarget = Number(timer.target_time !== undefined ? timer.target_time : 0);
            const currentStatus = String(timer.status || '');
            const previous = previousTimerSnapshotRef.current[key] || null;

            activeKeys.add(key);
            nextSnapshot[key] = {
                remainingSeconds: currentRemaining,
                targetTime: currentTarget,
                status: currentStatus,
            };

            // 判定“被重置”优先看目标时间是否后移：群消息会让沉默窗口整体往后推。
            // 用剩余秒数做差会漏判——若距上次快照恰好过去同样时间，差值可能接近 0。
            const previousRemaining = previous ? Number(previous.remainingSeconds) : NaN;
            const previousTarget = previous ? Number(previous.targetTime) : NaN;
            const targetAdvanced = Number.isFinite(previousTarget)
                && Number.isFinite(currentTarget)
                && currentTarget - previousTarget > 2;
            const remainingJumped = Number.isFinite(previousRemaining)
                && currentRemaining !== null
                && currentRemaining - previousRemaining > 3;

            const isGroupReset = timer.timer_kind === 'group_silence'
                && previous
                && currentStatus !== 'expired'
                && currentStatus !== 'unknown'
                && currentRemaining !== null
                && (targetAdvanced || remainingJumped);

            if (isGroupReset) {
                // 递增序号保证即使提示正在显示中，节点也会重建从而重播动画。
                resetHintSeqRef.current += 1;
                nextHints[key] = resetHintSeqRef.current;
                hintChanged = true;
                if (resetHintTimersRef.current[key]) {
                    clearTimeout(resetHintTimersRef.current[key]);
                }
                resetHintTimersRef.current[key] = setTimeout(() => {
                    setResetHintMap((current) => {
                        if (!current[key]) return current;
                        const updated = Object.assign({}, current);
                        delete updated[key];
                        return updated;
                    });
                    delete resetHintTimersRef.current[key];
                }, 4200);
            }
        });

        Object.keys(resetHintTimersRef.current).forEach((key) => {
            if (!activeKeys.has(key)) {
                clearTimeout(resetHintTimersRef.current[key]);
                delete resetHintTimersRef.current[key];
            }
        });

        previousTimerSnapshotRef.current = nextSnapshot;

        // 只有提示集合确实需要变化时才 setState，避免“渲染 -> effect -> setState”自激循环。
        setResetHintMap((current) => {
            const filteredEntries = Object.entries(current).filter(([key]) => activeKeys.has(key));
            const filtered = Object.fromEntries(filteredEntries);
            if (!hintChanged) {
                if (filteredEntries.length === Object.keys(current).length) {
                    return current;
                }
                return filtered;
            }
            const merged = Object.assign({}, filtered, nextHints);
            // 序号相同说明内容没变，返回原对象以跳过无意义重渲染。
            const isSame = Object.keys(merged).length === Object.keys(current).length
                && Object.keys(merged).every((key) => merged[key] === current[key]);
            return isSame ? current : merged;
        });
    }, [timerSnapshotKey]);

    // 重载插件：等价于 /主动消息重启。重载期间控制台会短暂断开，属预期行为。
    const handleReloadPlugin = async () => {
        if (reloadState.phase === 'loading') return;
        const confirmed = window.confirm(
            '确定要重载主动消息插件吗？\n重载期间 Web 管理端会短暂断开连接，稍后自动恢复。'
        );
        if (!confirmed) return;

        setReloadState({ phase: 'loading' });
        try {
            const result = await api.reloadPlugin();
            setReloadState({ phase: 'success' });
            if (window.Toast) {
                window.Toast.success(
                    (result && result.message) || '已提交重载请求，插件正在重启',
                    { title: '重载插件' }
                );
            }
        } catch (e) {
            setReloadState({ phase: 'error' });
            const message = e && e.message ? e.message : '重载插件失败，请查看服务端日志';
            if (window.Toast) window.Toast.error(message, { title: '重载插件失败' });
        }
    };

    // 重启 AstrBot：等价于 /重启AstrBot。进程会在数秒后整体重启。
    const handleRestartAstrbot = async () => {
        if (restartState.phase === 'loading') return;
        const confirmed = window.confirm(
            '确定要重启整个 AstrBot 进程吗？\n所有插件与平台连接都会中断，进程将在数秒后重新启动。'
        );
        if (!confirmed) return;

        setRestartState({ phase: 'loading' });
        try {
            const result = await api.restartAstrbot();
            setRestartState({ phase: 'success' });
            if (window.Toast) {
                window.Toast.success(
                    (result && result.message) || '已触发 AstrBot 重启',
                    { title: '重启 AstrBot' }
                );
            }
        } catch (e) {
            setRestartState({ phase: 'error' });
            const message = e && e.message ? e.message : '重启 AstrBot 失败，请查看服务端日志';
            if (window.Toast) window.Toast.error(message, { title: '重启 AstrBot 失败' });
        }
    };

    const handleManualRefresh = async () => {
        if (manualRefreshState.phase === 'loading') return;
        setManualRefreshState((current) => ({ ...current, phase: 'loading' }));
        try {
            // 手动刷新会回到入口层执行一次完整 loadAll，确保状态、会话、配置、任务全部同步。
            await onRefresh();
            setManualRefreshState({ phase: 'success', finishedAt: Date.now() });
            if (window.Toast) {
                window.Toast.success('控制台数据已刷新', { title: '运行状态' });
            }
        } catch (e) {
            setManualRefreshState({ phase: 'error', finishedAt: Date.now() });
            if (window.Toast) {
                window.Toast.error(e && e.message ? e.message : '刷新失败，请检查服务连接', {
                    title: '刷新失败',
                });
            }
        }
    };

    // 立即结束：只让计时器退场并把下一次主动消息交给调度器，不会真正发送消息。
    const handleFinishTimer = async (timer) => {
        const key = `${timer.timer_kind}-${timer.session_id}`;
        timerFeedback.setFeedback(key, { status: 'pending', text: '正在结束计时器并转入调度任务…' });
        try {
            await api.finishTimer(timer.session_id, timer.timer_kind);
            timerFeedback.setFeedback(key, { status: 'success', text: '已结束计时器，下一次主动消息已进入任务管理页' });
            // 卡片随后会从状态页消失，卡片内反馈看不到，必须补一条常驻短暂浮层。
            if (window.Toast) {
                window.Toast.success('已结束计时器，下一次主动消息已进入任务管理页', {
                    title: timer.session_display_name || timer.session_id,
                    key: `timer-finish-${timer.timer_kind}-${timer.session_id}`,
                });
            }
            await onRefresh();
        } catch (e) {
            const message = e && e.message ? e.message : '结束计时器失败，请稍后重试';
            timerFeedback.setFeedback(key, { status: 'error', text: message });
            if (window.Toast) window.Toast.error(message, { title: '结束计时器失败' });
        }
    };

    // 重置计时：按当前配置重新开始计时，不发送消息也不改动未回复计数。
    const handleResetTimer = async (timer) => {
        const key = `${timer.timer_kind}-${timer.session_id}`;
        timerFeedback.setFeedback(key, { status: 'pending', text: '正在按当前配置重新开始计时…' });
        try {
            await api.resetTimer(timer.session_id, timer.timer_kind);
            timerFeedback.setFeedback(key, { status: 'success', text: '已重新开始计时' });
            await onRefresh();
        } catch (e) {
            const message = e && e.message ? e.message : '重置计时器失败，请稍后重试';
            timerFeedback.setFeedback(key, { status: 'error', text: message });
            if (window.Toast) window.Toast.error(message, { title: '重置计时器失败' });
        }
    };

    const refreshButtonLabel = manualRefreshState.phase === 'loading'
        ? '正在刷新控制台数据...'
        : manualRefreshState.phase === 'success'
            ? '刷新完成'
            : manualRefreshState.phase === 'error'
                ? '刷新失败，请重试'
                : '刷新控制台数据';

    const refreshNoteText = manualRefreshState.phase === 'loading'
        ? '正在重新拉取运行状态、会话列表、配置与任务列表。'
        : manualRefreshState.phase === 'success'
            ? `已完成手动刷新 · ${formatFriendlyTime(manualRefreshState.finishedAt, displayTimezone)}`
            : manualRefreshState.phase === 'error'
                ? '本次手动刷新失败，请检查服务连接或稍后重试。'
                : '手动刷新会重新拉取运行状态、会话列表、配置与任务列表。';

    return (
        <Box>
            <div className="dashboard-grid proactive-status-grid">
                <div className="span-4">
                    <div className="card status-panel-card status-panel-card-primary">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-blue">⚡</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>服务状态</Typography>
                        </Box>

                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="插件状态"
                                value={pluginRunning ? '运行中' : '已停止'}
                                emphasize
                                status={pluginRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow
                                label="运行时长"
                                value={formatDuration(liveUptimeSeconds, { compact: true, maxUnits: 4 })}
                                emphasize
                            />
                            <StatusMetricRow label="已连接 WebSocket" value={`${Number(status.ws_connections !== undefined ? status.ws_connections : 0)} 个`} />
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-purple">⏱️</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>调度概览</Typography>
                        </Box>

                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="调度器"
                                value={schedulerRunning ? '已启动' : '未启动'}
                                emphasize
                                status={schedulerRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow label="当前任务总数" value={`${jobsCount} 个`} emphasize />
                            <StatusMetricRow label="会话数据量" value={`${sessionsCount} 个`} />
                            <StatusMetricRow label="自动触发计时器" value={`${autoTriggerTimers} 个`} />
                            <StatusMetricRow label="群沉默计时器" value={`${groupTimers} 个`} />
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-pink">🚀</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>快捷操作</Typography>
                        </Box>

                        <div className="status-actions-list">
                            <div className="status-action-tooltip-wrap">
                                <button
                                    type="button"
                                    className={`btn btn-action${manualRefreshState.phase === 'loading' ? ' is-busy' : ''}`}
                                    onClick={handleManualRefresh}
                                    disabled={manualRefreshState.phase === 'loading'}
                                    aria-describedby="status-refresh-tooltip"
                                >
                                    <span className="status-action-icon">
                                        {manualRefreshState.phase === 'loading' ? '⏳' : manualRefreshState.phase === 'success' ? '✅' : manualRefreshState.phase === 'error' ? '⚠️' : '🔄'}
                                    </span>
                                    {refreshButtonLabel}
                                </button>
                                <div className="status-action-tooltip-bubble" role="note" id="status-refresh-tooltip">
                                    {refreshNoteText}
                                </div>
                            </div>

                            <button
                                type="button"
                                className={`btn btn-action${reloadState.phase === 'loading' ? ' is-busy' : ''}`}
                                onClick={handleReloadPlugin}
                                disabled={reloadState.phase === 'loading'}
                                title="等价于聊天指令 /主动消息重启"
                            >
                                <span className="status-action-icon">
                                    {reloadState.phase === 'loading' ? '⏳' : reloadState.phase === 'success' ? '✅' : reloadState.phase === 'error' ? '⚠️' : '🔁'}
                                </span>
                                {reloadState.phase === 'loading'
                                    ? '正在重载插件...'
                                    : reloadState.phase === 'success'
                                        ? '重载请求已提交'
                                        : reloadState.phase === 'error'
                                            ? '重载失败，请重试'
                                            : '重载主动消息插件'}
                            </button>

                            <button
                                type="button"
                                className={`btn btn-action${restartState.phase === 'loading' ? ' is-busy' : ''}`}
                                onClick={handleRestartAstrbot}
                                disabled={restartState.phase === 'loading'}
                                title="等价于聊天指令 /重启AstrBot"
                            >
                                <span className="status-action-icon">
                                    {restartState.phase === 'loading' ? '⏳' : restartState.phase === 'success' ? '✅' : restartState.phase === 'error' ? '⚠️' : '♻️'}
                                </span>
                                {restartState.phase === 'loading'
                                    ? '正在重启 AstrBot...'
                                    : restartState.phase === 'success'
                                        ? '重启请求已提交'
                                        : restartState.phase === 'error'
                                            ? '重启失败，请重试'
                                            : '重启 AstrBot'}
                            </button>
                        </div>
                    </div>
                </div>

                <div className="span-12">
                    <div className="status-timers-section">
                        <Box className="status-timers-header-row">
                            <div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                                    {`会话计时器可视化 (当前共 ${timerCards.length} 个计时器)`}
                                </Typography>
                                <Typography variant="body2" className="tasks-header-subtitle">
                                    实时展示自动触发检测与群沉默检测的倒计时、进度和会话状态。此处卡片的倒计时结束后会进入任务管理页面
                                </Typography>
                            </div>
                            <div className="status-timers-summary-pills" aria-hidden="true">
                                {timerSections.map((section) => (
                                    <div className="status-timers-summary-pill" key={`summary-${section.key}`}>
                                        <span className="status-timers-summary-pill-label">{section.title}</span>
                                        <span className="status-timers-summary-pill-count">{section.cards.length}</span>
                                    </div>
                                ))}
                            </div>
                        </Box>

                        {timerCards.length === 0 ? (
                            <div className="card status-timers-empty-card">
                                <div className="tasks-empty-icon">🫧</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>暂无运行中的会话计时器</Typography>
                                <Typography variant="body1" color="text.secondary">
                                    当前没有正在计时的自动触发或群沉默会话。等插件进入下一轮会话调度后，这里会自动出现对应卡片。
                                </Typography>
                            </div>
                        ) : (
                            <div className="status-timer-sections">
                                {timerSections.map((section) => (
                                    <div className="status-timer-section-block" key={section.key}>
                                        <div className="status-timer-section-head">
                                            <div className="status-timer-section-head-main">
                                                <Typography variant="subtitle1" className="status-timer-section-title">
                                                    {section.title}
                                                </Typography>
                                                <Typography variant="body2" className="status-timer-section-desc">
                                                    {section.description}
                                                </Typography>
                                            </div>
                                            <div className="status-timer-section-count">{section.cards.length} 张</div>
                                        </div>

                                        <div className="status-timer-section-body">
                                            {section.cards.length === 0 ? (
                                                <div className="status-timer-section-empty">
                                                    {section.emptyText}
                                                </div>
                                            ) : (
                                                <div className="status-timers-grid">
                                                    {section.cards.map((timer) => {
                                                        const UI = window.TimerCardUI;
                                                        const actionKey = `${timer.timer_kind}-${timer.session_id}`;
                                                        const currentFeedback = timerFeedback.feedbackMap[actionKey];
                                                        const isBusy = Boolean(currentFeedback && currentFeedback.status === 'pending');

                                                        return (
                                                            <StatusTimerCard
                                                                key={actionKey}
                                                                timer={timer}
                                                                displayTimezone={displayTimezone}
                                                                nowMs={nowMs}
                                                                snapshotAtMs={snapshotAtMs}
                                                                snapshotServerMs={snapshotServerMs}
                                                                resetHint={Boolean(resetHintMap[actionKey])}
                                                                resetHintToken={resetHintMap[actionKey] || 0}
                                                                actions={(
                                                                    <UI.TimerCardActions
                                                                        feedbacks={[
                                                                            {
                                                                                text: currentFeedback ? currentFeedback.text : '',
                                                                                status: currentFeedback ? currentFeedback.status : '',
                                                                            },
                                                                        ]}
                                                                        buttons={(
                                                                            <React.Fragment>
                                                                                <Button
                                                                                    variant="outlined"
                                                                                    size="small"
                                                                                    fullWidth
                                                                                    disabled={isBusy}
                                                                                    onClick={() => handleFinishTimer(timer)}
                                                                                    sx={{ borderRadius: 2.5 }}
                                                                                >
                                                                                    立即结束
                                                                                </Button>
                                                                                <Button
                                                                                    variant="outlined"
                                                                                    size="small"
                                                                                    fullWidth
                                                                                    disabled={isBusy}
                                                                                    onClick={() => handleResetTimer(timer)}
                                                                                    sx={{ borderRadius: 2.5 }}
                                                                                >
                                                                                    重置计时
                                                                                </Button>
                                                                            </React.Fragment>
                                                                        )}
                                                                    />
                                                                )}
                                                            />
                                                        );
                                                    })}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按当前路由态切换展示。
window.StatusView = StatusView;
})();
