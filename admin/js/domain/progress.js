/**
 * 文件职责：进度领域纯函数，统一“剩余秒数 -> 状态标签 + 进度”的推导逻辑。
 */

(function () {
    // 状态阈值：两个视图共用同一套语义，避免分别维护导致表现不一致。
    const URGENT_THRESHOLD_SECONDS = 300;
    const SOON_THRESHOLD_SECONDS = 1800;

    function resolveProgressStatus(remainingSeconds, labels) {
        const text = labels || {};
        if (remainingSeconds === null || !Number.isFinite(Number(remainingSeconds))) {
            return { status: 'unknown', statusLabel: text.unknown || '待确认' };
        }

        const seconds = Number(remainingSeconds);
        if (seconds <= 0) {
            return { status: 'expired', statusLabel: text.expired || '待刷新' };
        }
        if (seconds <= URGENT_THRESHOLD_SECONDS) {
            return { status: 'urgent', statusLabel: text.urgent || '即将触发' };
        }
        if (seconds <= SOON_THRESHOLD_SECONDS) {
            return { status: 'soon', statusLabel: text.soon || '等待触发' };
        }
        return { status: 'future', statusLabel: text.future || '正常排队' };
    }

    function clampPercent(value) {
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) return 0;
        return Math.max(0, Math.min(100, Math.round(numeric)));
    }

    // 同一会话若同时存在多类计时器，只保留最关键的一张卡片。
    function dedupeTimerCards(cards) {
        const priorityMap = {
            group_silence: 3,
            auto_trigger: 2,
        };
        const merged = new Map();

        (Array.isArray(cards) ? cards : []).forEach((card) => {
            const key = String((card && card.session_id) || '');
            if (!key) return;

            const existing = merged.get(key);
            if (!existing) {
                merged.set(key, card);
                return;
            }

            const currentPriority = priorityMap[card.timer_kind] !== undefined ? priorityMap[card.timer_kind] : 0;
            const existingPriority = priorityMap[existing.timer_kind] !== undefined ? priorityMap[existing.timer_kind] : 0;

            if (currentPriority > existingPriority) {
                merged.set(key, card);
                return;
            }

            if (currentPriority === existingPriority) {
                // 类型优先级相同时保留更快触发的那张卡，突出最紧迫状态。
                const currentRemaining = Number(card.remaining_seconds !== undefined ? card.remaining_seconds : Number.MAX_SAFE_INTEGER);
                const existingRemaining = Number(existing.remaining_seconds !== undefined ? existing.remaining_seconds : Number.MAX_SAFE_INTEGER);
                if (currentRemaining < existingRemaining) {
                    merged.set(key, card);
                }
            }
        });

        return Array.from(merged.values());
    }

    // 状态页计时器卡片：把后端字段换算成可直接渲染的文案与样式字段。
    function resolveStatusTimerCard(timer, nowMs, displayTimezone) {
        const targetTime = window.TimeDomain.toDate(timer.target_time);
        const remainingSeconds = window.TimeDomain.resolveTimerRemainingSeconds(timer, nowMs);
        const windowSeconds = Math.max(0, Number(timer.window_seconds !== undefined ? timer.window_seconds : 0));

        const fallbackProgress = windowSeconds > 0
            ? clampPercent(((windowSeconds - remainingSeconds) / windowSeconds) * 100)
            : 0;
        const progressPercent = clampPercent(
            timer.progress_percent !== undefined && timer.progress_percent !== null
                ? timer.progress_percent
                : fallbackProgress
        );

        const statusMeta = resolveProgressStatus(
            targetTime ? remainingSeconds : null,
            {
                future: '稳定运行',
                soon: '正常计时',
                urgent: '即将结束',
                expired: '待刷新',
                unknown: '待确认',
            }
        );

        const isGroupSession = timer.session_category === 'group';
        const isGroupSilence = timer.timer_kind === 'group_silence';
        const categoryLabel = isGroupSession ? '群会话' : '私聊会话';
        const sectionKey = isGroupSilence ? 'group_silence' : 'auto_trigger';
        const sectionTitle = isGroupSilence ? '群沉默倒计时' : '自动触发检测';
        const accentClass = isGroupSilence
            ? 'accent-group-silence'
            : (isGroupSession ? 'accent-auto-group' : 'accent-auto-friend');
        const kindBadgeLabel = isGroupSilence
            ? '沉默重置型'
            : (isGroupSession ? '群自动触发' : '私聊自动触发');

        const session = window.SessionDomain.resolveSessionDisplay({
            session: timer.session_id,
            session_name: timer.session_display_name || timer.session_name,
        });

        return Object.assign({}, timer, {
            startedAt: window.TimeDomain.toDate(timer.started_at),
            targetTime: targetTime,
            remainingSeconds: remainingSeconds,
            progressPercent: progressPercent,
            status: statusMeta.status,
            statusLabel: statusMeta.statusLabel,
            categoryLabel: categoryLabel,
            sectionKey: sectionKey,
            sectionTitle: sectionTitle,
            accentClass: accentClass,
            kindBadgeLabel: kindBadgeLabel,
            sourceModeLabel: window.resolveSourceModeLabel(timer.source_mode),
            unansweredLabel: window.formatUnansweredLabel(timer.unanswered_count, timer.max_unanswered_times),
            countdownText: targetTime
                ? (remainingSeconds > 0
                    ? `${window.formatDuration(remainingSeconds, { compact: true, maxUnits: 3 })} 后到期`
                    : '等待下一轮刷新确认')
                : '暂无有效目标时间',
            sessionDisplayName: session.displayName,
            sessionSubText: session.subText,
            hasAlias: session.hasAlias,
            targetText: targetTime
                ? window.formatDateTime(targetTime, displayTimezone, { includeYear: true, includeSeconds: true })
                : '--',
        });
    }

    // 任务页卡片：推导下一次执行时间、倒计时、状态与进度。
    function resolveTaskProgress(job, nowMs, displayTimezone) {
        const nextRun = window.TimeDomain.toDate(job.next_run_time);
        const nextTrigger = window.TimeDomain.toDate(job.next_trigger_time);
        const scheduledAtMs = window.TimeDomain.toEpochMs(job.last_scheduled_at);

        const randomIntervalSeconds = Number(job.last_schedule_random_interval_seconds);
        const minIntervalSeconds = Number(job.last_schedule_min_interval_seconds);
        const maxIntervalSeconds = Number(job.last_schedule_max_interval_seconds);

        if (!nextRun) {
            return {
                nextRun: null,
                nextTrigger: nextTrigger,
                remainingSeconds: 0,
                remainingText: '--',
                countdownText: '暂无有效执行时间',
                friendlyText: '--',
                status: 'unknown',
                statusLabel: '待确认',
                progressPercent: 0,
            };
        }

        const remainingSeconds = Math.max(0, Math.ceil((nextRun.getTime() - nowMs) / 1000));
        // 展示时区必须由调用方传入，避免此处写死东八区导致跨时区显示不一致。
        const friendlyText = window.formatFriendlyTime(nextRun, displayTimezone || 'Asia/Shanghai');
        const remainingText = remainingSeconds > 0
            ? window.formatDuration(remainingSeconds, { compact: true, maxUnits: 3 })
            : '已到触发时间';

        const statusMeta = resolveProgressStatus(remainingSeconds, {
            future: '正常排队',
            soon: '等待触发',
            urgent: '即将触发',
            expired: '待刷新',
            unknown: '待确认',
        });

        // 优先使用随机调度窗口；缺失时退化为 nextTrigger / max / min 区间推导。
        const candidateWindowSeconds = [
            randomIntervalSeconds,
            nextTrigger ? Math.max(0, Math.round((nextTrigger.getTime() - nowMs) / 1000)) : 0,
            maxIntervalSeconds,
            minIntervalSeconds,
        ].filter((value) => Number.isFinite(value) && value > 0);
        const windowSeconds = candidateWindowSeconds.length > 0 ? candidateWindowSeconds[0] : 0;

        let progressPercent = remainingSeconds <= 0 ? 100 : 0;
        if (windowSeconds > 0) {
            const elapsedSeconds = scheduledAtMs
                ? Math.max(0, (nowMs - scheduledAtMs) / 1000)
                : Math.max(0, windowSeconds - remainingSeconds);
            progressPercent = clampPercent((elapsedSeconds / windowSeconds) * 100);
        }

        return {
            nextRun: nextRun,
            nextTrigger: nextTrigger,
            remainingSeconds: remainingSeconds,
            remainingText: remainingText,
            countdownText: remainingSeconds > 0 ? `${remainingText} 后执行` : '等待下一轮刷新确认',
            friendlyText: friendlyText,
            status: statusMeta.status,
            statusLabel: statusMeta.statusLabel,
            progressPercent: progressPercent,
        };
    }

    window.ProgressDomain = Object.freeze({
        URGENT_THRESHOLD_SECONDS: URGENT_THRESHOLD_SECONDS,
        SOON_THRESHOLD_SECONDS: SOON_THRESHOLD_SECONDS,
        resolveProgressStatus: resolveProgressStatus,
        clampPercent: clampPercent,
        dedupeTimerCards: dedupeTimerCards,
        resolveStatusTimerCard: resolveStatusTimerCard,
        resolveTaskProgress: resolveTaskProgress,
    });
})();
