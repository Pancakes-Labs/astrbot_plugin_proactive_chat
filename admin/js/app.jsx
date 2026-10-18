/**
 * 文件职责：前端应用入口，负责首屏数据加载、实时同步、视图装配与根节点挂载。
 */

// 视图级错误边界：单个页面崩溃时只影响自身区域，避免整站白屏。
class ViewErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { error: null };
    }

    static getDerivedStateFromError(error) {
        return { error: error };
    }

    componentDidCatch(error, info) {
        // 保留完整堆栈到控制台，便于定位具体组件。
        console.error('视图渲染失败', this.props.view, error, info && info.componentStack);
    }

    componentDidUpdate(prevProps) {
        // 切换视图时清除上一页的错误，避免错误状态跨页残留。
        if (prevProps.view !== this.props.view && this.state.error) {
            this.setState({ error: null });
        }
    }

    render() {
        if (this.state.error) {
            const err = this.state.error;
            return (
                <div className="card app-error-card" style={{ whiteSpace: 'pre-wrap' }}>
                    <div style={{ fontWeight: 800, marginBottom: 8 }}>
                        该页面渲染失败，其余页面仍可正常使用
                    </div>
                    <div style={{ marginBottom: 8 }}>{String(err && err.message ? err.message : err)}</div>
                    <div className="mono" style={{ opacity: 0.75, fontSize: '12px' }}>
                        {String((err && err.stack) || '').split('\n').slice(0, 8).join('\n')}
                    </div>
                </div>
            );
        }
        return this.props.children;
    }
}

function App() {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    const themeInitializedRef = React.useRef(false);
    const mainContentRef = React.useRef(null);
    // WebSocket 连接状态在回调中读取，避免把 effect 依赖拖成每秒重建。
    const wsConnectedRef = React.useRef(false);

    const loadAll = React.useCallback(async () => {
        // 首次进入页面或手动全量刷新时，统一拉取首页所需的全部关键数据。
        dispatch({ type: 'SET_LOADING', payload: true });
        dispatch({ type: 'SET_ERROR', payload: '' });
        try {
            // 并发请求状态、会话、配置、任务与通知，减少首屏等待时间。
            const [statusRes, sessionsRes, configRes, jobsRes, notificationsRes] = await Promise.all([
                api.getStatus(),
                api.listSessions(),
                api.getConfig(),
                api.listJobs(),
                api.getNotifications(),
            ]);
            dispatch({ type: 'SET_STATUS', payload: statusRes });
            dispatch({ type: 'SET_SESSIONS', payload: window.SessionDomain.normalizeSessionList(sessionsRes.sessions) });
            dispatch({ type: 'SET_CONFIG', payload: configRes || null });
            dispatch({ type: 'SET_JOBS', payload: jobsRes.jobs || [] });
            dispatch({ type: 'SET_NOTIFICATIONS', payload: notificationsRes.items || [] });
            dispatch({ type: 'SET_NOTIFICATIONS_META', payload: notificationsRes.meta || null });
        } catch (e) {
            // 将后端错误或网络错误统一透传到顶部错误卡片中展示。
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载失败' });
        } finally {
            dispatch({ type: 'SET_LOADING', payload: false });
        }
    }, [api, dispatch]);

    const loadRealtime = React.useCallback(async () => {
        try {
            // 兜底轮询只更新变化频率最高的状态与任务列表，避免重载完整配置。
            const [statusRes, jobsRes] = await Promise.all([
                api.getStatus(),
                api.listJobs(),
            ]);
            dispatch({ type: 'SET_STATUS', payload: statusRes });
            dispatch({ type: 'SET_JOBS', payload: jobsRes.jobs || [] });
        } catch (e) {
            // 兜底轮询失败不打断主界面；实时信息短暂过期比整页报错更友好。
        }
    }, [api, dispatch]);

    React.useEffect(() => {
        // 若入口页仍在等待鉴权预检查，则挂起首次加载，等 auth-ready 事件触发后再开始。
        if (window.__PROACTIVE_AUTH_PENDING) {
            const onReady = () => loadAll();
            window.addEventListener('auth-ready', onReady);
            return () => window.removeEventListener('auth-ready', onReady);
        }
        loadAll();
    }, [loadAll]);

    const handleRealtimeData = React.useCallback(
        (data) => {
            // WebSocket 推送只负责把增量/全量快照写回全局状态，不在这里做业务判断。
            if (data.status) dispatch({ type: 'SET_STATUS', payload: data.status });
            if (Array.isArray(data.jobs)) dispatch({ type: 'SET_JOBS', payload: data.jobs });
            if (data.notifications) {
                dispatch({ type: 'SET_NOTIFICATIONS', payload: data.notifications.items || [] });
                dispatch({ type: 'SET_NOTIFICATIONS_META', payload: data.notifications.meta || null });
            }
            if (data.notificationsMeta) {
                dispatch({ type: 'SET_NOTIFICATIONS_META', payload: data.notificationsMeta || null });
            }
            if (Array.isArray(data.sessions)) {
                // 后端可能返回字符串数组，也可能返回对象数组，统一标准化结构。
                dispatch({ type: 'SET_SESSIONS', payload: window.SessionDomain.normalizeSessionList(data.sessions) });
            }
        },
        [dispatch]
    );

    const wsConnected = useWebSocket(handleRealtimeData);

    React.useEffect(() => {
        wsConnectedRef.current = Boolean(wsConnected);
    }, [wsConnected]);

    React.useEffect(() => {
        // 实时数据以 WebSocket 为主通道；仅在断线时用较高频率兜底。
        if (window.__PROACTIVE_AUTH_PENDING) return;

        let disposed = false;
        let timer = null;

        const schedule = () => {
            if (disposed) return;
            const interval = wsConnectedRef.current ? 30000 : 3000;
            timer = window.setTimeout(async () => {
                if (disposed) return;
                // 页面隐藏时跳过本轮请求，减少后台标签页的无意义流量。
                if (document.visibilityState !== 'hidden') {
                    await loadRealtime();
                }
                schedule();
            }, interval);
        };

        schedule();
        return () => {
            disposed = true;
            window.clearTimeout(timer);
        };
    }, [loadRealtime, wsConnected]);

    React.useEffect(() => {
        if (window.__PROACTIVE_AUTH_PENDING) return;
        // 兼容旧版启动节点：一旦状态已返回，就主动隐藏遗留的 boot 元素。
        const boot = document.getElementById('boot');
        if (boot) boot.style.display = 'none';
    }, [state.status]);

    // 主内容区滚动位置：与配置页共用同一套恢复实现与键规则。
    useScrollRestore(mainContentRef, window.ScrollDomain.buildScrollKey(`view_${state.currentView}`), {
        enabled: !window.__PROACTIVE_AUTH_PENDING,
    });

    const renderView = () => {
        // 未识别视图时回退到状态页，避免出现空白主区域。
        switch (state.currentView) {
            case 'status':
                return <StatusView onRefresh={loadAll} />;
            case 'config':
                return <ConfigView onRefresh={loadAll} />;
            case 'tasks':
                return <TasksView onRefresh={loadAll} />;
            case 'notifications':
                return <NotificationsView onRefresh={loadAll} />;
            case 'docs':
                return <MarkdownDocsView />;
            default:
                return <StatusView onRefresh={loadAll} />;
        }
    };

    React.useEffect(() => {
        // 暗色模式变量只在 html.theme-dark 上定义，body 类名仅用于组件级覆盖规则。
        const root = document.documentElement;
        const body = document.body;
        const isDark = state.theme === 'dark';

        root.classList.toggle('theme-dark', isDark);
        body.classList.toggle('dark-theme', isDark);

        // 首次挂载不加过渡，避免首屏闪动；后续主题切换时才短暂打开统一过渡。
        if (!themeInitializedRef.current) {
            themeInitializedRef.current = true;
            return undefined;
        }

        root.classList.add('theme-transitioning');

        const computedStyle = window.getComputedStyle(root);
        const themeTransitionVar = computedStyle
            .getPropertyValue('--theme-transition-duration')
            .trim();
        const interactiveTransitionVar = computedStyle
            .getPropertyValue('--interactive-transition-duration')
            .trim();

        let themeTransitionDuration = Number.parseFloat(themeTransitionVar);
        let interactiveTransitionDuration = Number.parseFloat(interactiveTransitionVar);

        if (Number.isNaN(themeTransitionDuration)) {
            themeTransitionDuration = 220;
        }
        if (Number.isNaN(interactiveTransitionDuration)) {
            interactiveTransitionDuration = themeTransitionDuration;
        }

        const transitionDuration = Math.max(
            themeTransitionDuration,
            interactiveTransitionDuration
        );

        const timer = window.setTimeout(() => {
            root.classList.remove('theme-transitioning');
        }, transitionDuration);

        return () => {
            window.clearTimeout(timer);
            root.classList.remove('theme-transitioning');
        };
    }, [state.theme]);

    return (
        <div className="app">
            <Sidebar
                currentView={state.currentView}
                // 侧边栏只负责发出“切换视图”的意图，真正状态写入仍走 reducer。
                onChange={(view) => dispatch({ type: 'SET_VIEW', payload: view })}
            />
            <div className="main-wrapper">
                <Header currentView={state.currentView} wsConnected={wsConnected} />
                <div className={`main-content ${state.currentView === 'config' ? 'is-config-view' : ''}`} ref={mainContentRef}>
                    {/* 顶部错误条统一展示最近一次加载 / 操作失败的消息。 */}
                    {state.error ? <div className="card app-error-card">错误：{state.error}</div> : null}
                    {/* 错误边界包住视图本体，单页异常只会替换这一块内容。 */}
                    <ViewErrorBoundary view={state.currentView}>
                        {renderView()}
                    </ViewErrorBoundary>
                </div>
            </div>
        </div>
    );
}

function ThemedAppShell() {
    const { state } = useAppContext();

    // 色板统一由 ThemeTokens 维护；令牌文件缺失时回退到最小主题，避免整页崩溃。
    const muiTheme = React.useMemo(() => {
        if (window.ThemeTokens && typeof window.ThemeTokens.buildMuiTheme === 'function') {
            return window.ThemeTokens.buildMuiTheme(MaterialUI, state.theme === 'dark');
        }
        return MaterialUI.createTheme({ palette: { mode: state.theme === 'dark' ? 'dark' : 'light' } });
    }, [state.theme]);

    return (
        <MaterialUI.ThemeProvider theme={muiTheme}>
            <App />
        </MaterialUI.ThemeProvider>
    );
}

function AuthWrapper() {
    // ready 初值根据启动页鉴权状态决定，避免无鉴权场景下多等一次事件。
    const [ready, setReady] = React.useState(() => !window.__PROACTIVE_AUTH_PENDING);

    React.useEffect(() => {
        if (ready) return undefined;
        // 等待入口页完成鉴权预检查后，再真正挂载 React 应用。
        const onReady = () => setReady(true);
        window.addEventListener('auth-ready', onReady);
        return () => window.removeEventListener('auth-ready', onReady);
    }, [ready]);

    if (!ready) return null;

    return (
        <AppProvider>
            {/* 根级错误边界：即使整个应用初始化失败，也要把错误画到界面上。 */}
            <ViewErrorBoundary view="app">
                <ThemedAppShell />
            </ViewErrorBoundary>
        </AppProvider>
    );
}

if (!window.__PROACTIVE_WEBUI_INITIALIZED) {
    // 防止脚本重复执行时反复 createRoot，避免 React 在同一节点重复挂载。
    window.__PROACTIVE_WEBUI_INITIALIZED = true;
    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(<AuthWrapper />);
}
