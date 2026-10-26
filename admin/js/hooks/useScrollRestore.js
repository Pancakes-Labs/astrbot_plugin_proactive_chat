/**
 * 文件职责：滚动位置恢复 Hook，统一主内容区与配置表单的恢复/记录逻辑。
 */

(function () {
    // 解析真正承载滚动的元素：移动端断点会把元素的 overflow 改成 visible，
    // 此时滚动发生在文档上，继续读写该元素既收不到 scroll 事件也改不动位置。
    function resolveScrollTarget(containerRef) {
        const el = containerRef.current;
        if (!el) return null;

        let overflowY = 'visible';
        try {
            overflowY = window.getComputedStyle(el).overflowY || 'visible';
        } catch (e) {
            overflowY = 'visible';
        }

        if (overflowY === 'visible' || overflowY === 'clip') {
            return {
                element: document.scrollingElement || document.documentElement,
                isWindow: true,
            };
        }

        return { element: el, isWindow: false };
    }

    function maxScrollTopOf(element, isWindow) {
        const viewportHeight = isWindow ? window.innerHeight : element.clientHeight;
        return Math.max(element.scrollHeight - viewportHeight, 0);
    }

    // 视口跨过断点后滚动容器可能从元素变成文档（或反之），
    // 监听器与恢复逻辑都必须跟着换绑，否则后续滚动不会被记录。
    function useScrollTarget(containerRef) {
        const [target, setTarget] = React.useState(null);

        // 用 layout 阶段解析首次目标：若推迟到绘制之后，恢复会晚一帧才生效，
        // 用户会先看到顶部再跳到记忆位置。
        React.useLayoutEffect(() => {
            let disposed = false;
            let frame = 0;

            const sync = () => {
                frame = 0;
                if (disposed) return;
                const next = resolveScrollTarget(containerRef);
                setTarget((prev) => {
                    const prevElement = prev && prev.element;
                    const nextElement = next && next.element;
                    const prevIsWindow = prev ? prev.isWindow : null;
                    const nextIsWindow = next ? next.isWindow : null;
                    // 滚动目标未变化时保持原引用，避免无谓的重新绑定。
                    if (prevElement === nextElement && prevIsWindow === nextIsWindow) {
                        return prev;
                    }
                    return next;
                });
            };

            const scheduleSync = () => {
                if (frame) return;
                frame = requestAnimationFrame(sync);
            };

            sync();
            // resize 覆盖断点切换与移动端地址栏收放导致的布局变化。
            window.addEventListener('resize', scheduleSync);
            window.addEventListener('orientationchange', scheduleSync);

            return () => {
                disposed = true;
                if (frame) cancelAnimationFrame(frame);
                window.removeEventListener('resize', scheduleSync);
                window.removeEventListener('orientationchange', scheduleSync);
            };
        }, [containerRef]);

        return target;
    }

    function useScrollRestore(containerRef, storageKey, options) {
        const enabled = !options || options.enabled !== false;
        const restoringRef = React.useRef(false);
        const target = useScrollTarget(containerRef);

        // 恢复阶段临时关闭平滑滚动，避免 scrollTop 赋值被动画接管。
        React.useLayoutEffect(() => {
            if (!target || !enabled) return undefined;
            const element = target.element;

            const savedPos = window.ScrollDomain.readScrollTop(storageKey);
            if (savedPos <= 0) {
                restoringRef.current = false;
                element.scrollTop = 0;
                return undefined;
            }

            restoringRef.current = true;
            const previousBehavior = element.style.scrollBehavior;
            element.style.scrollBehavior = 'auto';

            const applyRestore = () => {
                element.scrollTop = Math.min(
                    savedPos,
                    maxScrollTopOf(element, target.isWindow)
                );
            };

            applyRestore();
            // 内容异步撑开时再补一次，但不再依赖全局平滑滚动。
            const raf = requestAnimationFrame(() => {
                if (restoringRef.current) applyRestore();
            });

            const timer = window.setTimeout(() => {
                restoringRef.current = false;
                element.style.scrollBehavior = previousBehavior;
            }, 320);

            return () => {
                cancelAnimationFrame(raf);
                window.clearTimeout(timer);
                element.style.scrollBehavior = previousBehavior;
            };
        }, [target, storageKey, enabled]);

        // 记录滚动位置；用户主动交互时立即终止恢复锁。
        React.useEffect(() => {
            if (!target || !enabled) return undefined;
            const element = target.element;
            // 文档滚动时事件落在 window 上，不会冒泡到任何具体元素。
            const host = target.isWindow ? window : element;

            let timeout = 0;
            const flushScrollTop = () => {
                window.ScrollDomain.writeScrollTop(storageKey, element.scrollTop);
            };
            const handleScroll = () => {
                window.clearTimeout(timeout);
                timeout = window.setTimeout(flushScrollTop, 120);
            };
            const stopRestoring = () => {
                restoringRef.current = false;
            };

            host.addEventListener('scroll', handleScroll, { passive: true });
            host.addEventListener('wheel', stopRestoring, { passive: true });
            host.addEventListener('touchstart', stopRestoring, { passive: true });
            host.addEventListener('mousedown', stopRestoring);

            return () => {
                host.removeEventListener('scroll', handleScroll);
                host.removeEventListener('wheel', stopRestoring);
                host.removeEventListener('touchstart', stopRestoring);
                host.removeEventListener('mousedown', stopRestoring);
                // 待写入的位置不能随计时器一起丢弃：用户可能在 120ms 内切换视图，
                // 那样这次滚动就永远不会被记录，下次进入会回退到更早的位置。
                if (timeout) {
                    window.clearTimeout(timeout);
                    timeout = 0;
                    flushScrollTop();
                }
            };
        }, [target, storageKey, enabled]);
    }

    window.useScrollRestore = useScrollRestore;
})();
