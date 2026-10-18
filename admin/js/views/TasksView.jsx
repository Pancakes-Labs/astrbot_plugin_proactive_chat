(() => {
/**
 * 文件职责：任务页视图，负责调度任务列表、倒计时进度与任务操作入口展示。
 */

 const { Box, Typography, Button, Chip } = MaterialUI;

function formatQuietHoursText(value) {
    const raw = String(value || '').trim();
    if (!raw) return '未配置';

    const matched = raw.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
    if (!matched) return raw;

    const startHour = Number(matched[1]);
    const endHour = Number(matched[2]);
    if (!Number.isInteger(startHour) || !Number.isInteger(endHour)) return raw;
    if (startHour < 0 || startHour > 23 || endHour < 0 || endHour > 23) return raw;

    return `${String(startHour).padStart(2, '0')}:00 - ${String(endHour).padStart(2, '0')}:00`;
}

function formatScheduleIntervalText(minMinutes, maxMinutes) {
    const minValue = Number(minMinutes);
    const maxValue = Number(maxMinutes);
    const hasMin = Number.isFinite(minValue) && minValue > 0;
    const hasMax = Number.isFinite(maxValue) && maxValue > 0;

    if (!hasMin && !hasMax) return '未配置';
    if (hasMin && hasMax) {
        if (minValue > maxValue) {
            return `配置异常：${minValue} > ${maxValue} 分钟`;
        }
        return `${minValue} - ${maxValue} 分钟`;
    }
    if (hasMin) return `${minValue} 分钟`;
    return `${maxValue} 分钟`;
}

function TasksView({ onRefresh }) {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    // 与状态页共用同一个全局时钟，避免多个每秒 interval。
    const nowMs = useNow();
    const triggerFeedback = useFeedbackMap(3000);
    const rescheduleFeedback = useFeedbackMap(3000);
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';

    const jobs = state.jobs || [];

    // 用 ref 读取反馈快照，避免把 feedbackMap 放进依赖造成 effect 自激。
    const triggerFeedbackMapRef = React.useRef({});
    triggerFeedbackMapRef.current = triggerFeedback.feedbackMap;

    // 后端标记“立即触发进行中”时，把前端反馈同步为 pending；结束后转为 success。
    React.useEffect(() => {
        jobs.forEach((job) => {
            if (job.manual_trigger_in_progress) {
                triggerFeedback.setFeedback(job.id, {
                    status: 'pending',
                    text: '正在触发，等待 LLM 回复完成…',
                });
                return;
            }
            const current = triggerFeedbackMapRef.current[job.id];
            if (current && current.status === 'pending') {
                triggerFeedback.setFeedback(job.id, {
                    status: 'success',
                    text: '本次立即触发已完成，按钮已恢复可用',
                });
            }
        });
    }, [jobs, triggerFeedback.setFeedback]);

    const triggerNow = async (session) => {
        triggerFeedback.setFeedback(session, { status: 'pending', text: '正在触发，等待 LLM 回复完成…' });
        try {
            // 手动触发后会重新走一次父级全量刷新，确保状态页与任务页同步更新。
            const result = await api.triggerJob(session);
            triggerFeedback.setFeedback(session, {
                status: 'pending',
                text: (result && result.message) || '已开始立即触发，正在等待 LLM 回复完成…',
            });
            await onRefresh();
        } catch (e) {
            triggerFeedback.setFeedback(session, {
                status: 'error',
                text: e.message || '触发任务失败，请稍后重试',
            });
            dispatch({ type: 'SET_ERROR', payload: e.message || '触发任务失败' });
        }
    };

    const cancelJob = async (session) => {
        try {
            await api.cancelJob(session);
            await onRefresh();
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '取消任务失败' });
        }
    };

    const rescheduleJob = async (session) => {
        rescheduleFeedback.setFeedback(session, { status: 'pending', text: '正在重新调度下一次主动消息时间…' });
        try {
            const result = await api.rescheduleJob(session);
            rescheduleFeedback.setFeedback(session, {
                status: 'success',
                text: (result && result.message) || '已重新调度下一次主动消息时间',
            });
            await onRefresh();
        } catch (e) {
            rescheduleFeedback.setFeedback(session, {
                status: 'error',
                text: e.message || '重新调度失败，请稍后重试',
            });
            dispatch({ type: 'SET_ERROR', payload: e.message || '重新调度失败' });
        }
    };

    return (
        <Box>
            <Box className="tasks-header-row">
                <div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                        {`调度任务 (当前共 ${jobs.length} 个调度任务)`}
                    </Typography>
                </div>
                <Button variant="contained" onClick={onRefresh} startIcon={<span>🔄</span>} sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}>
                    刷新任务
                </Button>
            </Box>

            {jobs.length === 0 ? (
                <div className="card tasks-empty-card">
                    <div className="tasks-empty-icon">🪄</div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>暂无调度任务</Typography>
                    <Typography variant="body1" color="text.secondary">
                        当前没有待执行的主动消息任务。当会话满足调度条件后，这里会自动展示任务卡片与倒计时信息。
                    </Typography>
                </div>
            ) : (
                <div className="tasks-grid-enhanced">
                    {jobs.map((job) => {
                        // 每张任务卡在渲染前先推导出倒计时、状态与进度等派生信息。
                        const task = window.ProgressDomain.resolveTaskProgress(job, nowMs, displayTimezone);
                        const unansweredCount = Number(job.unanswered_count !== undefined ? job.unanswered_count : 0);
                        const chipColor = unansweredCount > 0 ? 'warning' : 'default';
                        const session = window.SessionDomain.resolveSessionDisplay(job);
                        const sourceModeLabel = resolveSourceModeLabel(job.source_mode);
                        const unansweredLabel = formatUnansweredLabel(job.unanswered_count, job.max_unanswered_times);

                        const isTriggerRunning = Boolean(job.manual_trigger_in_progress);
                        const currentTriggerFeedback = triggerFeedback.feedbackMap[job.id];
                        const currentRescheduleFeedback = rescheduleFeedback.feedbackMap[job.id];
                        const isRescheduling = currentRescheduleFeedback && currentRescheduleFeedback.status === 'pending';
                        const triggerButtonLabel = isTriggerRunning ? '触发中…' : '立即触发';
                        const triggerHelperText = isTriggerRunning
                            ? ((currentTriggerFeedback && currentTriggerFeedback.text) || '正在触发，等待 LLM 回复完成…')
                            : (currentTriggerFeedback && currentTriggerFeedback.text);
                        const rescheduleHelperText = currentRescheduleFeedback && currentRescheduleFeedback.text;
                        const scheduleIntervalText = formatScheduleIntervalText(
                            job.schedule_min_interval_minutes,
                            job.schedule_max_interval_minutes,
                        );
                        const quietHoursText = formatQuietHoursText(job.quiet_hours);

                        return (
                            <div className={`card task-card-enhanced ${task.status === 'urgent' ? 'is-urgent' : ''} ${task.status === 'expired' ? 'is-expired' : ''}`} key={job.id}>
                                <div className="task-card-top is-overflow-visible">
                                    <div className="task-card-title-block is-overflow-visible">
                                        <Typography variant="subtitle2" className="task-card-kicker">
                                            会话
                                        </Typography>
                                        <Typography
                                            variant="body1"
                                            className={`task-card-session ${session.hasAlias ? 'is-primary' : 'mono'}`}
                                        >
                                            {session.displayName}
                                        </Typography>
                                    </div>
                                    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 1, flexShrink: 0 }}>
                                        <Box className="task-source-mode-pill">
                                            {sourceModeLabel}
                                        </Box>
                                        <Chip
                                            label={unansweredLabel}
                                            size="small"
                                            color={chipColor}
                                            variant={unansweredCount > 0 ? 'filled' : 'outlined'}
                                        />
                                    </Box>
                                </div>

                                {session.subText ? (
                                    <Typography
                                        variant="caption"
                                        className="task-card-session-sub mono"
                                        sx={{
                                            display: 'block',
                                            width: '100%',
                                            mt: -0.5,
                                            mb: 0.5,
                                            overflow: 'visible',
                                            whiteSpace: 'nowrap',
                                        }}
                                    >
                                        {`UMO · ${session.subText}`}
                                    </Typography>
                                ) : null}

                                <div className="task-next-run-panel">
                                    <div className="task-next-run-primary-row">
                                        <div>
                                            <Typography variant="caption" className="task-next-run-label">
                                                下一次执行时间
                                            </Typography>
                                            <Typography variant="body2" className="task-next-run-time">
                                                {task.nextRun ? formatDateTime(task.nextRun, displayTimezone, { includeYear: true, includeSeconds: true }) : '--'}
                                            </Typography>
                                        </div>
                                        <div className={`task-status-pill is-${task.status}`}>
                                            {task.statusLabel}
                                        </div>
                                    </div>
                                    <Typography variant="body2" className="task-countdown-text">
                                        {task.countdownText}
                                    </Typography>
                                    <div className="task-progress-track">
                                        <div
                                            className={`task-progress-bar is-${task.status}`}
                                            style={{ '--progress-width': `${task.progressPercent}%` }}
                                        ></div>
                                    </div>
                                </div>

                                <Box className="task-meta-grid">
                                    <Box className="task-meta-cell">
                                        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.4 }}>
                                            调度间隔
                                        </Typography>
                                        <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>
                                            {scheduleIntervalText}
                                        </Typography>
                                    </Box>
                                    <Box className="task-meta-cell">
                                        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.4 }}>
                                            免打扰时段
                                        </Typography>
                                        <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>
                                            {quietHoursText}
                                        </Typography>
                                    </Box>
                                </Box>

                                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, mt: 'auto', pt: 1.5 }}>
                                    {triggerHelperText ? (
                                        <Typography
                                            variant="caption"
                                            sx={{
                                                minHeight: 20,
                                                color: currentTriggerFeedback && currentTriggerFeedback.status === 'error'
                                                    ? 'error.main'
                                                    : currentTriggerFeedback && currentTriggerFeedback.status === 'success'
                                                        ? 'success.main'
                                                        : 'text.secondary',
                                            }}
                                        >
                                            {triggerHelperText}
                                        </Typography>
                                    ) : null}
                                    {rescheduleHelperText ? (
                                        <Typography
                                            variant="caption"
                                            sx={{
                                                minHeight: 20,
                                                color: currentRescheduleFeedback.status === 'error'
                                                    ? 'error.main'
                                                    : currentRescheduleFeedback.status === 'success'
                                                        ? 'success.main'
                                                        : 'text.secondary',
                                            }}
                                        >
                                            {rescheduleHelperText}
                                        </Typography>
                                    ) : null}
                                    <Box sx={{ display: 'flex', gap: 1 }}>
                                        <Button
                                            variant="outlined"
                                            size="small"
                                            fullWidth
                                            disabled={isTriggerRunning || isRescheduling}
                                            onClick={() => triggerNow(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            {triggerButtonLabel}
                                        </Button>
                                        <Button
                                            variant="outlined"
                                            size="small"
                                            fullWidth
                                            disabled={isTriggerRunning || isRescheduling}
                                            onClick={() => rescheduleJob(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            {isRescheduling ? '重新调度中…' : '重新调度'}
                                        </Button>
                                        <Button
                                            variant="outlined"
                                            color="error"
                                            size="small"
                                            fullWidth
                                            disabled={isRescheduling}
                                            onClick={() => cancelJob(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            取消任务
                                        </Button>
                                    </Box>
                                </Box>
                            </div>
                        );
                    })}
                </div>
            )}
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按 currentView 切换。
window.TasksView = TasksView;
})();
