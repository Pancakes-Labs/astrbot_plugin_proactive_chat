(() => {
/**
 * 文件职责：任务页视图，负责调度任务列表、倒计时进度与任务操作入口展示。
 */

 const { Box, Typography, Button } = MaterialUI;


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
                // 触发过程可能较长，用户此时多半已离开该卡片，补一条浮层确认结果。
                // 标题取派生出的会话显示名，并对同一任务使用固定 key 避免重复堆叠。
                if (window.Toast) {
                    const card = window.ProgressDomain.resolveScheduledCard(job, Date.now(), displayTimezone);
                    window.Toast.success('立即触发已完成', {
                        title: (card && card.sessionDisplayName) || job.id,
                        key: `task-trigger-done-${job.id}`,
                    });
                }
            }
        });
        // displayTimezone 参与派生会话显示名，需一并纳入依赖避免闭包取到旧值。
    }, [jobs, triggerFeedback.setFeedback, displayTimezone]);

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
            const message = e.message || '触发任务失败，请稍后重试';
            triggerFeedback.setFeedback(session, {
                status: 'error',
                text: message,
            });
            dispatch({ type: 'SET_ERROR', payload: e.message || '触发任务失败' });
            // 卡片反馈可能已滚出视口，用常驻短暂浮层保证失败一定被看到。
            if (window.Toast) window.Toast.error(message, { title: '立即触发失败' });
        }
    };

    const cancelJob = async (session) => {
        try {
            await api.cancelJob(session);
            if (window.Toast) window.Toast.success('调度任务已取消');
            await onRefresh();
        } catch (e) {
            const message = e.message || '取消任务失败';
            dispatch({ type: 'SET_ERROR', payload: message });
            if (window.Toast) window.Toast.error(message, { title: '取消任务失败' });
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
            const message = e.message || '重新调度失败，请稍后重试';
            rescheduleFeedback.setFeedback(session, {
                status: 'error',
                text: message,
            });
            dispatch({ type: 'SET_ERROR', payload: e.message || '重新调度失败' });
            if (window.Toast) window.Toast.error(message, { title: '重新调度失败' });
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
                        // 与状态页共用同一套三段式卡片结构，派生文案统一走领域层。
                        const task = window.ProgressDomain.resolveScheduledCard(job, nowMs, displayTimezone);
                        const UI = window.TimerCardUI;

                        const isTriggerRunning = Boolean(job.manual_trigger_in_progress);
                        const currentTriggerFeedback = triggerFeedback.feedbackMap[job.id];
                        const currentRescheduleFeedback = rescheduleFeedback.feedbackMap[job.id];
                        const isRescheduling = currentRescheduleFeedback && currentRescheduleFeedback.status === 'pending';
                        const triggerButtonLabel = isTriggerRunning ? '触发中…' : '立即触发';
                        const triggerHelperText = isTriggerRunning
                            ? ((currentTriggerFeedback && currentTriggerFeedback.text) || '正在触发，等待 LLM 回复完成…')
                            : (currentTriggerFeedback && currentTriggerFeedback.text);
                        const rescheduleHelperText = currentRescheduleFeedback && currentRescheduleFeedback.text;
                        const triggerFeedbackStatus = currentTriggerFeedback ? currentTriggerFeedback.status : '';
                        const rescheduleFeedbackStatus = currentRescheduleFeedback ? currentRescheduleFeedback.status : '';

                        return (
                            <div className={`card tcard accent-primary ${task.status === 'urgent' ? 'is-urgent' : ''} ${task.status === 'expired' ? 'is-expired' : ''}`} key={job.id}>
                                <UI.TimerCardIdentity
                                    name={task.sessionDisplayName}
                                    umoText={task.umoText}
                                    badges={(
                                        <React.Fragment>
                                            <span className={`tcard-badge ${isTriggerRunning ? 'accent-auto-group' : ''}`}>
                                                {isTriggerRunning ? '立即触发中' : '待发送'}
                                            </span>
                                            <span className={`task-status-pill is-${task.status}`}>{task.statusLabel}</span>
                                        </React.Fragment>
                                    )}
                                />

                                <UI.TimerCardCountdown
                                    percent={task.progressPercent}
                                    status={task.status}
                                    kicker={task.heroLabel}
                                    tokens={task.countdownTokens}
                                    emptyText={task.statusLabel}
                                    ringTitle={`${task.progressLabel}，已等待 ${task.elapsedText}`}
                                    foot={(
                                        <React.Fragment>
                                            <UI.TimerCardFootItem
                                                label="预计发送时间："
                                                value={task.executeClockText}
                                                title={`发送时间 ${task.executeFullText}`}
                                            />
                                            <span className="tcard-foot-dot">·</span>
                                            <UI.TimerCardFootItem label={`已等待 ${task.elapsedText}`} />
                                        </React.Fragment>
                                    )}
                                />

                                <UI.TimerCardFacts
                                    facts={[
                                        {
                                            label: '生效人格',
                                            value: task.personaText,
                                            sub: task.personaSourceText,
                                            title: task.personaTitle,
                                            personaState: task.personaState,
                                        },
                                        { label: '上下文来源', value: task.sourceModeLabel },
                                        { label: '发送延迟', value: task.intervalText },
                                        { label: '免打扰时段', value: task.quietText },
                                        {
                                            label: '未回复次数',
                                            value: task.unansweredValueText,
                                            sub: task.unansweredIsLimited
                                                ? `上限达到 ${task.max_unanswered_times} 次后暂停`
                                                : '未设上限',
                                            title: task.unansweredHintText,
                                            warning: task.unansweredIsWarning,
                                        },
                                    ]}
                                />

                                <UI.TimerCardActions
                                    feedbacks={[
                                        { text: triggerHelperText, status: triggerFeedbackStatus },
                                        { text: rescheduleHelperText, status: rescheduleFeedbackStatus },
                                    ]}
                                    buttons={(
                                        <React.Fragment>
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
                                        </React.Fragment>
                                    )}
                                />
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
