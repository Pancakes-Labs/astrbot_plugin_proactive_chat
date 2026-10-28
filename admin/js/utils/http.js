/**
 * 文件职责：HTTP 工具模块，负责统一请求封装、鉴权头注入与错误处理。
 */

(function () {
    function buildHeaders(extra) {
        // 所有请求默认发送 JSON；如调用方有额外头信息，再在此基础上合并。
        return window.AuthUtil.withAuthHeaders(
            Object.assign({ 'Content-Type': 'application/json' }, extra || {})
        );
    }

    async function request(url, options) {
        // 复制 options，避免上层传入对象在内部被意外修改。
        const opts = Object.assign({}, options || {});
        // 在统一入口补齐认证头与默认内容类型，减少各业务文件重复代码。
        opts.headers = buildHeaders(opts.headers || {});

        const response = await fetch(url, opts);
        let payload = null;
        try {
            // 后端大多数接口都返回 JSON；若解析失败则容忍并回退为 null。
            payload = await response.json();
        } catch (e) {
            payload = null;
        }

        if (!response.ok) {
            // 优先透传后端明确返回的 error 字段，提升前端报错可读性。
            const message = payload && payload.error ? payload.error : '请求失败';
            const error = new Error(message);
            // 附带状态码，便于调用方区分 401 等需要特殊处理的场景。
            error.status = response.status;
            error.payload = payload;
            throw error;
        }

        return payload;
    }

    window.HttpUtil = {
        get: function (url) {
            return request(url, { method: 'GET' });
        },
        post: function (url, body) {
            // POST 请求统一将 body 序列化为 JSON；空 body 则发送空对象保持接口风格一致。
            return request(url, {
                method: 'POST',
                body: JSON.stringify(body || {}),
            });
        },
        del: function (url) {
            return request(url, { method: 'DELETE' });
        }
    };
})();

