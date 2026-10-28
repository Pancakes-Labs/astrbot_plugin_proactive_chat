(() => {
/**
 * 文件职责：Markdown 文档浏览视图，负责文档目录展示、内容加载与原生 Markdown 阅读体验。
 */
 
 const { Box, Typography, Button, Chip } = MaterialUI;

function MarkdownDocsView() {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    const articleRef = React.useRef(null);
    // 从全局状态读取文档目录与当前选中文档，便于视图切换后仍能保留上下文。
    const markdownFiles = Array.isArray(state.markdownFiles) ? state.markdownFiles : [];
    const markdownDocument = state.markdownDocument || null;
    const selectedMarkdownPath = String(state.selectedMarkdownPath || '');
    // 文档目录和正文分别维护加载状态，这样可以实现更细粒度的按钮禁用与空态反馈。
    const [loadingList, setLoadingList] = React.useState(false);
    const [loadingDocument, setLoadingDocument] = React.useState(false);

    const loadMarkdownFiles = React.useCallback(async () => {
        setLoadingList(true);
        try {
            const payload = await api.listMarkdownFiles();
            const items = Array.isArray(payload?.items) ? payload.items : [];
            dispatch({ type: 'SET_MARKDOWN_FILES', payload: items });

            if (!selectedMarkdownPath && items.length > 0) {
                // 首次进入文档页时自动选中第一篇文档，避免右侧区域出现长时间空白。
                dispatch({ type: 'SET_SELECTED_MARKDOWN_PATH', payload: items[0].path || '' });
            }
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载 Markdown 文档列表失败' });
        } finally {
            setLoadingList(false);
        }
    }, [api, dispatch, selectedMarkdownPath]);

    const loadMarkdownDocument = React.useCallback(async (path) => {
        const normalizedPath = String(path || '').trim();
        if (!normalizedPath) {
            // 空路径时直接清空当前文档，避免请求出错后残留旧内容误导用户。
            dispatch({ type: 'SET_MARKDOWN_DOCUMENT', payload: null });
            return;
        }

        setLoadingDocument(true);
        try {
            const payload = await api.getMarkdownFile(normalizedPath);
            dispatch({ type: 'SET_MARKDOWN_DOCUMENT', payload: payload || null });
            dispatch({ type: 'SET_SELECTED_MARKDOWN_PATH', payload: normalizedPath });
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载 Markdown 文档失败' });
        } finally {
            setLoadingDocument(false);
        }
    }, [api, dispatch]);

    React.useEffect(() => {
        if (markdownFiles.length === 0) {
            // 仅在目录为空时自动拉取，避免每次渲染都重复请求列表。
            loadMarkdownFiles();
        }
    }, [loadMarkdownFiles, markdownFiles.length]);

    React.useEffect(() => {
        if (!selectedMarkdownPath && markdownFiles.length > 0) {
            // 若当前尚未有选中文档，则默认加载目录中的第一项。
            loadMarkdownDocument(markdownFiles[0].path || '');
            return;
        }

        if (
            selectedMarkdownPath
            && (!markdownDocument || String(markdownDocument.path || '') !== selectedMarkdownPath)
        ) {
            // 当选中路径变化，但正文尚未同步时，再补拉一次内容，保证左右两栏状态一致。
            loadMarkdownDocument(selectedMarkdownPath);
        }
    }, [loadMarkdownDocument, markdownDocument, markdownFiles, selectedMarkdownPath]);

    const handleRefreshCurrent = async () => {
        // 刷新当前文档时先刷新目录，再刷新正文，避免文件列表已经变化但正文仍指向旧路径。
        await loadMarkdownFiles();
        if (selectedMarkdownPath) {
            await loadMarkdownDocument(selectedMarkdownPath);
        }
    };

    const currentDocumentTitle = markdownDocument?.title
        || markdownFiles.find((item) => item.path === selectedMarkdownPath)?.title
        || 'Markdown 文档';
    const currentDocumentPath = markdownDocument?.path || selectedMarkdownPath;
    // 正文解析成本较高，仅在文档内容变化时重新渲染。
    const renderedHtml = React.useMemo(
        () => (markdownDocument?.content
            // 文档正文和通知正文共用同一套 MarkdownRenderUtil，确保渲染风格一致。
            ? window.MarkdownRenderUtil.renderMarkdownToHtml(markdownDocument.content)
            : ''),
        [markdownDocument?.content]
    );

    React.useEffect(() => {
        const articleEl = articleRef.current;
        const mermaid = window.mermaid;
        if (!articleEl) return;

        const mermaidBlocks = articleEl.querySelectorAll('.notification-md-mermaid[data-mermaid-source]');
        if (!mermaidBlocks.length) return;

        if (!mermaid || typeof mermaid.render !== 'function') {
            mermaidBlocks.forEach((block) => {
                block.classList.add('is-error');
            });
            return;
        }

        if (!window.__PROACTIVE_MERMAID_INITIALIZED && typeof mermaid.initialize === 'function') {
            mermaid.initialize({
                startOnLoad: false,
                securityLevel: 'strict',
                theme: state.theme === 'dark' ? 'dark' : 'default',
            });
            window.__PROACTIVE_MERMAID_INITIALIZED = true;
        }

        let disposed = false;
        const cleanupFns = [];

        const attachMermaidViewportControls = (block) => {
            const svg = block.querySelector('svg');
            if (!svg) return;

            const currentSvg = svg;
            const svgViewBox = currentSvg.viewBox?.baseVal;
            const fallbackWidth = Number(currentSvg.getAttribute('width')) || currentSvg.clientWidth || 1200;
            const fallbackHeight = Number(currentSvg.getAttribute('height')) || currentSvg.clientHeight || 800;
            const intrinsicWidth = svgViewBox && svgViewBox.width ? svgViewBox.width : fallbackWidth;
            const intrinsicHeight = svgViewBox && svgViewBox.height ? svgViewBox.height : fallbackHeight;

            currentSvg.removeAttribute('width');
            currentSvg.removeAttribute('height');
            currentSvg.style.width = `${intrinsicWidth}px`;
            currentSvg.style.height = `${intrinsicHeight}px`;
            currentSvg.style.maxWidth = 'none';
            currentSvg.style.maxHeight = 'none';

            const existingViewport = block.querySelector('.notification-md-mermaid-viewport');
            if (existingViewport) {
                existingViewport.remove();
            }
            const existingToolbar = block.parentElement?.querySelector('.notification-md-mermaid-toolbar');
            if (existingToolbar) {
                existingToolbar.remove();
            }

            const viewport = document.createElement('div');
            viewport.className = 'notification-md-mermaid-viewport';

            const canvas = document.createElement('div');
            canvas.className = 'notification-md-mermaid-canvas';
            canvas.style.width = `${intrinsicWidth}px`;
            canvas.style.height = `${intrinsicHeight}px`;
            canvas.appendChild(currentSvg);
            viewport.appendChild(canvas);
            block.appendChild(viewport);

            const toolbar = document.createElement('div');
            toolbar.className = 'notification-md-mermaid-toolbar';
            toolbar.innerHTML = [
                '<button type="button" class="notification-md-mermaid-tool-btn" data-action="zoom-in">＋</button>',
                '<button type="button" class="notification-md-mermaid-tool-btn" data-action="zoom-out">－</button>',
                '<button type="button" class="notification-md-mermaid-tool-btn" data-action="reset">重置</button>',
            ].join('');
            block.parentElement.insertBefore(toolbar, block);

            const stateRef = {
                scale: 1,
                dragging: false,
                pointerId: null,
                startX: 0,
                startY: 0,
                startScrollLeft: 0,
                startScrollTop: 0,
            };

            const clampScale = (value) => Math.min(12, Math.max(0.35, value));
            const getFitScale = () => {
                const viewportWidth = viewport.clientWidth || intrinsicWidth;
                const viewportHeight = viewport.clientHeight || intrinsicHeight;
                if (!viewportWidth || !viewportHeight || !intrinsicWidth || !intrinsicHeight) {
                    return 1;
                }
                const fitScale = Math.min(viewportWidth / intrinsicWidth, viewportHeight / intrinsicHeight, 1);
                return clampScale(Number(fitScale.toFixed(3)));
            };
            const applyScale = () => {
                canvas.style.width = `${intrinsicWidth * stateRef.scale}px`;
                canvas.style.height = `${intrinsicHeight * stateRef.scale}px`;
                currentSvg.style.width = '100%';
                currentSvg.style.height = '100%';

                requestAnimationFrame(() => {
                    const canPanX = viewport.scrollWidth - viewport.clientWidth > 2;
                    const canPanY = viewport.scrollHeight - viewport.clientHeight > 2;
                    viewport.classList.toggle('is-pannable', canPanX || canPanY);
                });
            };
            const centerViewport = () => {
                const targetScrollLeft = Math.max((viewport.scrollWidth - viewport.clientWidth) / 2, 0);
                const targetScrollTop = Math.max((viewport.scrollHeight - viewport.clientHeight) / 2, 0);
                viewport.scrollLeft = targetScrollLeft;
                viewport.scrollTop = targetScrollTop;
            };
            const resetTransform = () => {
                stateRef.scale = getFitScale();
                applyScale();
                requestAnimationFrame(() => {
                    centerViewport();
                });
            };
            const zoomBy = (delta, originX = null, originY = null) => {
                const prevScale = stateRef.scale;
                const nextScale = clampScale(Number((prevScale + delta).toFixed(3)));
                if (nextScale === prevScale) return;

                const viewportRect = viewport.getBoundingClientRect();
                const anchorClientX = originX === null ? viewportRect.left + viewportRect.width / 2 : originX;
                const anchorClientY = originY === null ? viewportRect.top + viewportRect.height / 2 : originY;
                const localX = anchorClientX - viewportRect.left;
                const localY = anchorClientY - viewportRect.top;
                const contentX = (viewport.scrollLeft + localX) / prevScale;
                const contentY = (viewport.scrollTop + localY) / prevScale;

                stateRef.scale = nextScale;
                applyScale();

                const nextScrollLeft = contentX * nextScale - localX;
                const nextScrollTop = contentY * nextScale - localY;
                viewport.scrollLeft = Math.max(0, nextScrollLeft);
                viewport.scrollTop = Math.max(0, nextScrollTop);

                if (stateRef.scale <= 1.001) {
                    centerViewport();
                }
            };

            const onToolbarClick = (event) => {
                const action = event.target?.getAttribute('data-action');
                if (!action) return;
                if (action === 'zoom-in') zoomBy(0.35);
                if (action === 'zoom-out') zoomBy(-0.35);
                if (action === 'reset') resetTransform();
            };

            const onWheel = (event) => {
                if (!(event.ctrlKey || event.metaKey)) return;
                event.preventDefault();
                zoomBy(event.deltaY < 0 ? 0.28 : -0.28, event.clientX, event.clientY);
            };

            const onPointerDown = (event) => {
                const canPanX = viewport.scrollWidth - viewport.clientWidth > 2;
                const canPanY = viewport.scrollHeight - viewport.clientHeight > 2;
                if (!canPanX && !canPanY) return;
                stateRef.dragging = true;
                stateRef.pointerId = event.pointerId;
                stateRef.startX = event.clientX;
                stateRef.startY = event.clientY;
                stateRef.startScrollLeft = viewport.scrollLeft;
                stateRef.startScrollTop = viewport.scrollTop;
                viewport.classList.add('is-dragging');
                if (typeof viewport.setPointerCapture === 'function') {
                    viewport.setPointerCapture(event.pointerId);
                }
            };

            const onPointerMove = (event) => {
                if (!stateRef.dragging || stateRef.pointerId !== event.pointerId) return;
                viewport.scrollLeft = stateRef.startScrollLeft - (event.clientX - stateRef.startX);
                viewport.scrollTop = stateRef.startScrollTop - (event.clientY - stateRef.startY);
            };

            const endDrag = (event) => {
                if (event && stateRef.pointerId !== event.pointerId) return;
                stateRef.dragging = false;
                viewport.classList.remove('is-dragging');
                if (event && typeof viewport.releasePointerCapture === 'function') {
                    try {
                        viewport.releasePointerCapture(event.pointerId);
                    } catch (e) {
                        // 某些浏览器在 pointer capture 状态不一致时会抛错，这里静默忽略。
                    }
                }
                stateRef.pointerId = null;
            };

            toolbar.addEventListener('click', onToolbarClick);
            viewport.addEventListener('wheel', onWheel, { passive: false });
            viewport.addEventListener('pointerdown', onPointerDown);
            viewport.addEventListener('pointermove', onPointerMove);
            viewport.addEventListener('pointerup', endDrag);
            viewport.addEventListener('pointercancel', endDrag);

            cleanupFns.push(() => {
                toolbar.removeEventListener('click', onToolbarClick);
                viewport.removeEventListener('wheel', onWheel);
                viewport.removeEventListener('pointerdown', onPointerDown);
                viewport.removeEventListener('pointermove', onPointerMove);
                viewport.removeEventListener('pointerup', endDrag);
                viewport.removeEventListener('pointercancel', endDrag);
            });

            stateRef.scale = getFitScale();
            applyScale();
            requestAnimationFrame(() => {
                centerViewport();
            });
        };

        const renderAllMermaidBlocks = async () => {
            for (let index = 0; index < mermaidBlocks.length; index += 1) {
                if (disposed) return;
                const block = mermaidBlocks[index];
                const source = String(block.getAttribute('data-mermaid-source') || '').trim();
                if (!source) continue;

                const renderId = `proactive-mermaid-${currentDocumentPath || 'doc'}-${index}-${Date.now()}`
                    .replace(/[^a-zA-Z0-9_-]/g, '-');

                try {
                    block.classList.remove('is-error');
                    if (typeof mermaid.parse === 'function') {
                        await mermaid.parse(source, { suppressErrors: true });
                    }
                    const renderResult = await mermaid.render(renderId, source);
                    if (disposed) return;
                    block.innerHTML = renderResult?.svg || '';
                    attachMermaidViewportControls(block);
                } catch (error) {
                    if (disposed) return;
                    block.classList.add('is-error');
                    block.textContent = source;
                }
            }
        };

        renderAllMermaidBlocks();

        return () => {
            disposed = true;
            cleanupFns.forEach((cleanup) => {
                try {
                    cleanup();
                } catch (e) {
                    // 组件卸载阶段的清理错误不影响主流程。
                }
            });
        };
    }, [currentDocumentPath, renderedHtml, state.theme]);

    return (
        <Box className="notifications-view markdown-docs-view">
            <div className="card notifications-hero-card markdown-docs-hero-card">
                <Box className="tasks-header-row notifications-header-row">
                    <div className="notifications-header-main">
                        <Box className="notifications-title-row">
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexWrap: 'wrap' }}>
                                <Typography variant="h6" sx={{ fontWeight: 800 }}>
                                    {`文档浏览 (当前共 ${markdownFiles.length} 份文档)`}
                                </Typography>
                                <Chip
                                    label={currentDocumentPath ? `当前：${currentDocumentTitle}` : '请选择文档'}
                                    size="small"
                                    color="primary"
                                    variant="outlined"
                                />
                            </Box>
                        </Box>
                        <Typography variant="body2" className="tasks-header-subtitle">
                            在插件前端中直接阅读原生 Markdown 文件，例如 README、CHANGELOG 与 docs 目录文档。
                        </Typography>
                    </div>
                    <Box className="notifications-actions-row">
                        <Button
                            variant="outlined"
                            onClick={loadMarkdownFiles}
                            disabled={loadingList}
                            sx={{ borderRadius: 3 }}
                        >
                            刷新目录
                        </Button>
                        <Button
                            variant="contained"
                            onClick={handleRefreshCurrent}
                            disabled={loadingList || loadingDocument || !currentDocumentPath}
                            startIcon={<span>📘</span>}
                            sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}
                        >
                            刷新文档
                        </Button>
                    </Box>
                </Box>
            </div>

            <div className="markdown-docs-shell">
                <aside className="markdown-docs-aside-column">
                    <div className="markdown-docs-aside-sticky">
                        <div className="card markdown-docs-sidebar-card">
                            <div className="markdown-docs-sidebar-head">
                                <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                                    文档目录
                                </Typography>
                                <Typography variant="body2" color="text.secondary">
                                    仅展示插件目录内允许浏览的 Markdown 文件。
                                </Typography>
                            </div>

                            {markdownFiles.length === 0 ? (
                                <div className="tasks-empty-card markdown-docs-empty-side-card">
                                    <div className="tasks-empty-icon">📚</div>
                                    <Typography variant="body1" sx={{ fontWeight: 700, mb: 1 }}>
                                        {loadingList ? '正在加载文档列表…' : '暂无可浏览文档'}
                                    </Typography>
                                </div>
                            ) : (
                                <div className="markdown-docs-file-list">
                                    {markdownFiles.map((item) => {
                                        const isActive = item.path === currentDocumentPath;
                                        return (
                                            <button
                                                key={item.path}
                                                className={`markdown-docs-file-item ${isActive ? 'is-active' : ''}`}
                                                onClick={() => {
                                                    if (isActive) {
                                                        return;
                                                    }
                                                    loadMarkdownDocument(item.path);
                                                }}
                                            >
                                                <div className="markdown-docs-file-item-top">
                                                    <span className="markdown-docs-file-icon">📝</span>
                                                    <span className="markdown-docs-file-title">{item.title || item.filename || item.path}</span>
                                                </div>
                                                <div className="markdown-docs-file-path mono">{item.path}</div>
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    </div>
                </aside>

                <div className="markdown-docs-content-column">
                    <div className="card markdown-docs-content-card">
                        {!currentDocumentPath ? (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">📄</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    请选择左侧文档
                                </Typography>
                                <Typography variant="body1" color="text.secondary">
                                    选择后即可在当前管理端中直接阅读 Markdown 文档内容。
                                </Typography>
                            </div>
                        ) : loadingDocument && !markdownDocument ? (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">⏳</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    正在加载文档内容…
                                </Typography>
                            </div>
                        ) : markdownDocument ? (
                            <>
                                <div className="markdown-docs-article-head">
                                    <div>
                                        <Typography variant="h5" sx={{ fontWeight: 800, mb: 0.5 }}>
                                            {currentDocumentTitle}
                                        </Typography>
                                        <Typography variant="body2" className="task-card-session-sub mono">
                                            {currentDocumentPath}
                                        </Typography>
                                    </div>
                                </div>
                                <Box
                                    ref={articleRef}
                                    className="task-countdown-text notification-feed-content-text notification-md markdown-docs-article"
                                    // 这里直接挂载已经渲染好的 HTML，正文样式由 notification-md 体系统一承接。
                                    dangerouslySetInnerHTML={{ __html: renderedHtml }}
                                />
                            </>
                        ) : (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">⚠️</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    文档暂时不可用
                                </Typography>
                                <Typography variant="body1" color="text.secondary">
                                    当前文档未能成功加载，请稍后重试。
                                </Typography>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Box>
    );
}

window.MarkdownDocsView = MarkdownDocsView;
})();
