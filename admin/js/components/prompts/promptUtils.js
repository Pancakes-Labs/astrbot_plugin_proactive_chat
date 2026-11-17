/**
 * 文件职责：提示词管理页的纯函数层。
 *
 * 这里只看归一化、候选池构建、卡盒汇总、概率计算、权重凑整、正文分段。
 *
 * 两个概念必须分清，混用会导致概率口径失真：
 * - Deck（卡盒）：按 scope 互斥归属，一张卡只属于一个盒子，是管理视图。
 * - Pool（候选池）：按 scope 叠加，通用卡同时进私聊池与群聊池，是抽样视图。
 *
 * 关键约定：池构建逻辑必须与后端
 * core/domain/prompt_templates.build_candidate_pool 严格同源，
 * 否则页面显示的概率会与真实抽签结果不一致。
 */

(function () {
    const HEADER_INTENT = '[这次我想]';
    const HEADER_FINAL = '[最终指令]';
    const TEMPLATE_KEY_FIELD = '__template_key';
    const DEFAULT_TEMPLATE_KEY = 'prompt_template';
    const DEFAULT_WEIGHT = 100;

    // 含该占位符的模板是包裹聊天记录的外壳，不是开场白，后端不会拿它抽样。
    const PLATFORM_HISTORY_PAYLOAD = '{{platform_history_lines}}';

    const SCOPE_LABELS = { '': '通用', friend: '仅私聊', group: '仅群聊' };

    // 卡盒顺序固定：私聊 -> 群聊 -> 通用，与页面主视觉从左到右一致。
    const DECK_ORDER = ['friend', 'group', 'any'];

    // 盒内迷你卡位数量：固定 8 个，按剩余张数逐个隐去。
    // 不按数量动态挂载节点，否则张数一减少节点会瞬间消失、没有动画。
    const PEEK_SLOTS = 8;

    // 动画时序的唯一事实源：由 PromptsView 注入 .pv-stage 的 CSS 变量，
    // 卡片飞行、迷你卡隐现、退场卸载全部引用同一组数值，JS 不再猜时间。
    const CARD_LAUNCH_DURATION = 540;
    const CARD_LAUNCH_STEP = 68;
    const CARD_RETRACT_DURATION = 380;
    const CARD_RETRACT_STEP = 58;
    // 收盒起手延迟：盒口迷你卡先从右到左缩回，墙上的卡晚这一小段才起飞。
    const CARD_RETRACT_LEAD = 200;

    const DECK_META = {
        friend: {
            key: 'friend', scope: 'friend', tone: 'friend', emoji: '💬',
            title: '私聊卡盒', short: '私聊',
        },
        group: {
            key: 'group', scope: 'group', tone: 'group', emoji: '👥',
            title: '群聊卡盒', short: '群聊',
        },
        any: {
            key: 'any', scope: '', tone: 'any', emoji: '🌐',
            title: '通用卡盒', short: '通用',
        },
    };

    // 页面内唯一键自增源：后端可能给出重复或空 id，界面需要独立标识。
    let __uiSeed = 0;
    function nextUiKey() {
        __uiSeed += 1;
        return `ui_${__uiSeed}`;
    }

    function createTemplateId() {
        return `tpl_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
    }

    function createEmptyTemplate(scope) {
        return {
            id: createTemplateId(),
            name: '',
            text: `${HEADER_INTENT}\n\n\n${HEADER_FINAL}\n`,
            enabled: true,
            weight: DEFAULT_WEIGHT,
            scope: scope || '',
            [TEMPLATE_KEY_FIELD]: DEFAULT_TEMPLATE_KEY,
        };
    }

    // 受控输入必须拿到完整结构，同时保留未知扩展字段，避免保存时静默丢数据。
    function normalizeItem(item) {
        if (!item || typeof item !== 'object') {
            return { ...createEmptyTemplate(), __ui_key: nextUiKey() };
        }
        const extra = {};
        Object.keys(item).forEach((key) => {
            if (['id', 'name', 'text', 'enabled', 'weight', 'scope',
                TEMPLATE_KEY_FIELD, '__ui_key'].includes(key)) {
                return;
            }
            extra[key] = item[key];
        });
        const weight = Number(item.weight);
        const uiKey = item.__ui_key || nextUiKey();
        return {
            ...extra,
            [TEMPLATE_KEY_FIELD]: item[TEMPLATE_KEY_FIELD] || DEFAULT_TEMPLATE_KEY,
            __ui_key: uiKey,
            // 旧数据可能没有 id。此时用 __ui_key 派生一个稳定 id。
            id: item.id || `tpl_${uiKey}`,
            name: item.name || '',
            text: typeof item.text === 'string' ? item.text : '',
            enabled: item.enabled !== false,
            weight: Number.isFinite(weight) && weight > 0 ? weight : DEFAULT_WEIGHT,
            scope: item.scope || '',
        };
    }

    // 提交前剥掉页面专用字段，避免污染配置。
    function stripUiKey(items) {
        return (items || []).map(({ __ui_key, ...rest }) => rest);
    }

    /**
     * 重新取数后沿用旧的页面内唯一键。
     *
     * 后端不返回 __ui_key，每次 normalizeItem 都会生成新的。
     * id 是后端稳定字段，用它做匹配。
     */
    function adoptUiKeys(prevItems, nextItems) {
        const byId = new Map();
        (prevItems || []).forEach((item) => {
            if (item && item.id) byId.set(item.id, item.__ui_key);
        });
        return (nextItems || []).map((item) => {
            const kept = item && item.id ? byId.get(item.id) : null;
            return kept ? { ...item, __ui_key: kept } : item;
        });
    }

    function isUsableTemplate(item) {
        const text = String(item && item.text ? item.text : '');
        if (!text.trim()) return false;
        return !text.includes(PLATFORM_HISTORY_PAYLOAD);
    }

    /**
     * 构建某个会话类型的真实候选池。
     *
     * scope 是适用范围：本类型专用模板与全部通用模板一起进池，按各自权重共同归一化。
     */
    function buildPool(templates, sessionType) {
        const list = Array.isArray(templates) ? templates : [];
        return list.filter((item) => item.enabled
            && isUsableTemplate(item)
            && (!item.scope || item.scope === sessionType));
    }

    // 池内归一化概率：键为 __ui_key，值与后端 compute_probabilities 同口径。
    function probabilitiesOf(pool) {
        const list = Array.isArray(pool) ? pool : [];
        const map = {};
        if (!list.length) return { map, total: 0 };
        const weights = list.map((item) => Number(item.weight) || 0);
        const total = weights.reduce((sum, value) => sum + value, 0);
        list.forEach((item, index) => {
            map[item.__ui_key] = total > 0
                ? weights[index] / total
                : 1 / list.length;
        });
        return { map, total };
    }

    // 模板抽不到的原因。
    function invalidReason(item) {
        if (!item.enabled) return '已停用';
        if (!String(item.text || '').trim()) return '正文为空';
        if (String(item.text || '').includes(PLATFORM_HISTORY_PAYLOAD)) {
            return '含流水载荷，不参与抽样';
        }
        return '';
    }

    // 概率段：宽度即该卡在本盒口径下的占比，用于卡盒表面的能量光谱。
    function ratioSegments(items, ratioOf) {
        return (items || [])
            .map((item) => ({
                uiKey: item.__ui_key,
                name: item.name || '未命名',
                weight: Number(item.weight) || 0,
                ratio: ratioOf(item) || 0,
            }))
            .filter((seg) => seg.ratio > 0)
            .sort((a, b) => b.ratio - a.ratio);
    }

    /**
     * 卡盒里还剩几张。
     *
     * lifted 是当前摊在桌面上的可见张数（搜索过滤之后的数量）。
     * 被搜索隐藏的卡并没有飞出来，仍然算在盒里，所以直接相减即可。
     */
    function remainingOf(deck, lifted) {
        if (!deck) return 0;
        return Math.max(0, deck.total - Math.max(0, Number(lifted) || 0));
    }

    // 卡片搜索匹配：名称或正文命中即算，与旧版卡面过滤口径保持一致。
    function matchKeyword(item, keyword) {
        const kw = String(keyword || '').trim().toLowerCase();
        if (!kw) return true;
        return String((item && item.name) || '').toLowerCase().includes(kw)
            || String((item && item.text) || '').toLowerCase().includes(kw);
    }

    /**
     * 汇总三个卡盒。
     *
     * 每个卡盒只带一个概率口径，保证一盒一条光谱，永不溢出：
     * - 私聊/群聊盒：卡片按 scope 归属，占比取自各自真实池的概率；
     * - 通用盒：通用卡没有自己的池，光谱改用盒内权重占比，
     *   但每张卡额外携带 [私聊概率, 群聊概率] 两个真实值。
     *
     * poolKeys 是本盒「凑整 100」需要一起缩放的全部条目。
     */
    function buildDecks(templates) {
        const list = Array.isArray(templates) ? templates : [];
        const friendPool = buildPool(list, 'friend');
        const groupPool = buildPool(list, 'group');
        const friendStats = probabilitiesOf(friendPool);
        const groupStats = probabilitiesOf(groupPool);

        const friendItems = list.filter((item) => item.scope === 'friend');
        const groupItems = list.filter((item) => item.scope === 'group');
        const anyItems = list.filter((item) => !item.scope);

        const poolProb = (items, map) => {
            const prob = {};
            items.forEach((item) => { prob[item.__ui_key] = map[item.__ui_key] || 0; });
            return prob;
        };

        // 通用卡的真实概率是两个池各一份，不能只取其一。
        const anyProb = {};
        anyItems.forEach((item) => {
            anyProb[item.__ui_key] = [
                friendStats.map[item.__ui_key] || 0,
                groupStats.map[item.__ui_key] || 0,
            ];
        });

        // 通用盒没有独立池，占比一律按盒内有效卡权重归一。
        const validAnyItems = anyItems.filter((cand) => !invalidReason(cand));
        const anyWeightTotal = validAnyItems.reduce(
            (sum, cand) => sum + (Number(cand.weight) || 0), 0,
        );
        const weightShare = (item) => (
            anyWeightTotal > 0 ? (Number(item.weight) || 0) / anyWeightTotal : 0
        );

        // 盒内占比：分母只算本盒有效卡，用于卡片上的占比读数。
        // 它与池概率是两套口径，卡片据此判断该不该同时显示两个数。
        const shareOf = (items) => {
            const valid = (items || []).filter((cand) => !invalidReason(cand));
            const total = valid.reduce((sum, cand) => sum + (Number(cand.weight) || 0), 0);
            const map = {};
            (items || []).forEach((item) => {
                const weight = Number(item.weight) || 0;
                map[item.__ui_key] = (total > 0 && !invalidReason(item))
                    ? weight / total
                    : 0;
            });
            return map;
        };

        const buildDeck = (
            meta, items, prob, segments, poolKeys, weight, weightLabel, poolSegments,
        ) => ({
            ...meta,
            items,
            total: items.length,
            enabled: items.filter((item) => item.enabled).length,
            valid: items.filter((item) => !invalidReason(item)).length,
            weight,
            weightLabel,
            // segments 是盒面光谱：只画本盒 scope 的卡，保证一盒一条。
            segments,
            // poolSegments 是透视抽屉用的完整候选池，含混进池里的通用卡。
            poolSegments,
            prob,
            share: shareOf(items),
            peekTotal: items.length,
            // 空数组表示这个盒子没有独立候选池，不能做凑整。
            poolKeys,
        });

        return {
            friend: buildDeck(
                DECK_META.friend,
                friendItems,
                poolProb(friendItems, friendStats.map),
                ratioSegments(friendItems, (item) => friendStats.map[item.__ui_key]),
                friendPool.map((item) => item.__ui_key),
                friendStats.total,
                '池权重',
                ratioSegments(friendPool, (item) => friendStats.map[item.__ui_key]),
            ),
            group: buildDeck(
                DECK_META.group,
                groupItems,
                poolProb(groupItems, groupStats.map),
                ratioSegments(groupItems, (item) => groupStats.map[item.__ui_key]),
                groupPool.map((item) => item.__ui_key),
                groupStats.total,
                '池权重',
                ratioSegments(groupPool, (item) => groupStats.map[item.__ui_key]),
            ),
            any: buildDeck(
                DECK_META.any,
                anyItems,
                anyProb,
                ratioSegments(validAnyItems, weightShare),
                [],
                anyWeightTotal,
                '权重',
                ratioSegments(validAnyItems, weightShare),
            ),
        };
    }

    /**
     * 把一组条目的权重按比例缩放到总和 100（最大余数法）。
     * 
     * 返回 { uiKey: weight } 映射。
     */
    function normalizeToHundred(items) {
        const list = Array.isArray(items) ? items : [];
        if (!list.length) return {};

        const raw = list.map((item) => Math.max(1, Math.round(Number(item.weight) || 1)));
        const count = list.length;
        // 条目超过 100 时无法既保证每条 >=1 又保证总和 100，此时以条目数为准。
        const target = Math.max(100, count);
        const sourceTotal = raw.reduce((sum, value) => sum + value, 0);
        const scaled = raw.map((value) => (value * target) / sourceTotal);

        const result = scaled.map((value) => Math.max(1, Math.floor(value)));
        let sum = result.reduce((acc, value) => acc + value, 0);

        if (sum < target) {
            // 小数部分大的先补，保证分配误差最小。
            const order = scaled
                .map((value, index) => ({ index, frac: value - Math.floor(value) }))
                .sort((a, b) => b.frac - a.frac);
            let need = target - sum;
            let cursor = 0;
            while (need > 0) {
                result[order[cursor % order.length].index] += 1;
                need -= 1;
                cursor += 1;
            }
        } else if (sum > target) {
            // 只从还有余量的条目里扣，且扣到 1 就停，避免出现 0。
            const order = scaled
                .map((value, index) => ({ index, frac: value - Math.floor(value) }))
                .sort((a, b) => a.frac - b.frac);
            let excess = sum - target;
            let cursor = 0;
            let guard = 0;
            const limit = order.length * target + 10;
            while (excess > 0 && guard < limit) {
                const slot = order[cursor % order.length].index;
                if (result[slot] > 1) {
                    result[slot] -= 1;
                    excess -= 1;
                }
                cursor += 1;
                guard += 1;
            }
        }

        const map = {};
        list.forEach((item, index) => {
            map[item.__ui_key] = result[index];
        });
        return map;
    }

    /**
     * 把模板正文切成可读气泡段。
     *
     * 用户自建或旧迁移模板可能完全没有段头，或只有其中一个，
     * 因此兜底成单个正文段，不返回空数组。
     */
    function splitTemplateSections(text) {
        const body = String(text || '');
        if (!body.trim()) return [];

        const intentAt = body.indexOf(HEADER_INTENT);
        const finalAt = body.indexOf(HEADER_FINAL);

        if (intentAt >= 0 && finalAt > intentAt) {
            const intro = body.slice(0, intentAt).trim();
            const intent = body.slice(intentAt + HEADER_INTENT.length, finalAt).trim();
            const final = body.slice(finalAt + HEADER_FINAL.length).trim();
            const sections = [];
            if (intro) sections.push({ key: 'intro', label: '前置说明', text: intro });
            if (intent) sections.push({ key: 'intent', label: '这次我想', text: intent });
            if (final) sections.push({ key: 'final', label: '最终指令', text: final });
            if (sections.length) return sections;
        }

        if (finalAt === 0) {
            const final = body.slice(HEADER_FINAL.length).trim();
            if (final) return [{ key: 'final', label: '最终指令', text: final }];
        }

        if (intentAt === 0 && finalAt < 0) {
            const intent = body.slice(HEADER_INTENT.length).trim();
            if (intent) return [{ key: 'intent', label: '这次我想', text: intent }];
        }

        return [{ key: 'body', label: '正文', text: body.trim() }];
    }

    // 正文格式检查：占位符白名单以后端 PLACEHOLDER_SPECS 为唯一事实源。
    function checkTextIssues(text) {
        const issues = [];
        const body = String(text || '');
        if (!body.trim()) {
            issues.push('正文为空，这条模板不会参与抽样。');
            return issues;
        }
        if (body.includes(PLATFORM_HISTORY_PAYLOAD)) {
            issues.push(`正文含 ${PLATFORM_HISTORY_PAYLOAD}，那是包裹聊天记录的载荷，`
                + '不是开场白，该条不会参与抽样。');
        }
        if (!body.trimStart().startsWith('[') && body.includes(HEADER_FINAL)) {
            issues.push(`正文不以 [ 开头却内含 ${HEADER_FINAL}，实际注入时可能出现重复标题。`);
        }
        const shared = window.ConfigShared;
        if (shared && typeof shared.findUnknownPlaceholders === 'function') {
            const unknown = shared.findUnknownPlaceholders(body);
            if (unknown.length) {
                issues.push(`无法识别的占位符：${unknown.join('、')}`);
            }
        }
        return issues;
    }

    // 占位符清单直接复用配置页的公共常量，新增占位符时无需改本文件。
    function placeholderEntries() {
        const shared = window.ConfigShared;
        const labels = (shared && shared.PLACEHOLDER_LABELS) || {};
        return Object.entries(labels);
    }

    function scopeLabel(scope) {
        return SCOPE_LABELS[scope] || '通用';
    }

    function formatPercent(value) {
        const num = Number(value);
        if (!Number.isFinite(num) || num <= 0) return '0%';
        const pct = num * 100;
        if (pct < 0.5) return '<1%';
        return `${Math.round(pct * 10) / 10}%`;
    }

    window.PromptUtils = Object.freeze({
        CARD_LAUNCH_DURATION,
        CARD_LAUNCH_STEP,
        CARD_RETRACT_DURATION,
        CARD_RETRACT_STEP,
        CARD_RETRACT_LEAD,
        DECK_META,
        DECK_ORDER,
        HEADER_FINAL,
        HEADER_INTENT,
        PEEK_SLOTS,
        PLATFORM_HISTORY_PAYLOAD,
        SCOPE_LABELS,
        adoptUiKeys,
        buildDecks,
        buildPool,
        checkTextIssues,
        createEmptyTemplate,
        createTemplateId,
        formatPercent,
        invalidReason,
        isUsableTemplate,
        matchKeyword,
        normalizeItem,
        normalizeToHundred,
        nextUiKey,
        placeholderEntries,
        probabilitiesOf,
        remainingOf,
        scopeLabel,
        splitTemplateSections,
        stripUiKey,
    });
})();
