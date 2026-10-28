(() => {
/**
 * 文件职责：通知中心视图，负责通知列表展示、已读状态操作与手动刷新交互。
 */

 const { Box, Typography, Button, Chip } = MaterialUI;

const NOTIFICATION_TYPE_META = {
    // 不同类型通知映射到不同的 UI 标签与颜色语义，便于用户快速区分内容性质。
    UPDATE: { label: '更新通知', color: 'info' },
    BUGFIX: { label: '修复通知', color: 'success' },
    NOTICE: { label: '公告通知', color: 'warning' },
    SECURITY: { label: '安全通知', color: 'error' },
    TEST: { label: '测试通知', color: 'default' },
};

const NOTIFICATION_TYPE_ALIASES = {
    // 兼容后端可能输出的别名类型，统一归并后再进入主映射表。
    INFO: 'NOTICE',
};

function resolveNotificationTypeMeta(type) {
    const normalizedType = String(type || '').trim().toUpperCase();
    const canonicalType = NOTIFICATION_TYPE_ALIASES[normalizedType] || normalizedType;
    // 即使类型未知，也返回兜底标签，避免前端因为类型缺失出现空白 chip。
    return NOTIFICATION_TYPE_META[canonicalType] || { label: '其他通知', color: 'default' };
}

// 通知正文渲染拆分到独立组件，并用 useMemo 缓存昂贵的 Markdown 解析结果。
const NotificationContent = React.memo(function NotificationContent({ item }) {
    const content = window.MarkdownRenderUtil.normalizeMarkdownContent(item && item.content ? item.content : '--');
    const format = String((item && item.content_format) || 'text').trim().toLowerCase();
    const shouldRenderMarkdown = format === 'markdown' || window.MarkdownRenderUtil.isProbablyMarkdown(content);

    const renderedHtml = React.useMemo(
        () => (shouldRenderMarkdown ? window.MarkdownRenderUtil.renderMarkdownToHtml(content) : ''),
        [content, shouldRenderMarkdown]
    );

    if (!shouldRenderMarkdown) {
        return (
            <Typography
                variant="body2"
                className="task-countdown-text notification-feed-content-text notification-feed-content-text-plain"
                sx={{ whiteSpace: 'pre-wrap', lineHeight: 1.85 }}
            >
                {content}
            </Typography>
        );
    }

    return (
        <Box
            className="task-countdown-text notification-feed-content-text notification-md"
            // Markdown HTML 已在公共工具中做过 sanitize，这里只负责挂载结果。
            dangerouslySetInnerHTML={{ __html: renderedHtml }}
        />
    );
});

function NotificationsView({ onRefresh }) {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    // 兜底保证渲染层始终拿到数组，避免 map 时出现空值异常。
    const notifications = Array.isArray(state.notifications) ? state.notifications : [];
    const notificationsMeta = state.notificationsMeta || { unread_count: 0, last_sync_at: '', total_count: 0 };
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';
    // busyAction 用单字符串标记当前进行中的动作，便于按钮粒度控制禁用状态。
    const [busyAction, setBusyAction] = React.useState('');

    const refreshFromServer = async () => {
        setBusyAction('refresh');
        try {
            const payload = await api.refreshNotifications();
            // 先写回刷新接口直接返回的最新通知快照，再触发上层全量刷新补齐其它区域状态。
            dispatch({ type: 'SET_NOTIFICATIONS', payload: payload.items || [] });
            dispatch({ type: 'SET_NOTIFICATIONS_META', payload: payload.meta || null });
            await onRefresh();
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '刷新通知失败' });
        } finally {
            setBusyAction('');
        }
    };

    const markOneAsRead = async (id) => {
        setBusyAction(`read-${id}`);
        try {
            const payload = await api.readNotification(id);
            dispatch({ type: 'SET_NOTIFICATIONS_META', payload: payload.meta || null });
            dispatch({
                type: 'SET_NOTIFICATIONS',
                // 单条已读时直接在本地列表上做映射更新，避免为了一个按钮再整页重载。
                payload: notifications.map((item) =>
                    Number(item.id) === Number(id) ? { ...item, _read: true } : item
                ),
            });
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '标记已读失败' });
        } finally {
            setBusyAction('');
        }
    };

    const markAllAsRead = async () => {
        setBusyAction('read-all');
        try {
            const payload = await api.readAllNotifications();
            dispatch({ type: 'SET_NOTIFICATIONS_META', payload: payload.meta || null });
            dispatch({
                type: 'SET_NOTIFICATIONS',
                // 全部已读时直接把当前列表统一标成已读，保证交互即时反馈。
                payload: notifications.map((item) => ({ ...item, _read: true })),
            });
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '全部已读失败' });
        } finally {
            setBusyAction('');
        }
    };

    const unreadCount = Number(notificationsMeta.unread_count !== undefined ? notificationsMeta.unread_count : 0);

    return (
        <Box className="notifications-view">
            <div className="card notifications-hero-card">
                <Box className="tasks-header-row notifications-header-row">
                    <div className="notifications-header-main">
                        <Box className="notifications-title-row">
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexWrap: 'wrap' }}>
                                <Typography variant="h6" sx={{ fontWeight: 800 }}>
                                    {`通知中心 (当前共 ${notificationsMeta.total_count || notifications.length} 条通知)`}
                                </Typography>
                                <Box className={`notification-unread-pill ${unreadCount > 0 ? 'is-unread' : 'is-clear'}`}>
                                    {unreadCount > 0 ? `未读 ${unreadCount} 条` : '全部已读'}
                                </Box>
                            </Box>
                            <div className="notifications-inline-meta">
                                <div className="notifications-inline-meta-item is-pill">
                                    <span className="notifications-inline-meta-label">未读通知</span>
                                    <strong>{unreadCount}</strong>
                                </div>
                                <div className="notifications-inline-meta-item is-pill">
                                    <span className="notifications-inline-meta-label">上次同步</span>
                                    <strong>
                                        {notificationsMeta.last_sync_at
                                            ? formatDateTime(notificationsMeta.last_sync_at, displayTimezone, { includeYear: true, includeSeconds: true })
                                            : '--'}
                                    </strong>
                                </div>
                            </div>
                        </Box>
                        <Typography variant="body2" className="tasks-header-subtitle">
                            用于接收插件更新、修复说明、注意事项等官方通知。
                        </Typography>
                    </div>
                    <Box className="notifications-actions-row">
                        <Button
                            variant="outlined"
                            onClick={markAllAsRead}
                            disabled={busyAction === 'read-all' || notifications.length === 0}
                            sx={{ borderRadius: 3 }}
                        >
                            全部已读
                        </Button>
                        <Button
                            variant="contained"
                            onClick={refreshFromServer}
                            disabled={busyAction === 'refresh'}
                            startIcon={<span>🔄</span>}
                            sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}
                        >
                            立即同步
                        </Button>
                    </Box>
                </Box>
            </div>

            {notifications.length === 0 ? (
                <div className="card tasks-empty-card notifications-empty-card">
                    <div className="tasks-empty-icon">🔔</div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>暂无通知</Typography>
                    <Typography variant="body1" color="text.secondary">
                        当前还没有可展示的系统通知，后续若有插件更新、修复说明或注意事项，这里会自动出现。
                    </Typography>
                </div>
            ) : (
                <div className="notifications-feed-list">
                    {notifications.map((item) => {
                        const meta = resolveNotificationTypeMeta(item.type);
                        const isRead = Boolean(item._read) || false;
                        return (
                            <div className={`card task-card-enhanced notification-feed-card ${isRead ? 'is-read' : 'is-urgent'}`} key={item.id}>
                                <div className="task-card-top notification-feed-card-top">
                                    <div className="notification-feed-heading-group">
                                        <Typography variant="body1" className="task-card-session is-primary notification-feed-title">
                                            {item.title || '--'}
                                        </Typography>
                                        <Chip
                                            className="notification-feed-type-chip"
                                            label={meta.label}
                                            size="small"
                                            color={meta.color}
                                            variant="outlined"
                                        />
                                    </div>
                                    <div className="notification-feed-side notification-feed-meta-inline">
                                        <span className={`notification-feed-status-pill ${isRead ? 'is-read' : 'is-unread'}`}>
                                            {isRead ? '已读' : '未读'}
                                        </span>
                                        <Typography variant="caption" className="task-card-session-sub mono notification-feed-time">
                                            {formatDateTime(item.created_at, displayTimezone, { includeYear: true, includeSeconds: true })}
                                        </Typography>
                                    </div>
                                </div>

                                <div className="task-next-run-panel notification-feed-content-panel">
                                    <NotificationContent item={item} />
                                    <Box className="notification-feed-actions notification-feed-actions-inside">
                                        <Button
                                            variant="outlined"
                                            size="small"
                                            disabled={isRead || busyAction === `read-${item.id}`}
                                            onClick={() => markOneAsRead(item.id)}
                                            sx={{ borderRadius: 999, minWidth: 112, boxShadow: 'none' }}
                                        >
                                            {isRead ? '已读' : '确认已读'}
                                        </Button>
                                    </Box>
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </Box>
    );
}

window.NotificationsView = NotificationsView;
})();
