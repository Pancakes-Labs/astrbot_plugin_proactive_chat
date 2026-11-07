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
                // 剩余秒数缺失（null/undefined）应视为“最不紧迫”，避免 Number(null) 被当成 0。
                const toSortableRemaining = (value) => {
                    if (value === null || value === undefined || value === '') return Number.MAX_SAFE_INTEGER;
                    const numeric = Number(value);
                    return Number.isFinite(numeric) ? numeric : Number.MAX_SAFE_INTEGER;
                };
                const currentRemaining = toSortableRemaining(card.remaining_seconds);
                const existingRemaining = toSortableRemaining(existing.remaining_seconds);
                if (currentRemaining < existingRemaining) {
                    merged.set(key, card);
                }
            }
        });

        return Array.from(merged.values());
    }

    // 把剩余秒数拆成「数字 + 单位」片段，便于前端把数字放大、单位缩小排版。
    function splitDurationTokens(totalSeconds, maxUnits) {
        const limit = Number(maxUnits) || 3;
        const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        const units = [
            { label: '天', size: 86400 },
            { label: '小时', size: 3600 },
            { label: '分', size: 60 },
            { label: '秒', size: 1 },
        ];

        const tokens = [];
        let rest = seconds;
        units.forEach((unit, index) => {
            if (tokens.length >= limit) return;
            const value = Math.floor(rest / unit.size);
            rest -= value * unit.size;
            const isLastUnit = index === units.length - 1;
            // 前导零单位（如 0天0小时）不展示，但秒必须有值，避免出现空倒计时。
            if (value > 0 || tokens.length > 0 || isLastUnit) {
                tokens.push({
                    value: tokens.length > 0 ? String(value).padStart(2, '0') : String(value),
                    unit: unit.label,
                });
            }
        });

        return tokens.slice(0, limit);
    }


    // 计时窗口的来源配置：字段名严格对齐配置页，数值直接复用窗口秒数。
    function resolveTriggerSpec(isGroupSilence, windowSeconds) {
        return {
            configLabel: isGroupSilence ? '群聊沉默触发时间' : '自动触发等待时间',
            configValue: windowSeconds > 0
                ? window.formatDuration(windowSeconds, { compact: true, maxUnits: 3 })
                : '未配置',
        };
    }

    // 未回复计数说明：对齐「最大未回复次数上限」配置项，0 表示不限制。
    function resolveUnansweredSpec(timer) {
        const current = Math.max(0, Number(timer.unanswered_count) || 0);
        const max = Math.max(0, Number(timer.max_unanswered_times) || 0);
        return {
            current: current,
            max: max,
            isLimited: max > 0,
            isWarning: current > 0,
            valueText: max > 0 ? `${current} / ${max} 次` : `${current} 次`,
            hintText: max > 0
                ? `连续 ${max} 次主动消息无人回复后会暂停发送，当前已累计 ${current} 次。`
                : '未设置最大未回复次数上限，会持续主动发送。',
        };
    }

    // 状态页计时器卡片：把后端字段换算成可直接渲染的文案与样式字段。
    function resolveStatusTimerCard(timer, nowMs, displayTimezone, snapshotAtMs, snapshotServerMs) {
        const targetTime = window.TimeDomain.toDate(timer.target_time);
        const remainingSeconds = window.TimeDomain.resolveTimerRemainingSeconds(timer, nowMs, snapshotAtMs, snapshotServerMs);
        // 只要剩余秒数可推导即可展示倒计时，target_time 仅用于展示目标时间文本。
        const hasLiveRemaining = remainingSeconds !== null && Number.isFinite(remainingSeconds);
        const windowSeconds = Math.max(0, Number(timer.window_seconds !== undefined ? timer.window_seconds : 0));

        // 有总窗口时用“窗口 - 实时剩余”推导进度，避免沿用后端快照值导致进度条长时间不动。
        const derivedProgress = hasLiveRemaining && windowSeconds > 0
            ? clampPercent(((windowSeconds - remainingSeconds) / windowSeconds) * 100)
            : null;
        const backendProgress = (timer.progress_percent !== undefined && timer.progress_percent !== null)
            ? Number(timer.progress_percent)
            : null;
        const progressPercent = derivedProgress !== null
            ? derivedProgress
            : clampPercent(Number.isFinite(backendProgress) ? backendProgress : 0);

        const statusMeta = resolveProgressStatus(
            hasLiveRemaining ? remainingSeconds : null,
            {
                future: '稳定运行',
                soon: '正常计时',
                urgent: '即将结束',
                expired: '等待刷新',
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
            : (isGroupSession ? '群聊自动触发' : '私聊自动触发');

        const session = window.SessionDomain.resolveSessionDisplay({
            session: timer.session_id,
            session_name: timer.session_display_name || timer.session_name,
        });

        const triggerSpec = resolveTriggerSpec(isGroupSilence, windowSeconds);
        const unansweredSpec = resolveUnansweredSpec(timer);
        const personaDisplay = window.resolvePersonaDisplay(timer.persona);

        // 已等待时长由窗口与剩余时间反推，比裸露的“目标时间”更贴近用户感受。
        const elapsedSeconds = windowSeconds > 0 && hasLiveRemaining
            ? Math.max(0, windowSeconds - remainingSeconds)
            : 0;

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
            sessionDisplayName: session.displayName,
            sessionSubText: session.subText,
            hasAlias: session.hasAlias,
            umoText: session.sessionId || '--',
            // 倒计时文案：主数字 + 说明。
            countdownTokens: hasLiveRemaining && remainingSeconds > 0
                ? splitDurationTokens(remainingSeconds, 3)
                : [],
            heroLabel: hasLiveRemaining && remainingSeconds > 0
                ? '距离创建待执行的主动消息任务还有'
                : '当前计时状态',
            progressLabel: `已等待 ${progressPercent}%`,
            elapsedSeconds: elapsedSeconds,
            elapsedText: window.formatDuration(elapsedSeconds, { compact: true, maxUnits: 3 }),
            // 预计创建时间只给时分秒，日期放在 title 里避免占宽。
            targetClockText: targetTime
                ? window.formatDateTime(targetTime, displayTimezone, { includeYear: false, includeSeconds: true }).trim()
                : '--',
            targetText: targetTime
                ? window.formatDateTime(targetTime, displayTimezone, { includeYear: true, includeSeconds: true })
                : '--',
            triggerConfigLabel: triggerSpec.configLabel,
            triggerConfigValue: triggerSpec.configValue,
            unansweredValueText: unansweredSpec.valueText,
            unansweredHintText: unansweredSpec.hintText,
            unansweredIsLimited: unansweredSpec.isLimited,
            unansweredIsWarning: unansweredSpec.isWarning,
            sourceModeLabel: window.resolveSourceModeLabel(timer.source_mode),
            personaText: personaDisplay.name,
            personaSourceText: personaDisplay.sourceLabel || '',
            personaState: personaDisplay.state,
            personaTitle: personaDisplay.title,
        });
    }

    // 任务页卡片展示数据：与计时器卡片复用同一套三段式结构，文案口径保持一致。
    function resolveScheduledCard(job, nowMs, displayTimezone) {
        // 复用计时卡片的状态、倒计时与进度推导，避免两处阈值不一致。
        const progress = resolveTaskProgress(job, nowMs, displayTimezone);
        const session = window.SessionDomain.resolveSessionDisplay(job);
        const personaDisplay = window.resolvePersonaDisplay(job.persona);
        const unansweredSpec = resolveUnansweredSpec(job);
        const remainingSeconds = Number(progress.remainingSeconds) || 0;
        const progressPercent = clampPercent(progress.progressPercent);

        // 已等待时长 = 调度窗口（随机区间） - 剩余时间，比裸露的发送时刻更直观。
        const windowSeconds = Math.max(0, Number(job.last_schedule_random_interval_seconds) || 0);
        const elapsedSeconds = windowSeconds > 0
            ? Math.max(0, windowSeconds - remainingSeconds)
            : 0;

        // 调度间隔取配置项口径，与配置页「最小/最大间隔」文案对齐。
        const minMinutes = Number(job.schedule_min_interval_minutes) || 0;
        const maxMinutes = Number(job.schedule_max_interval_minutes) || 0;
        const intervalText = (minMinutes > 0 && maxMinutes > 0)
            ? `${minMinutes} - ${maxMinutes} 分钟`
            : (minMinutes > 0 ? `${minMinutes} 分钟` : (maxMinutes > 0 ? `${maxMinutes} 分钟` : '未配置'));

        // 免打扰支持多时段：拆分后逐段格式化，非法片段原样展示便于排查配置错误。
        // 开关关闭时统一显示「未启用」，避免与时段配置产生歧义。
        const quietEnabled = job.quiet_hours_enabled !== false;
        const quietRaw = String(job.quiet_hours || '').trim();
        const formatQuietRange = (part) => {
            const text = String(part || '').trim();
            const matched = text.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
            if (!matched) return text;
            const startHour = Number(matched[1]);
            const endHour = Number(matched[2]);
            if (startHour < 0 || startHour > 23 || endHour < 0 || endHour > 24) return text;
            return `${String(startHour).padStart(2, '0')}:00 - ${String(endHour).padStart(2, '0')}:00`;
        };
        let quietText = '未启用';
        if (quietEnabled) {
            const parts = quietRaw.split(/[,，、]/).map((item) => item.trim()).filter(Boolean);
            quietText = parts.length > 0 ? parts.map(formatQuietRange).join(' / ') : '未配置';
        }

        return Object.assign({}, job, {
            status: progress.status,
            statusLabel: progress.statusLabel,
            progressPercent: progressPercent,
            progressLabel: `已等待 ${progressPercent}%`,
            sessionDisplayName: session.displayName,
            umoText: session.sessionId || '--',
            // 倒计时正文：主数字 + 单位，与计时器卡片完全一致。
            countdownTokens: remainingSeconds > 0
                ? splitDurationTokens(remainingSeconds, 3)
                : [],
            // 任务卡是「待发送」，因此 kicker 用发送语义而非创建语义。
            heroLabel: remainingSeconds > 0 ? '距离发送这次主动消息还有' : '当前发送状态',
            elapsedSeconds: elapsedSeconds,
            elapsedText: window.formatDuration(elapsedSeconds, { compact: true, maxUnits: 3 }),
            executeClockText: progress.nextRun
                ? window.formatDateTime(progress.nextRun, displayTimezone, { includeYear: false, includeSeconds: true }).trim()
                : '--',
            executeFullText: progress.nextRun
                ? window.formatDateTime(progress.nextRun, displayTimezone, { includeYear: true, includeSeconds: true })
                : '--',
            sourceModeLabel: window.resolveSourceModeLabel(job.source_mode),
            personaText: personaDisplay.name,
            personaSourceText: personaDisplay.sourceLabel || '',
            personaState: personaDisplay.state,
            personaTitle: personaDisplay.title,
            unansweredValueText: unansweredSpec.valueText,
            unansweredHintText: unansweredSpec.hintText,
            unansweredIsLimited: unansweredSpec.isLimited,
            unansweredIsWarning: unansweredSpec.isWarning,
            intervalText: intervalText,
            quietText: quietText,
        });
    }

    // 任务页卡片：推导下一次发送时间、倒计时、状态与进度。
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
                countdownText: '暂无有效发送时间',
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
            countdownText: remainingSeconds > 0 ? `${remainingText} 后发送` : '等待下一轮刷新确认',
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
        resolveScheduledCard: resolveScheduledCard,
        resolveTaskProgress: resolveTaskProgress,
    });
})();
