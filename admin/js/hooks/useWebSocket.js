/**
 * 文件职责：WebSocket Hook，负责全局单例连接管理、心跳与实时消息分发。
 */

// 管理端全局只维护一个 WebSocket 连接，避免多个组件重复建立相同连接。
let __globalWs = null;
// 所有订阅实时数据的回调统一挂到一个监听集合中，由单连接分发。
let __wsListeners = new Set();
// 连接状态订阅者，用于让上层感知实时通道是否可用。
let __wsStateListeners = new Set();
let __wsConnected = false;
// 重连定时器与退避参数：插件重载期间避免高频重连打满日志。
let __reconnectTimer = null;
let __heartbeatTimer = null;
const WS_RECONNECT_BASE_DELAY_MS = 1000;
const WS_RECONNECT_MAX_DELAY_MS = 10000;
// 后端会响应 ping 并回 pong，用短心跳及时发现半开连接。
const WS_HEARTBEAT_INTERVAL_MS = 25000;
// 连续多个心跳周期都收不到 pong，即可判定连接已半开（此时 send 不会抛错）。
const WS_PONG_TIMEOUT_MS = WS_HEARTBEAT_INTERVAL_MS * 2.5;
let __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
// 最近一次确认链路可用的时间戳（open 或收到 pong 时刷新）。
let __lastPongAt = 0;

function setConnected(value) {
    if (__wsConnected === value) return;
    __wsConnected = value;
    __wsStateListeners.forEach((listener) => {
        try {
            listener(value);
        } catch (e) {
            // 单个订阅者异常不影响其它组件。
        }
    });
}

function clearReconnectTimer() {
    if (!__reconnectTimer) return;
    clearTimeout(__reconnectTimer);
    __reconnectTimer = null;
}

function clearHeartbeatTimer() {
    if (!__heartbeatTimer) return;
    clearInterval(__heartbeatTimer);
    __heartbeatTimer = null;
}

function resetReconnectBackoff() {
    __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
    clearReconnectTimer();
}

function scheduleReconnect() {
    // 没有任何订阅者时不需要重连，避免后台空转。
    if (__wsListeners.size === 0) return;
    // 已有重连任务时直接复用，防止重复排队。
    if (__reconnectTimer) return;

    const delay = __nextReconnectDelayMs;
    __nextReconnectDelayMs = Math.min(__nextReconnectDelayMs * 2, WS_RECONNECT_MAX_DELAY_MS);

    __reconnectTimer = setTimeout(() => {
        __reconnectTimer = null;
        ensureGlobalWs();
    }, delay);
}

// 主动废弃当前连接：半开连接下 send() 既不抛错也收不到 pong，
// 只能自己关闭，让 onclose 完成状态更新与重连，否则连接状态会永久卡在“已连接”。
function recycleSocket(ws) {
    if (!ws || __globalWs !== ws) return;
    try {
        ws.close();
    } catch (e) {
        // 异常状态下 close 可能抛错且不会触发 onclose，这里手动回收避免状态卡死。
        if (__globalWs === ws) {
            __globalWs = null;
            clearHeartbeatTimer();
            __lastPongAt = 0;
            setConnected(false);
            scheduleReconnect();
        }
    }
}

function ensureGlobalWs() {
    // 若连接已存在且仍处于“连接中 / 已连接”状态，则直接复用。
    if (__globalWs && (__globalWs.readyState === WebSocket.OPEN || __globalWs.readyState === WebSocket.CONNECTING)) {
        return;
    }

    // 与 HTTP 保持相同的 token 来源，统一从 AuthUtil 中读取。
    const token = window.AuthUtil.getToken();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // 开启鉴权时通过 query 传 token；无鉴权或 no-auth 场景则不附加参数。
    const tokenQuery = token && token !== 'no-auth' ? `?token=${encodeURIComponent(token)}` : '';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws${tokenQuery}`);
    __globalWs = ws;

    ws.onopen = function () {
        if (__globalWs !== ws) return;
        setConnected(true);
        // 连接恢复后重置退避，后续若再次断开可从短延迟开始。
        resetReconnectBackoff();
        clearHeartbeatTimer();
        // 刚建立连接即可视为链路可用，后续靠 pong 持续刷新。
        __lastPongAt = Date.now();
        __heartbeatTimer = setInterval(() => {
            if (!__globalWs || __globalWs.readyState !== WebSocket.OPEN) return;
            // 后台标签页的定时器会被浏览器节流，此期间不做过期判定，避免误杀健康连接。
            if (document.visibilityState !== 'visible') {
                __lastPongAt = Date.now();
                return;
            }
            if (Date.now() - __lastPongAt > WS_PONG_TIMEOUT_MS) {
                recycleSocket(__globalWs);
                return;
            }
            try {
                __globalWs.send(JSON.stringify({ type: 'ping' }));
            } catch (e) {
                // 发送异常说明连接已不可用，主动断开走统一重连路径。
                recycleSocket(__globalWs);
            }
        }, WS_HEARTBEAT_INTERVAL_MS);
    };

    ws.onmessage = function (event) {
        try {
            const msg = JSON.parse(event.data);
            // pong 仅用于维持连接与刷新活性时间戳，不参与业务数据分发。
            if (msg.type === 'pong') {
                __lastPongAt = Date.now();
                return;
            }
            // 仅处理后端定义的两类数据推送消息，其余消息类型忽略。
            if (msg.type === 'full_update' || msg.type === 'update') {
                const data = msg.data || {};
                __wsListeners.forEach((listener) => {
                    try {
                        // 将后端 payload 原样广播给所有订阅者，由页面组件自行决定如何消费。
                        listener(data);
                    } catch (e) {
                        // 单个监听器报错不应影响其他订阅者继续接收消息。
                    }
                });
            }
        } catch (e) {
            // 非 JSON 消息或异常格式直接忽略，避免污染控制台与中断连接。
        }
    };

    ws.onerror = function () {
        // 出错后主动触发 close 流程，统一走 onclose 的回收与重连路径。
        try {
            ws.close();
        } catch (e) {
            // 某些浏览器状态下 close 可能抛错，这里吞掉即可。
        }
    };

    ws.onclose = function () {
        // 仅处理当前活动连接的关闭事件，避免旧连接回调干扰新的连接状态。
        if (__globalWs !== ws) {
            return;
        }
        __globalWs = null;
        __lastPongAt = 0;
        clearHeartbeatTimer();
        setConnected(false);
        // 连接关闭后自动尝试重连，覆盖插件重载等短暂不可用场景。
        scheduleReconnect();
    };
}

function useWebSocket(onData) {
    const [connected, setConnectedState] = React.useState(__wsConnected);

    React.useEffect(() => {
        if (typeof onData === 'function') {
            __wsListeners.add(onData);
        }
        __wsStateListeners.add(setConnectedState);

        // 只要有任意一个订阅者存在，就确保全局连接已建立。
        ensureGlobalWs();

        return () => {
            if (typeof onData === 'function') {
                __wsListeners.delete(onData);
            }
            __wsStateListeners.delete(setConnectedState);
            // 最后一个订阅者离开时主动关闭连接，减少空闲资源占用。
            if (__wsListeners.size === 0) {
                clearReconnectTimer();
                clearHeartbeatTimer();
                __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
                __lastPongAt = 0;
                if (__globalWs) {
                    try {
                        __globalWs.close();
                    } catch (e) {
                        // 某些浏览器状态下 close 可能抛错，这里吞掉即可。
                    }
                }
                __globalWs = null;
                setConnected(false);
            }
        };
    }, [onData]);

    return connected;
}

// 暴露为全局 Hook，供入口应用与各页面脚本直接调用。
window.useWebSocket = useWebSocket;
