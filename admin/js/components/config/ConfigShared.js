/**
 * 文件职责：配置渲染公共常量与纯函数，供字段组件、加载 Hook 与主组件共用。
 */

(function () {
    // 递归扫描 Schema 中所有 object 节点，生成“全部展开 / 收起”需要的路径列表。
    function getAllExpandablePaths(schema, prefix) {
        const base = prefix || '';
        let paths = [];
        Object.entries(schema || {}).forEach(([key, value]) => {
            const currentPath = base ? `${base}.${key}` : key;
            if (value && value.type === 'object' && value.items) {
                paths.push(currentPath);
                paths = paths.concat(getAllExpandablePaths(value.items, currentPath));
            }
        });
        return paths;
    }

    // 只为“配置分组”定义 emoji：字段级图标会带来视觉噪音，配置项靠文字描述即可区分。
    const CONFIG_ICONS = {
        admin_users: '🔐',
        session_defaults: '🛠️',
        prompt_settings: '🧠',
        model_settings: '🔌',
        context_settings: '🧩',
        schedule_settings: '🕒',
        startup_check_settings: '🤖',
        group_silence_settings: '🕊️',
        tts_settings: '🔊',
        segmented_reply_settings: '🔪',
        web_admin: '🌐',
        notification_settings: '🔔',
        telemetry_config: '📡',
    };

    // 某些 Schema 描述文本自身已带 emoji；这里剥离前缀，避免界面上图标重复出现。
    const LEADING_EMOJI_REGEX = /^\s*(?:\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)\s*/u;

    function stripLeadingEmoji(text) {
        if (typeof text !== 'string') return text;
        return text.replace(LEADING_EMOJI_REGEX, '').trimStart();
    }

    // 会话差异配置页只暴露可编辑字段子集，并跳过仅对服务端有意义的分组。
    function getSessionSchemaEntries(schema, sessionType) {
        const rootKey = 'session_defaults';
        const rootItems = (schema && schema[rootKey] && schema[rootKey].items) || {};
        const orderedKeys = [
            'persona_id',
            'model_settings',
            'prompt_settings',
            'startup_check_settings',
            'group_silence_settings',
            'context_settings',
            'schedule_settings',
            'tts_settings',
            'segmented_reply_settings',
        ];
        const hiddenKeys = new Set(['enabled', 'session_list']);

        const entries = [];
        const sessionNameSchema = rootItems.session_name || {
            type: 'string',
            default: '',
            description: '会话备注名',
            hint: '用于日志和管理端展示。为空时将回退显示 UMO。',
        };
        entries.push(['session_name', sessionNameSchema]);

        orderedKeys
            .filter((key) => {
                if (hiddenKeys.has(key) || !rootItems[key]) return false;
                // 群聊沉默配置只对群聊有意义，私聊覆写页不应出现。
                if (key === 'group_silence_settings' && sessionType !== 'group') {
                    return false;
                }
                return true;
            })
            .forEach((key) => {
                entries.push([key, rootItems[key]]);
            });

        return entries;
    }

    // 全局配置保存时提交的键白名单必须与后端保持一致。
    const GLOBAL_CONFIG_KEYS = Object.freeze([
        'admin_users',
        'session_defaults',
        'web_admin',
        'notification_settings',
        'telemetry_config',
    ]);

    // 迁移期保留的隐藏字段：不在配置页展示，也不参与保存。
    const HIDDEN_ROOT_KEYS = Object.freeze([
        'legacy_type_fallback',
        'friend_settings',
        'group_settings',
    ]);

    // 可用占位符清单：与后端 prompt_placeholders.PLACEHOLDER_SPECS 保持一致，
    // 用于在模板编辑处提示用户，并检测拼写错误。
    const PLACEHOLDER_LABELS = Object.freeze({
        '{{current_time}}': '当前时间',
        '{{unanswered_count}}': '未回复次数',
        '{{conversation_type}}': '会话类型（私聊/群聊）',
        '{{platform_history_lines}}': '平台聊天流水正文',
        '{{platform_history_last_time}}': '流水最后一条时间',
    });

    // 模板不再区分类型：每条模板就是一句主动消息正文。

    // 模板适用范围（scope）可选值。
    const TEMPLATE_SCOPES = Object.freeze([
        { value: '', label: '通用' },
        { value: 'friend', label: '仅私聊' },
        { value: 'group', label: '仅群聊' },
    ]);

    // 提取文本中所有未登记的占位符，供编辑框下方提示拼写错误。
    // 触发模型开始输出的最小正文要求：留空会让该模板在选取阶段被跳过。
    const TEMPLATE_EMPTY_HINT = '正文为空时该模板不会被使用。';

    function findUnknownPlaceholders(text) {
        if (typeof text !== 'string' || !text) return [];
        const matches = text.match(/\{\{[^{}]*\}\}/g) || [];
        const unknown = matches.filter((item) => !(item in PLACEHOLDER_LABELS));
        return Array.from(new Set(unknown));
    }

    window.ConfigShared = Object.freeze({
        CONFIG_ICONS: CONFIG_ICONS,
        GLOBAL_CONFIG_KEYS: GLOBAL_CONFIG_KEYS,
        HIDDEN_ROOT_KEYS: HIDDEN_ROOT_KEYS,
        PLACEHOLDER_LABELS: PLACEHOLDER_LABELS,
        TEMPLATE_EMPTY_HINT: TEMPLATE_EMPTY_HINT,
        TEMPLATE_SCOPES: TEMPLATE_SCOPES,
        getAllExpandablePaths: getAllExpandablePaths,
        stripLeadingEmoji: stripLeadingEmoji,
        getSessionSchemaEntries: getSessionSchemaEntries,
        findUnknownPlaceholders: findUnknownPlaceholders,
    });
})();
