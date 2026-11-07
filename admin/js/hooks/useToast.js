/**
 * 文件职责：全局 Toast 通知容器，负责浮层的入队、去重合并、自动消失与离场动画。
 *
 * 以事件方式对外暴露（window.Toast.*），避免各页面再各自维护提示状态，
 * 也规避了在非组件环境中无法拿到 setState 的问题。
 *
 * 能力概览：
 * - notify / success / error / warning / info / loading
 * - key 去重：相同 key 的提示会原地替换内容并重置倒计时，不堆叠
 * - promise：一次调用覆盖 loading -> 成功 / 失败三态
 * - 悬停或聚焦时暂停倒计时，移开后按剩余时间继续
 * - 底部进度条直观展示剩余停留时间
 */

(function () {
    const { useState, useEffect, useCallback, useRef } = React;

    // 不同语义的停留时长：错误与警告需要更长的阅读时间；loading 常驻直到被替换。
    const DURATION_MAP = {
        success: 2600,
        info: 3200,
        warning: 4200,
        error: 5200,
        loading: 0,
    };

    // 离场过渡时长，需与样式表中的 .app-toast 过渡时长保持一致。
    const EXIT_DURATION = 240;
    // 同屏上限，超出后丢弃最旧的一条，避免高频操作把界面糊满。
    const MAX_TOASTS = 5;

    let nextId = 1;
    // 模块级订阅者集合，Toast 容器挂载后接管事件派发。
    const listeners = new Set();
    // 容器尚未挂载时先缓存，挂载后统一补发，避免首屏提示丢失。
    const pendingQueue = [];
    // key -> id 与 id -> toast，用于同 key 原地替换与状态清理。
    const keyIndex = new Map();
    const knownToasts = new Map();
    // 最近一次播报的文本与时间，供上层判断“这条错误是不是已经有 toast 了”，避免重复提示。
    let lastNotified = { text: '', at: 0 };

    function emit(event) {
        if (listeners.size === 0) {
            if (event.type === 'add') pendingQueue.push(event.toast);
            return;
        }
        listeners.forEach((listener) => {
            try {
                listener(event);
            } catch (e) {
                console.error('Toast 派发失败', e);
            }
        });
    }

    // 从内部索引中彻底移除一条提示，避免 key 长期占用导致后续同 key 提示无法新增。
    function forget(id) {
        const toast = knownToasts.get(id);
        if (!toast) return;
        knownToasts.delete(id);
        if (toast.key && keyIndex.get(toast.key) === id) {
            keyIndex.delete(toast.key);
        }
    }

    function resolveStatus(value) {
        return DURATION_MAP[value] !== undefined ? value : 'info';
    }

    function notify(message, options) {
        const text = String(message === undefined || message === null ? '' : message).trim();
        if (!text) return null;

        const opts = options || {};
        const status = resolveStatus(opts.status);
        const key = opts.key ? String(opts.key) : '';

        // 同 key 复用既有 id，让容器把新内容替换到原位置，避免闪断。
        let id;
        if (key && keyIndex.has(key)) {
            id = keyIndex.get(key);
        } else {
            id = nextId++;
            if (key) keyIndex.set(key, id);
        }

        let duration;
        if (typeof opts.duration === 'number' && opts.duration >= 0) {
            duration = opts.duration;
        } else {
            duration = DURATION_MAP[status];
        }

        const toast = {
            id: id,
            key: key,
            text: text,
            status: status,
            title: opts.title ? String(opts.title) : '',
            description: opts.description ? String(opts.description) : '',
            action: opts.action || null,
            duration: duration,
            dismissible: opts.dismissible !== false,
        };

        knownToasts.set(id, toast);
        lastNotified = { text: text, at: Date.now() };
        emit({ type: 'add', toast: toast });
        return id;
    }

    // 判断某条文案是否刚刚已经播报过，用于全局错误兜底去重。
    function seenRecently(message, withinMs) {
        const text = String(message === undefined || message === null ? '' : message).trim();
        if (!text || !lastNotified.text) return false;
        const windowMs = Number(withinMs) > 0 ? Number(withinMs) : 1500;
        return lastNotified.text === text && Date.now() - lastNotified.at <= windowMs;
    }

    function dismiss(target) {
        if (target === undefined || target === null) return;
        if (typeof target === 'number') {
            forget(target);
            emit({ type: 'remove', id: target });
            return;
        }
        const key = String(target);
        if (!keyIndex.has(key)) return;
        const id = keyIndex.get(key);
        forget(id);
        emit({ type: 'remove', id: id });
    }

    function dismissAll() {
        knownToasts.clear();
        keyIndex.clear();
        emit({ type: 'clear' });
    }

    /**
     * 把一个 Promise 的三种状态映射到同一条提示上。
     * messages 支持字符串或 (result|error) => string；传 false 表示该阶段不提示。
     */
    function promise(promiseOrFactory, messages) {
        const msg = messages || {};
        const key = msg.key ? String(msg.key) : `toast-promise-${nextId}`;
        // loading 传 false 表示不展示进行中提示，只关心最终结果。
        const loadingText = msg.loading === false
            ? ''
            : (typeof msg.loading === 'string' ? msg.loading : '处理中，请稍候…');

        const loadingId = loadingText
            ? notify(loadingText, { status: 'loading', title: msg.title, key: key })
            : null;

        let pending;
        try {
            pending = typeof promiseOrFactory === 'function' ? promiseOrFactory() : promiseOrFactory;
        } catch (e) {
            pending = Promise.reject(e);
        }

        return Promise.resolve(pending).then(
            (result) => {
                const text = typeof msg.success === 'function'
                    ? msg.success(result)
                    : (msg.success === undefined ? '操作成功' : msg.success);
                if (text === false || !String(text || '').trim()) {
                    dismiss(loadingId);
                } else {
                    notify(text, { status: 'success', title: msg.title, key: key });
                }
                return result;
            },
            (error) => {
                const text = typeof msg.error === 'function'
                    ? msg.error(error)
                    : (msg.error || (error && error.message) || '操作失败，请稍后重试');
                if (text === false || !String(text || '').trim()) {
                    dismiss(loadingId);
                } else {
                    notify(text, { status: 'error', title: msg.title || '操作失败', key: key });
                }
                throw error;
            }
        );
    }

    const ToastApi = Object.freeze({
        notify: notify,
        success: (message, options) => notify(message, Object.assign({}, options, { status: 'success' })),
        error: (message, options) => notify(message, Object.assign({}, options, { status: 'error' })),
        warning: (message, options) => notify(message, Object.assign({}, options, { status: 'warning' })),
        info: (message, options) => notify(message, Object.assign({}, options, { status: 'info' })),
        loading: (message, options) => notify(message, Object.assign({}, options, { status: 'loading' })),
        promise: promise,
        dismiss: dismiss,
        dismissAll: dismissAll,
        seenRecently: seenRecently,
    });

    // 图标统一使用内联 SVG，避免 emoji 在不同系统下字形与基线不一致。
    const ICON_PATHS = {
        success: (
            <React.Fragment>
                <circle cx="12" cy="12" r="9" />
                <path d="M8.4 12.3l2.4 2.4 4.8-4.9" />
            </React.Fragment>
        ),
        error: (
            <React.Fragment>
                <circle cx="12" cy="12" r="9" />
                <path d="M12 7.6v5.2" />
                <path d="M12 16.2h.01" />
            </React.Fragment>
        ),
        warning: (
            <React.Fragment>
                <path d="M12 3.9l8.1 14.1a1.2 1.2 0 0 1-1 1.8H4.9a1.2 1.2 0 0 1-1-1.8z" />
                <path d="M12 9.2v4.2" />
                <path d="M12 16.4h.01" />
            </React.Fragment>
        ),
        info: (
            <React.Fragment>
                <circle cx="12" cy="12" r="9" />
                <path d="M12 11.2v5" />
                <path d="M12 7.9h.01" />
            </React.Fragment>
        ),
        loading: (
            <React.Fragment>
                <circle cx="12" cy="12" r="9" opacity="0.22" />
                <path d="M21 12a9 9 0 0 0-9-9" />
            </React.Fragment>
        ),
    };

    function ToastIcon({ status }) {
        return (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
                {ICON_PATHS[status] || ICON_PATHS.info}
            </svg>
        );
    }

    function ToastItem({ toast, onClose }) {
        const [leaving, setLeaving] = useState(false);
        const [paused, setPaused] = useState(false);
        // 剩余可停留时间：暂停时只扣减已消耗部分，恢复后从该值继续。
        const remainingRef = useRef(toast.duration);
        const startedAtRef = useRef(0);

        // 内容被同 key 替换（revision 变化）时重置倒计时并撤掉离场态。
        useEffect(() => {
            remainingRef.current = toast.duration;
            startedAtRef.current = 0;
            setLeaving(false);
        }, [toast.id, toast.revision, toast.duration]);

        // 常驻提示（duration 为 0）不启动定时器，仅等待外部替换或手动关闭。
        useEffect(() => {
            if (!toast.duration || toast.duration <= 0) return undefined;
            if (paused || leaving) return undefined;

            startedAtRef.current = Date.now();
            const timer = window.setTimeout(() => setLeaving(true), Math.max(0, remainingRef.current));

            return () => {
                window.clearTimeout(timer);
                if (startedAtRef.current) {
                    const elapsed = Date.now() - startedAtRef.current;
                    remainingRef.current = Math.max(0, remainingRef.current - elapsed);
                    startedAtRef.current = 0;
                }
            };
        }, [paused, leaving, toast.duration, toast.revision, toast.id]);

        // 离场动画播完再从列表移除，避免元素瞬间消失造成跳变。
        useEffect(() => {
            if (!leaving) return undefined;
            const timer = window.setTimeout(() => onClose(toast.id), EXIT_DURATION);
            return () => window.clearTimeout(timer);
        }, [leaving, onClose, toast.id]);

        const requestClose = useCallback(() => {
            setLeaving(true);
        }, []);

        // 悬停或键盘聚焦时暂停倒计时，方便阅读长文案。
        const handlePause = useCallback(() => setPaused(true), []);
        const handleResume = useCallback(() => setPaused(false), []);

        const showProgress = toast.duration > 0;

        return (
            <div
                className={`app-toast is-${toast.status}${leaving ? ' is-leaving' : ''}${paused ? ' is-paused' : ''}`}
                role={toast.status === 'error' || toast.status === 'warning' ? 'alert' : 'status'}
                aria-live={toast.status === 'error' || toast.status === 'warning' ? 'assertive' : 'polite'}
                onMouseEnter={handlePause}
                onMouseLeave={handleResume}
                onFocus={handlePause}
                onBlur={handleResume}
            >
                <span className="app-toast-icon">
                    <ToastIcon status={toast.status} />
                </span>

                <div className="app-toast-body">
                    {toast.title ? <div className="app-toast-title">{toast.title}</div> : null}
                    <div className="app-toast-text">{toast.text}</div>
                    {toast.description ? (
                        <div className="app-toast-desc">{toast.description}</div>
                    ) : null}
                    {toast.action && toast.action.label ? (
                        <button
                            type="button"
                            className="app-toast-action"
                            onClick={() => {
                                try {
                                    if (typeof toast.action.onClick === 'function') toast.action.onClick();
                                } finally {
                                    requestClose();
                                }
                            }}
                        >
                            {toast.action.label}
                        </button>
                    ) : null}
                </div>

                {toast.dismissible ? (
                    <button
                        type="button"
                        className="app-toast-close"
                        aria-label="关闭提示"
                        onClick={requestClose}
                    >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                            strokeLinecap="round" aria-hidden="true" focusable="false">
                            <path d="M6 6l12 12M18 6L6 18" />
                        </svg>
                    </button>
                ) : null}

                {showProgress ? (
                    <span
                        className="app-toast-progress"
                        // key 绑定 revision，让替换后的提示进度条从头开始。
                        key={`progress-${toast.revision}`}
                        style={{
                            '--toast-progress-duration': `${toast.duration}ms`,
                            animationPlayState: paused ? 'paused' : 'running',
                        }}
                    />
                ) : null}
            </div>
        );
    }

    function ToastHost() {
        const [toasts, setToasts] = useState([]);

        const dismissOne = useCallback((id) => {
            forget(id);
            setToasts((current) => current.filter((item) => item.id !== id));
        }, []);

        useEffect(() => {
            const listener = (event) => {
                if (event.type === 'add') {
                    const incoming = event.toast;
                    setToasts((current) => {
                        // 同 id（同 key）时原地更新，revision 递增以重置倒计时与进度条。
                        const index = current.findIndex((item) => item.id === incoming.id);
                        if (index >= 0) {
                            const next = current.slice();
                            next[index] = Object.assign({}, incoming, {
                                revision: (current[index].revision || 0) + 1,
                            });
                            return next;
                        }
                        const next = current.concat(Object.assign({}, incoming, { revision: 0 }));
                        if (next.length <= MAX_TOASTS) return next;
                        // 超出上限时丢弃最旧的一条，同步清理索引防止内存滞留。
                        const dropped = next.slice(0, next.length - MAX_TOASTS);
                        dropped.forEach((item) => forget(item.id));
                        return next.slice(next.length - MAX_TOASTS);
                    });
                    return;
                }
                if (event.type === 'remove') {
                    setToasts((current) => current.filter((item) => item.id !== event.id));
                    return;
                }
                if (event.type === 'clear') {
                    setToasts([]);
                }
            };

            listeners.add(listener);
            // 容器晚于提示挂载时，把此前累积的提示按序补发。
            if (pendingQueue.length > 0) {
                const queued = pendingQueue.splice(0, pendingQueue.length);
                queued.forEach((toast) => listener({ type: 'add', toast: toast }));
            }
            return () => listeners.delete(listener);
        }, []);

        if (toasts.length === 0) return null;

        return (
            <div className="app-toast-stack" role="region" aria-label="通知提示">
                {toasts.map((toast) => (
                    <ToastItem key={toast.id} toast={toast} onClose={dismissOne} />
                ))}
            </div>
        );
    }

    window.Toast = ToastApi;
    window.ToastHost = ToastHost;
})();
