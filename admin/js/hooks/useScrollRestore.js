/**
 * 文件职责：滚动位置恢复 Hook，统一主内容区与配置表单的恢复/记录逻辑。
 */

(function () {
    function useScrollRestore(containerRef, storageKey, options) {
        const enabled = !options || options.enabled !== false;
        const restoringRef = React.useRef(false);

        // 恢复阶段临时关闭平滑滚动，避免 scrollTop 赋值被动画接管。
        React.useLayoutEffect(() => {
            const el = containerRef.current;
            if (!el || !enabled) return undefined;

            const savedPos = window.ScrollDomain.readScrollTop(storageKey);
            if (savedPos <= 0) {
                restoringRef.current = false;
                el.scrollTop = 0;
                return undefined;
            }

            restoringRef.current = true;
            const previousBehavior = el.style.scrollBehavior;
            el.style.scrollBehavior = 'auto';

            const applyRestore = () => {
                const maxScrollTop = Math.max(el.scrollHeight - el.clientHeight, 0);
                el.scrollTop = Math.min(savedPos, maxScrollTop);
            };

            applyRestore();
            // 内容异步撑开时再补一次，但不再依赖全局平滑滚动。
            const raf = requestAnimationFrame(() => {
                if (restoringRef.current) applyRestore();
            });

            const timer = window.setTimeout(() => {
                restoringRef.current = false;
                el.style.scrollBehavior = previousBehavior;
            }, 320);

            return () => {
                cancelAnimationFrame(raf);
                window.clearTimeout(timer);
                el.style.scrollBehavior = previousBehavior;
            };
        }, [containerRef, storageKey, enabled]);

        // 记录滚动位置；用户主动交互时立即终止恢复锁。
        React.useEffect(() => {
            const el = containerRef.current;
            if (!el || !enabled) return undefined;

            let timeout = 0;
            const handleScroll = () => {
                window.clearTimeout(timeout);
                timeout = window.setTimeout(() => {
                    window.ScrollDomain.writeScrollTop(storageKey, el.scrollTop);
                }, 120);
            };
            const stopRestoring = () => {
                restoringRef.current = false;
            };

            el.addEventListener('scroll', handleScroll, { passive: true });
            el.addEventListener('wheel', stopRestoring, { passive: true });
            el.addEventListener('touchstart', stopRestoring, { passive: true });
            el.addEventListener('mousedown', stopRestoring);

            return () => {
                el.removeEventListener('scroll', handleScroll);
                el.removeEventListener('wheel', stopRestoring);
                el.removeEventListener('touchstart', stopRestoring);
                el.removeEventListener('mousedown', stopRestoring);
                window.clearTimeout(timeout);
            };
        }, [containerRef, storageKey, enabled]);
    }

    window.useScrollRestore = useScrollRestore;
})();
