(() => {
/**
 * 文件职责：状态页视图，负责服务状态、计时器可视化与手动刷新交互展示。
 */

 const { Box, Typography, Chip } = MaterialUI;

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

function StatusTimerCard({ timer, displayTimezone, nowMs, resetHint }) {
    // 派生信息统一交给领域函数计算，视图只负责结构。
    const meta = React.useMemo(
        () => window.ProgressDomain.resolveStatusTimerCard(timer, nowMs, displayTimezone),
        [timer, nowMs, displayTimezone]
    );
    // 未回复次数大于 0 时用 warning 色，帮助管理员快速定位“机器人已被晾着”的会话。
    const unansweredCount = Number(meta.unanswered_count !== undefined ? meta.unanswered_count : 0);
    const chipColor = unansweredCount > 0 ? 'warning' : 'default';

    return (
        <div className={`card status-timer-card ${meta.accentClass} ${meta.status === 'urgent' ? 'is-urgent' : ''} ${meta.status === 'expired' ? 'is-expired' : ''} ${resetHint ? 'is-resetting' : ''}`}>
            <div className="status-timer-card-top">
                <div className="status-timer-title-block">
                    <Typography variant="subtitle2" className="status-timer-kicker">
                        {meta.sectionTitle}
                    </Typography>
                    <Typography
                        variant="body1"
                        className={`status-timer-session ${meta.hasAlias ? 'is-primary' : 'mono'}`}
                    >
                        {meta.sessionDisplayName}
                    </Typography>
                    {meta.sessionSubText ? (
                        <Typography variant="caption" className="status-timer-session-sub mono">
                            {`UMO · ${meta.sessionSubText}`}
                        </Typography>
                    ) : null}
                </div>
                <div className="status-timer-chip-stack">
                    <div className={`status-timer-kind-badge ${meta.accentClass}`}>{meta.kindBadgeLabel}</div>
                    <div className="status-timer-kind-badge">{meta.sourceModeLabel}</div>
                    <Chip
                        label={meta.unansweredLabel}
                        size="small"
                        color={chipColor}
                        variant={unansweredCount > 0 ? 'filled' : 'outlined'}
                    />
                </div>
            </div>

            <div className="status-timer-meta-row">
                <div className={`task-status-pill is-${meta.status}`}>{meta.statusLabel}</div>
                <div className="status-timer-category-pill">{meta.categoryLabel}</div>
            </div>

            {resetHint ? (
                // 群聊出现新消息导致目标时间明显后移时，短暂显示“已重置”提示，帮助理解状态变化原因。
                <div className="status-timer-reset-hint">
                    <span className="status-timer-reset-dot"></span>
                    群聊刚刚有新消息，沉默计时器已重新开始计时
                </div>
            ) : null}

            <div className="status-timer-panel">
                <div className="status-timer-primary-row">
                    <div>
                        <Typography variant="caption" className="status-timer-label">
                            倒计时
                        </Typography>
                        <Typography variant="body2" className="status-timer-countdown">
                            {meta.countdownText}
                        </Typography>
                    </div>
                    <div className="status-timer-progress-value">{meta.progressPercent}%</div>
                </div>
                <div className="task-progress-track">
                    {/* 宽度通过自定义属性传递，样式表统一声明 width，避免内联样式散落。 */}
                    <div className={`task-progress-bar is-${meta.status}`} style={{ '--progress-width': `${meta.progressPercent}%` }}></div>
                </div>
            </div>

            <div className="status-timer-info-grid">
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">
                        目标时间
                    </Typography>
                    <Typography variant="body2" className="status-timer-info-value">
                        {meta.targetText}
                    </Typography>
                </div>
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">
                        计时窗口
                    </Typography>
                    <Typography variant="body2" className="status-timer-info-value">
                        {meta.window_seconds ? formatDuration(meta.window_seconds, { compact: true, maxUnits: 2 }) : '--'}
                    </Typography>
                </div>
            </div>
        </div>
    );
}

function LogUploadCard({ displayTimezone }) {
    const api = useApi();
    // 状态机：idle -> loading -> success / error，附带已上传结果供展示链接。
    const [uploadState, setUploadState] = React.useState({ phase: 'idle', result: null, error: '' });
    const [stats, setStats] = React.useState(null);
    // 可选行数，默认 500（与后端默认一致），上限由后端统计返回。
    const [count, setCount] = React.useState(500);

    const loadStats = React.useCallback(async () => {
        try {
            const payload = await api.getLogStats();
            setStats(payload || null);
        } catch (e) {
            // 统计失败不打断页面，仅保留上次快照。
            setStats(null);
        }
    }, [api]);

    React.useEffect(() => {
        loadStats();
    }, [loadStats]);

    const handleUpload = async () => {
        if (uploadState.phase === 'loading') return;
        setUploadState({ phase: 'loading', result: null, error: '' });
        try {
            const payload = await api.uploadLogs(Number(count) || 500);
            setUploadState({ phase: 'success', result: payload || null, error: '' });
            // 上传后刷新统计，避免同一份日志被反复上传而不自知。
            loadStats();
        } catch (e) {
            const message = (e && e.payload && (e.payload.message || e.payload.error))
                || (e && e.message)
                || '上传失败';
            setUploadState({ phase: 'error', result: null, error: message });
        }
    };

    const buttonLabel = uploadState.phase === 'loading'
        ? '正在上传运行日志...'
        : uploadState.phase === 'success'
            ? '上传完成，可再次上传'
            : uploadState.phase === 'error'
                ? '上传失败，点击重试'
                : '上传运行日志并生成链接';

    const availableLines = stats ? Number(stats.lines || 0) : 0;
    const maxCount = stats ? Number(stats.max_count || 10000) : 10000;

    return (
        <div className="span-12">
            <div className="card log-upload-card">
                <div className="log-upload-head">
                    <div className="log-upload-head-main">
                        <div className="status-panel-icon status-panel-icon-blue">📤</div>
                        <div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>运行日志上传</Typography>
                            <Typography variant="body2" className="tasks-header-subtitle">
                                读取本次启动以来插件自身的运行日志（INFO 及以上），脱敏后上传到日志分享服务，
                                只返回链接，不会把日志正文发送到聊天或页面中。
                            </Typography>
                        </div>
                    </div>
                    <div className="log-upload-stats">
                        <div className="log-upload-stat">
                            <span className="log-upload-stat-label">可导出行数</span>
                            <span className="log-upload-stat-value">{availableLines} 行</span>
                        </div>
                        <div className="log-upload-stat">
                            <span className="log-upload-stat-label">缓冲占用</span>
                            <span className="log-upload-stat-value">{stats ? `${Number(stats.size_kb || 0)} KB` : '--'}</span>
                        </div>
                    </div>
                </div>

                <div className="log-upload-controls">
                    <label className="log-upload-count-field">
                        <span className="log-upload-count-label">导出行数</span>
                        <input
                            type="number"
                            min="1"
                            max={maxCount}
                            step="50"
                            value={count}
                            className="log-upload-count-input"
                            onChange={(event) => {
                                // 输入过程允许空值，提交时再交给后端钳制。
                                const next = event.target.value;
                                setCount(next === '' ? '' : Number(next));
                            }}
                            disabled={uploadState.phase === 'loading'}
                        />
                    </label>

                    <div className="status-action-tooltip-wrap log-upload-button-wrap">
                        <button
                            type="button"
                            className={`btn btn-action${uploadState.phase === 'loading' ? ' is-busy' : ''}`}
                            onClick={handleUpload}
                            disabled={uploadState.phase === 'loading' || availableLines === 0}
                            aria-describedby="log-upload-tooltip"
                        >
                            <span className="status-action-icon">
                                {uploadState.phase === 'loading' ? '⏳' : uploadState.phase === 'success' ? '✅' : uploadState.phase === 'error' ? '⚠️' : '📤'}
                            </span>
                            {buttonLabel}
                        </button>
                        <div className="status-action-tooltip-bubble" role="note" id="log-upload-tooltip">
                            日志内容会对凭据、会话标识与本机路径做脱敏，但仍请自行确认后再公开分享链接。
                        </div>
                    </div>
                </div>

                {availableLines === 0 ? (
                    <div className="log-upload-hint">
                        暂无可导出的运行日志。日志自插件本次启动开始累积，稍后再试。
                    </div>
                ) : null}

                {uploadState.phase === 'error' ? (
                    <div className="log-upload-result is-error">
                        <Typography variant="body2">❌ {uploadState.error}</Typography>
                    </div>
                ) : null}

                {uploadState.phase === 'success' && uploadState.result ? (
                    <div className="log-upload-result is-success">
                        <Typography variant="body2">
                            ✅ 已上传 {Number(uploadState.result.exported_lines || 0)} 行
                            （{Number(uploadState.result.size_kb || 0)} KB），
                            完成于 {formatFriendlyTime(Date.now(), displayTimezone)}。
                        </Typography>
                        <div className="log-upload-link-row">
                            <a
                                className="log-upload-link"
                                href={uploadState.result.url}
                                target="_blank"
                                rel="noopener noreferrer"
                            >
                                {uploadState.result.url}
                            </a>
                            <button
                                type="button"
                                className="btn log-upload-copy-btn"
                                onClick={() => {
                                    const url = uploadState.result?.url || '';
                                    if (!url) return;
                                    if (navigator.clipboard && navigator.clipboard.writeText) {
                                        navigator.clipboard.writeText(url).catch(() => {});
                                        return;
                                    }
                                    // 非安全上下文下 clipboard API 不可用，退化为选中提示。
                                    window.prompt('复制日志链接', url);
                                }}
                            >
                                复制链接
                            </button>
                        </div>
                        {uploadState.result.expires_at ? (
                            <Typography variant="caption" className="log-upload-expire">
                                链接过期时间：{uploadState.result.expires_at}
                            </Typography>
                        ) : null}
                    </div>
                ) : null}
            </div>
        </div>
    );
}

function StatusView({ onRefresh }) {
    const { state } = useAppContext();
    const status = state.status || {};
    // 全局共享时钟，避免每个视图各起一个每秒 interval。
    const nowMs = useNow();
    // resetHintMap 记录哪些群沉默卡片当前需要显示“刚刚被重置”的短暂提示。
    const [resetHintMap, setResetHintMap] = React.useState({});
    // 手动刷新按钮的视觉反馈状态机：idle -> loading -> success / error。
    const [manualRefreshState, setManualRefreshState] = React.useState({ phase: 'idle', finishedAt: 0 });
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

    // 统一把后端状态字段规整为前端友好的基础数字类型。
    const uptimeSeconds = Number(status.uptime_seconds !== undefined ? status.uptime_seconds : 0);
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
            const currentRemaining = Math.max(0, Number(timer.remaining_seconds !== undefined ? timer.remaining_seconds : 0));
            const currentTarget = Number(timer.target_time !== undefined ? timer.target_time : 0);
            const currentStatus = String(timer.status || '');
            const previous = previousTimerSnapshotRef.current[key] || null;

            activeKeys.add(key);
            nextSnapshot[key] = {
                remainingSeconds: currentRemaining,
                targetTime: currentTarget,
                status: currentStatus,
            };

            // 真正的“被重置”应表现为：同一张群沉默卡片的剩余时间突然回升，且当前仍处于有效运行态。
            const isGroupReset = timer.timer_kind === 'group_silence'
                && previous
                && currentStatus !== 'expired'
                && currentStatus !== 'unknown'
                && currentRemaining - Number(previous.remainingSeconds !== undefined ? previous.remainingSeconds : 0) > 3;

            if (isGroupReset) {
                nextHints[key] = true;
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
            return Object.assign({}, filtered, nextHints);
        });
    }, [timerSnapshotKey]);

    const handleManualRefresh = async () => {
        if (manualRefreshState.phase === 'loading') return;
        setManualRefreshState((current) => ({ ...current, phase: 'loading' }));
        try {
            // 手动刷新会回到入口层执行一次完整 loadAll，确保状态、会话、配置、任务全部同步。
            await onRefresh();
            setManualRefreshState({ phase: 'success', finishedAt: Date.now() });
        } catch (e) {
            setManualRefreshState({ phase: 'error', finishedAt: Date.now() });
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
                                value={formatDuration(uptimeSeconds, { compact: true, maxUnits: 4 })}
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
                        </div>
                    </div>
                </div>

                <LogUploadCard displayTimezone={displayTimezone} />

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
                                                    {section.cards.map((timer) => (
                                                        <StatusTimerCard
                                                            key={`${timer.timer_kind}-${timer.session_id}`}
                                                            timer={timer}
                                                            displayTimezone={displayTimezone}
                                                            nowMs={nowMs}
                                                            resetHint={Boolean(resetHintMap[`${timer.timer_kind}-${timer.session_id}`])}
                                                        />
                                                    ))}
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
