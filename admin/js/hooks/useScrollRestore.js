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
    // 监听器与恢复逻辑都必须跟着换绑，且 enabled 必须参与依赖。
    function useScrollTarget(containerRef, enabled, allowWindow) {
        const [target, setTarget] = React.useState(null);

        // 用 layout 阶段解析首次目标：若推迟到绘制之后，恢复会晚一帧才生效，
        // 用户会先看到顶部再跳到记忆位置。
        React.useLayoutEffect(() => {
            if (!enabled) {
                setTarget(null);
                return undefined;
            }

            let disposed = false;
            let frame = 0;

            const sync = () => {
                frame = 0;
                if (disposed) return;
                let next = resolveScrollTarget(containerRef);
                // allowWindow=false 的容器（如文档目录列表）在移动端会退化成
                // 跟随文档滚动，而文档滚动已由 App 层统一恢复；
                // 两边写同一个滚动源只会互相覆盖，这里直接放弃接管。
                if (next && next.isWindow && !allowWindow) {
                    next = null;
                }
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
        }, [containerRef, enabled, allowWindow]);

        return target;
    }

    function useScrollRestore(containerRef, storageKey, options) {
        const enabled = !options || options.enabled !== false;
        // 默认允许接管文档滚动；内部滚动区（如文档目录）应传 false。
        const allowWindow = !options || options.allowWindow !== false;
        const restoringRef = React.useRef(false);
        // 最新滚动位置的镜像：layout cleanup 阶段读到的 scrollTop 可能已被
        // 新视图的 DOM 变更钳制，只有事件里记录下来的值才是可靠的。
        const latestScrollTopRef = React.useRef(0);
        // 每次 storageKey 变化（切换视图）都必须重新走一遍恢复流程。
        // 主内容区在视图切换时复用同一个 DOM 节点，target 引用不会变，
        // 若只依赖 target，恢复 effect 不会重跑，表现就是“位置永远是初始的”。
        const restoreToken = String(storageKey || '');
        const target = useScrollTarget(containerRef, enabled, allowWindow);

        // 恢复阶段临时关闭平滑滚动，避免 scrollTop 赋值被动画接管。
        React.useLayoutEffect(() => {
            if (!target || !enabled) return undefined;
            const element = target.element;

            const savedPos = window.ScrollDomain.readScrollTop(restoreToken);
            // 以记忆值为初始基准，避免清理时把上一个 key 的残留值写进本 key。
            latestScrollTopRef.current = savedPos;
            if (savedPos <= 0) {
                restoringRef.current = false;
                element.scrollTop = 0;
                return undefined;
            }

            restoringRef.current = true;
            const previousBehavior = element.style.scrollBehavior;
            element.style.scrollBehavior = 'auto';

            // 尝试把容器滚到记忆位置；返回 true 表示已经到位。
            // 内容高度不足时浏览器会把 scrollTop 钳制住，此时需要稍后重试。
            const applyRestore = () => {
                const maxTop = maxScrollTopOf(element, target.isWindow);
                element.scrollTop = Math.min(savedPos, maxTop);
                latestScrollTopRef.current = element.scrollTop;
                return maxTop >= savedPos;
            };

            // 数据异步加载：刚挂载时内容还没撑开，这里在短窗口内逐帧重试，
            // 等内容高度足够后再落位，避免位置被钳制后无法回到原处。
            const deadline = Date.now() + 900;
            let frame = 0;
            const tick = () => {
                frame = 0;
                if (!restoringRef.current) return;
                const reached = applyRestore();
                if (reached || Date.now() > deadline) {
                    restoringRef.current = false;
                    element.style.scrollBehavior = previousBehavior;
                    return;
                }
                frame = requestAnimationFrame(tick);
            };
            tick();

            return () => {
                if (frame) cancelAnimationFrame(frame);
                element.style.scrollBehavior = previousBehavior;
                // 不要在这里读 element.scrollTop：React 18 的 layout cleanup 在
                // 子树 DOM 变更之后才执行，此时容器可能已被新视图替换或变矮，
                // 读出来的值会被浏览器钳制，覆盖掉原本正确的位置。
                const latestTop = latestScrollTopRef.current;
                if (restoringRef.current && latestTop < savedPos) {
                    // 恢复没跑完：记忆值比当前值更可信，保留原值。
                    window.ScrollDomain.writeScrollTop(restoreToken, savedPos);
                } else {
                    window.ScrollDomain.writeScrollTop(restoreToken, latestTop);
                }
                restoringRef.current = false;
            };
            // 显式依赖 restoreToken：视图切换时即使 DOM 节点复用也要重新恢复。
        }, [target, restoreToken, enabled]);

        // 记录滚动位置；用户主动交互时立即终止恢复锁。
        React.useEffect(() => {
            if (!target || !enabled) return undefined;
            const element = target.element;
            // 文档滚动时事件落在 window 上，不会冒泡到任何具体元素。
            const host = target.isWindow ? window : element;

            let timeout = 0;
            const flushScrollTop = () => {
                window.ScrollDomain.writeScrollTop(restoreToken, element.scrollTop);
            };
            const handleScroll = () => {
                // 同步记录到 ref：cleanup 只信这个值，它对应钳制前的真实滚动位置。
                latestScrollTopRef.current = element.scrollTop;
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
                // 待写入的位置不会丢失：layout cleanup 已在 DOM 更新前落盘，
                // 这里只需丢弃待触发的计时器，避免卸载后回写。
                if (timeout) {
                    window.clearTimeout(timeout);
                    timeout = 0;
                }
            };
        }, [target, restoreToken, enabled]);
    }

    window.useScrollRestore = useScrollRestore;
})();
