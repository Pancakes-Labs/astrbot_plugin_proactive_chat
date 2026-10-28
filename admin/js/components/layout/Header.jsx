(() => {
/**
 * 文件职责：顶部栏组件，负责标题展示、时钟显示、连接状态指示与主题切换入口。
 */

 const { Box, Typography, IconButton } = MaterialUI;

function RealTimeClock({ timeZone }) {
    // 复用全局共享时钟，避免头部再单独维护一个每秒 interval。
    const nowMs = useNow();
    const timeStr = React.useMemo(
        () => formatDateTime(new Date(nowMs), timeZone || 'Asia/Shanghai', {
            includeYear: true,
            includeSeconds: true,
        }),
        // 秒级精度即可，按秒取整可以避免同一秒内重复格式化。
        [Math.floor(nowMs / 1000), timeZone]
    );

    return (
        <div className="header-clock-chip">
            <span className="header-clock-label">当前时间 🕒</span>
            <span className="header-clock-value">{timeStr}</span>
        </div>
    );
}

function Header({ currentView, wsConnected }) {
    const { state, dispatch } = useAppContext();
    const { config, status } = state;
    // 若配置中未单独指定展示时区，则默认按插件主要使用场景的东八区展示。
    const displayTimezone = config?.displayTimezone || 'Asia/Shanghai';

    const toggleTheme = () => {
        dispatch({ type: 'TOGGLE_THEME' });
    };

    // 视图 key 到标题文案的映射集中维护，避免 JSX 中散落条件判断。
    const viewTitles = {
        status: '运行状态',
        tasks: '任务管理',
        notifications: '通知中心',
        docs: '文档浏览',
        config: '配置管理',
    };

    // 优先使用前端 socket 实时状态，后端连接计数作为补充信息。
    const wsCount = Number(status?.ws_connections !== undefined ? status.ws_connections : 0);
    const connected = typeof wsConnected === 'boolean' ? wsConnected : wsCount > 0;

    return (
        <div className="top-bar">
            <Typography variant="h5" sx={{
                fontWeight: 800,
                color: 'text.primary',
                letterSpacing: '-0.5px'
            }}>
                {viewTitles[currentView] || viewTitles.status}
            </Typography>

            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                <RealTimeClock timeZone={displayTimezone} />

                {/* 连接状态胶囊用于快速反馈实时通道是否可用。 */}
                <div className={`connection-chip ${connected ? 'is-connected' : 'is-disconnected'}`}>
                    <div className="connection-chip-dot"></div>
                    <Typography variant="body2" className="connection-chip-text">
                        {connected ? '已连接' : '未连接'}
                    </Typography>
                </div>

                <IconButton
                    onClick={toggleTheme}
                    aria-label={state.theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
                    sx={{
                        width: 44,
                        height: 44,
                        background: 'var(--md-sys-color-surface)',
                        border: '1px solid var(--glass-border)',
                        boxShadow: 'var(--md-elevation-shadow)',
                        '&:hover': { background: 'var(--md-sys-color-surface-variant)' }
                    }}
                >
                    <span className="header-theme-icon">
                        {/* 图标语义与即将切换到的模式对应，帮助用户快速理解当前状态。 */}
                        {state.theme === 'dark' ? '🌞' : '🌙'}
                    </span>
                </IconButton>
            </Box>
        </div>
    );
}

// 暴露到全局，供入口应用直接渲染 Header。
window.Header = Header;
})();
