/**
 * 文件职责：API 客户端常量，集中维护后端接口路径与调用方式。
 *
 * HttpUtil 本身是全局单例，引用天然稳定，因此这里直接导出一个冻结对象，
 * 不再用大量 useCallback + 依赖数组去维持引用稳定性。
 */

(function () {
    const HttpUtil = window.HttpUtil;

    const ApiClient = Object.freeze({
        getStatus: () => HttpUtil.get('/api/status'),
        getConfig: () => HttpUtil.get('/api/config'),
        getConfigSchema: () => HttpUtil.get('/api/config-schema'),
        // 全局配置保存接口，传入完整 payload 由后端按分组白名单更新。
        updateConfig: (payload) => HttpUtil.post('/api/config', payload),

        // 会话列表用于会话差异配置页与主应用首载。
        listSessions: () => HttpUtil.get('/api/session-config/sessions'),
        // 会话 ID 可能包含特殊字符，路径参数必须先 encodeURIComponent。
        getSessionConfig: (session) => HttpUtil.get(`/api/session-config/${encodeURIComponent(session)}`),
        // 同一接口同时支持 override 与 effective 两种保存模式，由 payload.mode 区分。
        updateSessionConfig: (session, payload) => HttpUtil.post(`/api/session-config/${encodeURIComponent(session)}`, payload),
        resetSessionConfig: (session) => HttpUtil.del(`/api/session-config/${encodeURIComponent(session)}`),

        listJobs: () => HttpUtil.get('/api/jobs'),
        listMarkdownFiles: () => HttpUtil.get('/api/markdown-files'),
        getMarkdownFile: (path) => HttpUtil.get(`/api/markdown-files/${encodeURIComponent(path)}`),
        getNotifications: () => HttpUtil.get('/api/notifications'),
        readNotification: (id) => HttpUtil.post('/api/notifications/read', { id }),
        readAllNotifications: () => HttpUtil.post('/api/notifications/read-all', {}),
        refreshNotifications: () => HttpUtil.post('/api/notifications/refresh', {}),
        // “立即触发”接口无参，body 传空对象保持请求格式统一。
        triggerJob: (session) => HttpUtil.post(`/api/jobs/${encodeURIComponent(session)}/trigger`, {}),
        cancelJob: (session) => HttpUtil.del(`/api/jobs/${encodeURIComponent(session)}`),
        rescheduleJob: (session) => HttpUtil.post(`/api/jobs/${encodeURIComponent(session)}/reschedule`, {}),

        // 运行日志：先取缓冲统计，再按需导出并上传生成分享链接。
        getLogStats: () => HttpUtil.get('/api/logs/stats'),
        uploadLogs: (count) => HttpUtil.post('/api/logs/upload', { count }),
    });

    // 兼容旧调用点：useApi() 直接返回同一个冻结对象，不再创建 Hook 内部状态。
    function useApi() {
        return ApiClient;
    }

    window.ApiClient = ApiClient;
    window.useApi = useApi;
})();
