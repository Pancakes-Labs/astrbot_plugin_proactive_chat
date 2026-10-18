(() => {
/**
 * 文件职责：侧边导航组件，负责视图切换、目录快捷操作与仓库信息入口展示。
 */
 
 const { Box, Typography } = MaterialUI;

function SidebarActionButton({ icon, label, onClick, variant }) {
    return (
        <button className="btn sidebar-action-btn" onClick={onClick}>
            {/* 图标变体交给 CSS 控制，避免在 JSX 里散落偏移量。 */}
            <span className={`sidebar-action-icon${variant ? ` is-${variant}` : ''}`}>{icon}</span>
            {label}
        </button>
    );
}

function Sidebar({ currentView, onChange }) {
    const { state } = useAppContext();
    // 版本号来自全局状态的 status 快照；未加载前先展示占位文本。
    const version = state.status?.version || '...';
    const unreadNotificationCount = Math.max(0, Number(state.notificationsMeta?.unread_count ?? 0));
    const [logoSrc, setLogoSrc] = React.useState('/logo.png');

    React.useEffect(() => {
        // 管理端可能被挂载在不同层级路径，依次尝试多个 logo 地址以提高兼容性。
        const candidates = ['/logo.png', '../logo.png', 'logo.png'];
        let cancelled = false;

        async function resolveLogo() {
            for (const candidate of candidates) {
                try {
                    const response = await fetch(candidate, { method: 'HEAD' });
                    if (response.ok) {
                        if (!cancelled) setLogoSrc(candidate);
                        return;
                    }
                } catch (e) {
                    // 某个候选地址失败并不代表整体失败，继续尝试下一个即可。
                }
            }
        }

        resolveLogo();
        return () => {
            cancelled = true;
        };
    }, []);

    // 当前管理端开放的主导航项集中定义在这里，便于后续扩展新视图。
    const menus = [
        { key: 'status', label: '运行状态', icon: '📊' },
        { key: 'tasks', label: '任务管理', icon: '📋' },
        { key: 'notifications', label: '通知中心', icon: '🔔' },
        { key: 'docs', label: '文档浏览', icon: '📚' },
        { key: 'config', label: '配置管理', icon: '⚙️' },
    ];

    const openDirectory = async (path) => {
        try {
            // 通过后端调用本机资源管理器打开目录，前端无需感知具体平台命令。
            const response = await window.HttpUtil.post('/api/open-directory', { path });
            if (!response || response.ok === false) {
                // 读取后端可能传回来的错误描述与详细 message
                const errorTitle = response?.error || '打开目录失败';
                const errorMsg = response?.message ? `\n详情: ${response.message}` : '';
                throw new Error(`${errorTitle}${errorMsg}`);
            }
            
            // 可选：如果是 Docker 环境等虽然成功响应但并非打开了窗口的场景，给出提示
            if (response.message && response.message.includes('已在系统文件管理器中打开目录')) {
                // 正常打开，可以什么都不做，也可以给个轻提示
                console.log(response.message);
            }
        } catch (e) {
            const message = e?.message || '打开目录失败';
            window.alert(message);
        }
    };

    return (
        <div className="sidebar">
            <div className="sidebar-header">
                <img
                    src={logoSrc}
                    alt="Logo"
                    className="sidebar-logo-img"
                    onError={() => {
                        // 图片加载失败时再尝试回退到上一级相对路径，兼容静态资源引用差异。
                        if (logoSrc !== '../logo.png') setLogoSrc('../logo.png');
                    }}
                />
                <div>
                    <Typography variant="h6" sx={{ fontWeight: 800, lineHeight: 1.2, letterSpacing: '-0.5px' }}>
                        主动消息
                    </Typography>
                    <Typography variant="caption" sx={{ opacity: 0.6, display: 'block', mt: 0.5 }}>
                        Admin Console
                    </Typography>
                </div>
            </div>

            <Box sx={{ flex: 1, mt: 4 }} className="sidebar-menu">
                {menus.map((item) => {
                    const showUnreadBadge = item.key === 'notifications' && unreadNotificationCount > 0;
                    const unreadBadgeText = unreadNotificationCount > 99 ? '99+' : String(unreadNotificationCount);
                    return (
                        <button
                            key={item.key}
                            type="button"
                            className={`nav-item ${currentView === item.key ? 'active' : ''}`}
                            aria-current={currentView === item.key ? 'page' : undefined}
                            // 导航只上抛目标视图 key，具体状态更新由父组件统一处理。
                            onClick={() => onChange(item.key)}
                        >
                            <span className="sidebar-menu-icon">{item.icon}</span>
                            <Typography variant="body2" sx={{ fontWeight: 500 }}>
                                {item.label}
                            </Typography>
                            {showUnreadBadge ? (
                                <span className="sidebar-notification-badge" aria-label={`未读通知 ${unreadNotificationCount} 条`}>
                                    {unreadBadgeText}
                                </span>
                            ) : null}
                        </button>
                    );
                })}
            </Box>

            <Box sx={{ px: 2, pt: 0, pb: 0 }} className="sidebar-footer">
                <div className="sidebar-actions-panel">
                    <SidebarActionButton
                        icon="📂"
                        label="打开插件文件目录"
                        onClick={() => openDirectory('plugin')}
                    />
                    <SidebarActionButton
                        icon="🗃️"
                        label="打开插件数据目录"
                        onClick={() => openDirectory('data')}
                        variant="data"
                    />
                </div>

                <a
                    href="https://github.com/DBJD-CR/astrbot_plugin_proactive_chat"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="github-btn sidebar-github-card"
                    aria-label="打开项目 GitHub 仓库"
                >
                    <div className="sidebar-github-inner">
                        <svg
                            height="28"
                            width="28"
                            viewBox="0 0 16 16"
                            className="sidebar-github-icon"
                        >
                            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path>
                        </svg>

                        <div className="sidebar-github-texts">
                            <Typography variant="caption" className="sidebar-github-author">
                                @DBJD-CR
                            </Typography>
                            <Typography variant="caption" className="sidebar-github-meta">
                                🔧 (主动消息) {version}
                            </Typography>
                        </div>
                    </div>
                </a>
                <Typography variant="caption" sx={{ display: 'block', textAlign: 'center', mt: 1, opacity: 0.7, fontSize: '14px', fontWeight: 500 }}>
                    点个 Star 吧~ ⭐
                </Typography>
            </Box>
        </div>
    );
}

// 暴露到全局，供入口应用直接使用侧边栏组件。
window.Sidebar = Sidebar;
})();

