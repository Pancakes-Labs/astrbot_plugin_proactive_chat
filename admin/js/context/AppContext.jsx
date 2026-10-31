/**
 * 文件职责：全局状态上下文，集中维护管理端共享状态与 reducer 分发逻辑。
 */

const { createContext, useContext, useReducer } = React;

// 管理端的全局状态树；由于项目未使用打包器，这里采用单文件 context 统一收口共享状态。
const initialState = {
    // 当前主内容区展示的视图 key。
    currentView: 'status',
    // 全局加载态，通常用于首屏或全量刷新。
    loading: false,
    // 最近一次操作失败信息，会显示在页面顶部错误卡片中。
    error: '',
    // 首页状态汇总数据，对应后端 /api/status。
    status: null,
    // 状态快照到达本地的时刻，用于把后端给出的整秒值继续在本地递增。
    statusReceivedAt: 0,
    // 已知会话列表，可来自 HTTP 首载或 WebSocket 推送。
    sessions: [],
    // 当前生效的全局配置快照，对应后端 /api/config。
    config: null,
    // 调度任务列表，对应后端 /api/jobs。
    jobs: [],
    // 通知列表与元信息，对应后端 /api/notifications。
    notifications: [],
    notificationsMeta: {
        unread_count: 0,
        last_sync_at: '',
        total_count: 0,
    },
    // Markdown 文档浏览页的文件目录、当前文档与选中路径。
    markdownFiles: [],
    markdownDocument: null,
    selectedMarkdownPath: '',
    // 会话详情页当前选中的会话 ID。
    selectedSession: '',
    // 当前选中会话的 base / override / effective 详情。
    sessionDetail: null,
    // 主题优先读取本地存储，确保刷新页面后仍能保持用户偏好。
    theme: localStorage.getItem('theme') || 'light',
};

function reducer(state, action) {
    // reducer 负责统一处理所有状态写入，避免各组件直接散写共享状态。
    switch (action.type) {
        case 'TOGGLE_THEME':
            // 主题切换时同步写入 localStorage，保证跨页面刷新可恢复。
            const newTheme = state.theme === 'light' ? 'dark' : 'light';
            localStorage.setItem('theme', newTheme);
            return { ...state, theme: newTheme };
        case 'SET_VIEW':
            return { ...state, currentView: action.payload };
        case 'SET_LOADING':
            return { ...state, loading: action.payload };
        case 'SET_ERROR':
            // 统一把 falsy 值收敛为空字符串，减少渲染层判断分支。
            return { ...state, error: action.payload || '' };
        case 'SET_STATUS':
            return { ...state, status: action.payload, statusReceivedAt: Date.now() };
        case 'SET_SESSIONS':
            return { ...state, sessions: action.payload || [] };
        case 'SET_CONFIG':
            return { ...state, config: action.payload || null };
        case 'SET_JOBS':
            return { ...state, jobs: action.payload || [] };
        case 'SET_NOTIFICATIONS':
            return { ...state, notifications: action.payload || [] };
        case 'SET_NOTIFICATIONS_META':
            return {
                ...state,
                notificationsMeta: action.payload || {
                    unread_count: 0,
                    last_sync_at: '',
                    total_count: 0,
                },
            };
        case 'SET_MARKDOWN_FILES':
            return { ...state, markdownFiles: action.payload || [] };
        case 'SET_MARKDOWN_DOCUMENT':
            return { ...state, markdownDocument: action.payload || null };
        case 'SET_SELECTED_MARKDOWN_PATH':
            return { ...state, selectedMarkdownPath: action.payload || '' };
        case 'SET_SELECTED_SESSION':
            return { ...state, selectedSession: action.payload || '' };
        case 'SET_SESSION_DETAIL':
            return { ...state, sessionDetail: action.payload || null };
        default:
            // 未识别 action 时保持原状态，避免误 dispatch 导致页面崩溃。
            return state;
    }
}

// 初始值为 null，强制消费方必须在 Provider 内部使用，避免静默使用默认值。
const AppContext = createContext(null);

function AppProvider({ children }) {
    const [state, dispatch] = useReducer(reducer, initialState);
    return (
        <AppContext.Provider value={{ state, dispatch }}>
            {children}
        </AppContext.Provider>
    );
}

function useAppContext() {
    const ctx = useContext(AppContext);
    // 这里主动抛错，方便在开发阶段尽早发现 Provider 包裹缺失。
    if (!ctx) throw new Error('useAppContext must be used within AppProvider');
    return ctx;
}

// 由于当前前端采用多文件 UMD / Babel 直接挂载方案，因此通过 window 暴露给其他脚本使用。
window.AppProvider = AppProvider;
window.useAppContext = useAppContext;

