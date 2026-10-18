(() => {
/**
 * 文件职责：配置页视图容器，负责承载配置页头部结构并挂载配置编辑器。
 */

 const { Box, Typography } = MaterialUI;

function ConfigView() {
    return (
        // 视图层只负责页面框架与标题，配置编辑逻辑由 ConfigEditor 承载。
        <Box className="config-view-shell">
            <div className="card config-card">
                <div className="config-header">
                    <div className="config-header-title">
                        {/* 左侧彩色竖条用于强化“当前处于配置管理页”的视觉识别。 */}
                        <div className="config-header-accent"></div>
                        <Typography variant="h6" sx={{ fontWeight: 800, letterSpacing: '-0.5px' }}>配置管理</Typography>
                    </div>
                </div>
                {/* 动态 Schema 解析、草稿缓存、保存与会话差异逻辑都由 ConfigEditor 负责。 */}
                <ConfigEditor />
            </div>
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按 currentView 渲染。
window.ConfigView = ConfigView;
})();
