(() => {
/**
 * 文件职责：提示词管理页的编排层与唯一状态源。
 *
 * 两层纵深，任一时刻只有一层活跃：卡盒架 -> 卡片放大态。
 * 卡池透视是覆盖式抽屉，层级低于放大态、高于舞台。
 *
 * 状态集中在这里的原因：卡盒要显示「还剩几张」，就必须知道卡片区摊开了多少张，
 * 而盒子与卡片区是兄弟节点，谁都不该去猜对方的数量。
 *
 *   openDeck   当前摊开的卡盒，null 表示全部收纳；
 *   keyword    搜索词，只影响可见性，不影响概率口径；
 *   poolOpen   卡池透视抽屉；
 *   editingKey 正在放大的卡牌。
 *
 * 可见张数由父层用同一个 matchKeyword 自己算：靠子组件回传要多一拍 effect，
 * 首个渲染帧盒子会以为卡还在，迷你卡会闪一下。
 */

const {
    Box, Typography, Button, TextField, Menu, MenuItem, Divider,
    CircularProgress, Alert, Tooltip, IconButton,
} = MaterialUI;
const {
    useState, useEffect, useMemo, useCallback, useRef, useLayoutEffect,
} = React;
const { createPortal } = ReactDOM;

const {
    DECK_ORDER, DECK_META, adoptUiKeys, buildDecks, createEmptyTemplate,
    invalidReason, isUsableTemplate, matchKeyword, mergeSavedTemplates,
    normalizeItem, normalizeToHundred, scopeLabel, stripUiKey,
    CARD_LAUNCH_DURATION, CARD_LAUNCH_STEP,
    CARD_RETRACT_DURATION, CARD_RETRACT_STEP, CARD_RETRACT_LEAD,
} = window.PromptUtils;
const {
    DeckBox, DeckSection, PromptDrawer, PoolDrawer,
} = window.PromptComponents;

// 模态放大：卡片宽约 288px，模态宽约 1080px，比值约 0.27。
// 用固定值而不是实时测量，卡片宽度一致时能让缩放节奏在换卡时保持稳定。
const MODAL_SCALE = 0.27;

// 舞台高度预算用的几何常量，必须与 prompts.css 保持一致：
// .pv-card-grid 的 minmax(288px, 1fr) 与 gap 16，
// .pv-card 的 min-height 330，.pv-stage 的 gap 18。
// 三者任一变动都要同步，否则洞洞板会在开盒瞬间继续变长。
const CARD_MIN_WIDTH = 288;
const CARD_MIN_HEIGHT = 330;
// .pv-stage 的 padding-top：木架与舞台顶的呼吸空隙，链条吊环钉在这段墙上。
// 必须与 prompts.css 的 --pv-headroom 一致。
const STAGE_HEADROOM = 28;
// .pv-card-slot 的 padding-top：挂钩露出段。卡片本身只有 330，
// 但网格单元格高度是 330 + 16，漏算这一段会让预算少一行的高度。
const CARD_SLOT_TOP_PAD = 16;
const CARD_GRID_GAP = 16;
// .pv-stage 的 gap 18（见 prompts.css 的 .pv-stage）＋
// .pv-field 自身的上下 padding（2 + 6 = 8）。
const STAGE_FIELD_GAP = 18;
const STAGE_FIELD_TAIL = 8;
// .pv-field 的 padding-top（见 prompts.css 的 .pv-field：2px 0 6px）。
// 挂钩层是 stage 的绝对定位子元素，必须自己把这个 2px 算进来，
// 否则整排挂钩会比卡片高 2px，孔与杆就不同轴了。
const FIELD_PAD_TOP = 2;
const HOOK_TOP_OFFSET = STAGE_FIELD_GAP + FIELD_PAD_TOP;
// 行数上限：卡盒有几十张时不能把整页撑成几千像素，超出部分交给页面滚动。
const STAGE_MAX_ROWS = 6;

// 必须与 prompts.css 的 pv-modal-out 时长一致，否则退场会被截断。
const MODAL_OUT_DURATION = 320;

// 展开态记忆：刷新页面、切页回来都保持上次摊开的卡盒。
const DECK_MEMORY_KEY = 'proactive.prompts.openDeck';

function readDeckMemory() {
    try {
        const saved = window.localStorage.getItem(DECK_MEMORY_KEY);
        return DECK_ORDER.includes(saved) ? saved : null;
    } catch (e) {
        return null;
    }
}

// 徽章的说明文案：都在解释「这个数是怎么算的」，不放空洞形容词。
const PILL_HINTS = {
    total: '三个卡盒里的模板总数',
    enabled: '启用开关打开的数量，其中正文为空或含流水载荷的仍不会被抽到',
    usable: '真正进入抽签的数量：已启用、正文非空、且不含聊天记录载荷',
};

/**
 * 一排挂钩。
 *
 * 挂钩是纯装饰，不参与交互，也不依赖卡盒是否展开 —— 由舞台按
 * STAGE_MAX_ROWS 行常驻渲染，每行一个实例，位置由 hookTop + 行距算出来。
 *
 * variant:
 *   'shank'  杆身 + 插孔，画在卡片之下（卡片 mask 打孔后从孔里透出来）
 *   'tip'    只有钩尖，画在卡片之上（压在卡面上，读作穿出来翘在卡上）
 *
 * 格子数量由列宽决定：CSS 用 auto-fill + minmax(288px, 1fr)，
 * 这里只需把格子铺满一行，多出来的会被舞台 overflow 裁掉，
 * 与 .pv-card-grid 的列数天然一致。
 */
const HOOK_CELLS_PER_ROW = 12;

function HookRow({ variant }) {
    return (
        <>
            {Array.from({ length: HOOK_CELLS_PER_ROW }).map((unused, index) => (
                <Box className="pv-hook-cell" key={index}>
                    <span className={variant === 'tip' ? 'pv-hook-tip' : 'pv-hook-shank'} />
                </Box>
            ))}
        </>
    );
}

function timeLabel(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function PromptsView() {
    const api = useApi();
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [templates, setTemplates] = useState([]);
    const [dirty, setDirty] = useState(false);
    const [savedAt, setSavedAt] = useState(null);

    const [openDeck, setOpenDeck] = useState(readDeckMemory);
    // 挂载即落位：切页回来不重播飞入，只有用户换盒时才由 commitDeck 打开动画。
    const [deckInstant, setDeckInstant] = useState(true);
    const [closing, setClosing] = useState(false);
    const [editingKey, setEditingKey] = useState(null);
    const [modalOrigin, setModalOrigin] = useState(null);
    const [modalClosing, setModalClosing] = useState(false);
    // 新建卡草稿：点「新建卡牌」只打开一张待填的卡，不往列表里塞东西。
    // 只有用户在模态里真的点了保存，它才会 merge 进 templates 并提交后端。
    // 之前是「点按钮即无条件生成一张空卡」，用户只是手滑点一下，墙上就多一张。
    const [draft, setDraft] = useState(null);
    const pendingDeckRef = useRef(null);
    // 跨盒新建时先收盒再开编辑，避免模态和退场动画同时播放。
    const pendingEditRef = useRef(null);
    const modalTimerRef = useRef(0);

    const [keyword, setKeyword] = useState('');
    const [searchOpen, setSearchOpen] = useState(false);

    const [poolOpen, setPoolOpen] = useState(false);

    const [preview, setPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);

    const [simulateResult, setSimulateResult] = useState(null);
    const [simulating, setSimulating] = useState(false);

    const [menuAnchor, setMenuAnchor] = useState(null);
    const [newMenuAnchor, setNewMenuAnchor] = useState(null);
    const [importOpen, setImportOpen] = useState(false);
    const [importText, setImportText] = useState('');

    const dirtyRef = useRef(false);
    const templatesRef = useRef(templates);
    // 草稿也必须走 ref：patchByKey / 保存回调都在事件里同步读取当前值，
    // 只靠 state 会读上一帧的旧对象，把刚敲的字覆盖掉。
    const draftRef = useRef(null);
    useEffect(() => { dirtyRef.current = dirty; }, [dirty]);
    useEffect(() => { templatesRef.current = templates; }, [templates]);

    /** 同时写 ref 与 state：草稿需要「立刻可读、随后重渲染」。 */
    const setDraftNow = useCallback((next) => {
        draftRef.current = next;
        setDraft(next);
    }, []);

    // 加载序号：首屏加载与多次广播可能并发，只有最新请求能写状态。
    const loadSeqRef = useRef(0);

    const load = useCallback(async (silent) => {
        loadSeqRef.current += 1;
        const seq = loadSeqRef.current;
        if (!silent) {
            setLoading(true);
            setError('');
        }
        try {
            const data = await api.listPrompts();
            if (seq !== loadSeqRef.current) return;
            // 有未保存改动、或有一张还没提交的草稿时，不接受后台同步：
            // 否则用户刚填的内容会凭空消失。草稿不计入 dirty，
            // 所以这里必须单独判一次 draftRef。
            if (silent && (dirtyRef.current || draftRef.current)) return;
            // 沿用旧的页面内键：否则重新取数后 editingKey 指向已不存在的条目，
            // 正在编辑的模态会凭空消失（这是之前"展开态丢失"的根因）。
            setTemplates((prev) => adoptUiKeys(prev, (data.templates || []).map(normalizeItem)));
            // 重新取数等于回到服务端真相：没保存的草稿必须一起丢掉，
            // 否则它会在下一次保存时被悄悄带进去。
            setDraftNow(null);
            setDirty(false);
            setSavedAt(Date.now());
        } catch (e) {
            if (seq !== loadSeqRef.current) return;
            if (!silent) setError(e.message || '加载提示词失败');
        } finally {
            if (seq === loadSeqRef.current) setLoading(false);
        }
    }, [api]);

    useEffect(() => { load(false); }, [load]);

    // 配置页保存或本页保存后后端都会广播 update，这里主动同步。
    useEffect(() => {
        const onUpdated = (event) => {
            const reason = event && event.detail ? event.detail.reason : '';
            if (reason && !['config', 'prompts', 'session-config'].includes(reason)) return;
            load(true);
        };
        window.addEventListener('proactive:data-updated', onUpdated);
        return () => window.removeEventListener('proactive:data-updated', onUpdated);
    }, [load]);

    const decks = useMemo(() => buildDecks(templates), [templates]);
    const deck = openDeck ? decks[openDeck] : null;

    const enabledCount = useMemo(
        () => templates.filter((item) => item.enabled).length,
        [templates],
    );

    // 可抽张数：进了任意池的卡（启用 + 正文有效 + 不含流水载荷）。
    const usableCount = useMemo(
        () => templates.filter((item) => item.enabled && isUsableTemplate(item)).length,
        [templates],
    );

    // 展开态落盘：刷新、切页回来都能恢复到上次摊开的盒子。
    const commitDeck = useCallback((key) => {
        setOpenDeck(key);
        // 从这里开始的每次换盒都是用户操作，需要完整动画。
        setDeckInstant(false);
        try {
            if (key) window.localStorage.setItem(DECK_MEMORY_KEY, key);
            else window.localStorage.removeItem(DECK_MEMORY_KEY);
        } catch (e) {
            // 隐私模式下 localStorage 可能抛错，记忆失效不影响主流程。
        }
    }, []);

    const handleDeckClick = useCallback((key) => {
        setSimulateResult(null);
        // 退场窗口内忽略重复点击，否则会打乱逐张收回的节奏。
        if (closing) return;

        if (openDeck === key) {
            setClosing(true);
            return;
        }
        if (openDeck) {
            setClosing(true);
            pendingDeckRef.current = key;
            return;
        }
        commitDeck(key);
    }, [openDeck, closing, commitDeck]);

    // 退场结束：这时才真正换盒或收起。
    const handleClosed = useCallback(() => {
        commitDeck(pendingDeckRef.current);
        pendingDeckRef.current = null;
        setClosing(false);
        // 收盒动画播完才把新建的卡放大，两段动画首尾相接。
        if (pendingEditRef.current) {
            setEditingKey(pendingEditRef.current);
            setModalOrigin(null);
            pendingEditRef.current = null;
        }
    }, [commitDeck]);

    // 摊开卡盒里当前可见的张数：与 DeckSection 用同一个 matchKeyword，口径完全一致。
    const visibleCount = useMemo(() => {
        if (!deck) return 0;
        if (!keyword.trim()) return deck.items.length;
        return deck.items.filter((item) => matchKeyword(item, keyword)).length;
    }, [deck, keyword]);

    // 三个盒里最大的那张数：舞台高度按它预算，任何一个盒打开都不会再长高。
    const maxDeckCount = useMemo(
        () => DECK_ORDER.reduce((max, key) => Math.max(max, decks[key].total), 0),
        [decks],
    );

    const editingItem = useMemo(() => {
        // 草稿还没进表，必须单独查一次，否则新建模态一打开就是空的。
        if (draft && draft.__ui_key === editingKey) return draft;
        return templates.find((item) => item.__ui_key === editingKey) || null;
    }, [templates, editingKey, draft]);

    const editingKeyRef = useRef(null);
    useEffect(() => { editingKeyRef.current = editingKey; }, [editingKey]);

    /**
     * 打开卡片：先量出它到屏幕中心的距离，模态从这个位置长出来。
     * 此时卡片处于落位态（动画已 fill 到 scale(1)），rect 就是真实位置。
     */
    const openCard = useCallback((item, element) => {
        window.clearTimeout(modalTimerRef.current);
        setModalClosing(false);
        setPreview(null);
        if (element && typeof element.getBoundingClientRect === 'function') {
            const rect = element.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            setModalOrigin({
                dx: Math.round(cx - window.innerWidth / 2),
                dy: Math.round(cy - window.innerHeight / 2),
                k: MODAL_SCALE,
            });
        } else {
            setModalOrigin(null);
        }
        setEditingKey(item.__ui_key);
    }, []);

    /**
     * 关闭模态。所有关闭路径都走这里：先挂 is-closing 播缩回动画，
     * 动画结束才真正卸载，避免退场途中组件被卸载导致动画被截断。
     */
    const closeModal = useCallback(() => {
        if (modalClosing) return;
        setModalClosing(true);
        window.clearTimeout(modalTimerRef.current);
        modalTimerRef.current = window.setTimeout(() => {
            setEditingKey(null);
            setModalClosing(false);
            // 没提交的草稿随模态一起丢弃：它从没进过列表，
            // 关掉之后不该在墙上留一张空卡。
            setDraftNow(null);
        }, MODAL_OUT_DURATION);
    }, [modalClosing, setDraftNow]);

    useEffect(() => () => window.clearTimeout(modalTimerRef.current), []);

    // ---- 盒口锚点 ----

    const stageRef = useRef(null);
    const shelfRef = useRef(null);
    const mouthRefs = useRef({});
    const [mouthAnchor, setMouthAnchor] = useState(null);

    /**
     * 测量盒口锚点。
     *
     * 卡牌区与卡盒同在 .pv-stage 内，两边都用「相对 stage 的偏移」计算，
     * 差值就是纯粹的相对位移，不受页面滚动影响。
     * 盒口的 offset 系列属性读的是布局盒，不受 rotateX 等变换影响。
     */
    const measureAnchor = useCallback(() => {
        const stage = stageRef.current;
        const mouth = openDeck ? mouthRefs.current[openDeck] : null;
        if (!stage || !mouth) {
            setMouthAnchor(null);
            return;
        }
        let x = 0;
        let y = 0;
        let node = mouth;
        while (node && node !== stage) {
            x += node.offsetLeft;
            y += node.offsetTop;
            node = node.offsetParent;
        }
        setMouthAnchor({
            x: x + mouth.offsetWidth / 2,
            y: y + mouth.offsetHeight / 2,
        });
    }, [openDeck]);

    useLayoutEffect(() => { measureAnchor(); }, [measureAnchor, decks]);

    // ---- 舞台高度预算 ----

    // 洞洞板墙面必须提前铺满「打开卡盒后」的长度，否则开盒瞬间墙会被撑长，
    // 背景看起来是「后来才延伸出来的」。这里按最宽的那一盒预先算出所需高度。
    const [stageMinHeight, setStageMinHeight] = useState(0);
    // 挂钩层的 top：相对 .pv-stage，等于「货架高度 + 舞台间距 + 卡片区上内距」。
    // 挂钩由舞台常驻渲染，不依赖 openDeck，所以位置只能由这里算出来注入。
    const [hookTop, setHookTop] = useState(0);

    const measureStageBudget = useCallback(() => {
        const stage = stageRef.current;
        const shelf = shelfRef.current;
        if (!stage || !shelf) return;

        const shelfHeight = shelf.offsetHeight;
        if (!shelfHeight) return;

        // 与 .pv-card-grid 同口径的数行算法：auto-fill + minmax(288px, 1fr)。
        const fieldWidth = stage.clientWidth;
        const cols = Math.max(
            1,
            Math.floor((fieldWidth + CARD_GRID_GAP) / (CARD_MIN_WIDTH + CARD_GRID_GAP)),
        );
        const rows = Math.min(
            STAGE_MAX_ROWS,
            Math.ceil(Math.max(maxDeckCount, 1) / cols),
        );
        // 网格里还额外有一个「新建卡位」，它可能单独占一行，按 +1 预留。
        const slotRows = Math.min(
            STAGE_MAX_ROWS,
            Math.ceil((Math.max(maxDeckCount, 1) + 1) / cols),
        );
        const gridRows = Math.max(rows, slotRows);
        // 单元高度 = 卡片最小高 + 挂钩露出段；gap 只存在于行与行之间，
        // 所以按 (行数 - 1) 计，不能乘行数（那会多算一整行间距）。
        const fieldHeight = gridRows * (CARD_MIN_HEIGHT + CARD_SLOT_TOP_PAD)
            + Math.max(0, gridRows - 1) * CARD_GRID_GAP;

        setStageMinHeight(
            Math.round(
                STAGE_HEADROOM + shelfHeight + STAGE_FIELD_GAP + fieldHeight + STAGE_FIELD_TAIL,
            ),
        );
        // 卡片顶边相对 stage 的位置 = 舞台顶部留白 + 货架高 + 舞台 gap + 卡片区 padding-top(2)。
        // 挂钩层的格子与卡片网格同列同行高，top 对上之后孔与杆必然同轴。
        setHookTop(Math.round(STAGE_HEADROOM + shelfHeight + HOOK_TOP_OFFSET));
    }, [maxDeckCount]);

    useLayoutEffect(() => { measureStageBudget(); }, [measureStageBudget, decks]);

    /**
     * 盒架尺寸变化也要重算锚点。
     * 只在 window.resize 里测是不够的：页头从两行折成一行、控制栏换行，
     * 都会改变卡盒位置却不触发 resize，锚点就会指错方向。
     */
    useEffect(() => {
        const onResize = () => measureAnchor();
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
    }, [measureAnchor]);

    // 一个观察器同时处理两件事：盒口锚点与舞台高度预算。
    // 拆成两个 ResizeObserver 会在同一帧里触发两次布局，卡片飞行位移会抖一下。
    useEffect(() => {
        const shelf = shelfRef.current;
        const stage = stageRef.current;
        if (!shelf || !stage || typeof ResizeObserver === 'undefined') return undefined;
        let frame = 0;
        const observer = new ResizeObserver(() => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => {
                measureAnchor();
                measureStageBudget();
            });
        });
        observer.observe(shelf);
        observer.observe(stage);
        return () => {
            cancelAnimationFrame(frame);
            observer.disconnect();
        };
    }, [measureAnchor, measureStageBudget]);

    // 卡片上的概率按条目自身 scope 取，与当前打开的是哪个盒无关。
    const probabilityOf = useCallback((item) => {
        // 草稿不在 decks 里，算不出真实概率。返回 null 而不是 0，
        // 让编辑器显示「保存后计算」—— 0% 会被误读成「这张卡抽不到」。
        // draft 必须进依赖：否则新草稿会落到下面的数值分支显示 0%。
        if (draft && item && draft.__ui_key === item.__ui_key) return null;
        if (!item.scope) {
            const anyProb = decks.any && decks.any.prob[item.__ui_key];
            if (Array.isArray(anyProb)) return anyProb;
            return [
                (decks.friend && decks.friend.prob[item.__ui_key]) || 0,
                (decks.group && decks.group.prob[item.__ui_key]) || 0,
            ];
        }
        const target = decks[item.scope];
        return (target && target.prob[item.__ui_key]) || 0;
    }, [decks, draft]);

    const shareOf = useCallback((item) => {
        const target = decks[item.scope || 'any'];
        return (target && target.share[item.__ui_key]) || 0;
    }, [decks]);

    const indexOf = useCallback((item) => {
        const target = openDeck ? decks[openDeck] : null;
        if (!target) return 0;
        return Math.max(0, target.items.findIndex((row) => row.__ui_key === item.__ui_key));
    }, [decks, openDeck]);

    // ---- 写操作 ----

    /**
     * 改一张卡。
     *
     * 草稿还没进 templates，必须走单独分支：它既不能被 setTemplates 找不到而静默丢弃，
     * 也不该把 dirty 置真 —— 草稿尚未进表，页头那个「保存」按钮不该为它亮起来。
     * 草稿的落库由模态里的「保存并关闭」触发（见 save）。
     */
    const patchByKey = useCallback((uiKey, patch) => {
        const pending = draftRef.current;
        if (pending && pending.__ui_key === uiKey) {
            setDraftNow({ ...pending, ...patch });
            return;
        }
        setTemplates((prev) => prev.map((item) => (
            item.__ui_key === uiKey ? { ...item, ...patch } : item
        )));
        setDirty(true);
        // 权重或正文变了，旧的预演结果就不再对应这份配置，作废。
        setSimulateResult(null);
    }, [setDraftNow]);

    const removeByKey = useCallback((uiKey) => {
        // 草稿从未进表，删除它就是丢弃：不用确认，也不该把页面标脏。
        const pending = draftRef.current;
        if (pending && pending.__ui_key === uiKey) {
            setDraftNow(null);
            setEditingKey((prev) => (prev === uiKey ? null : prev));
            return true;
        }
        const target = templatesRef.current.find((item) => item.__ui_key === uiKey);
        if (target && !window.confirm(`确定删除「${target.name || '未命名模板'}」？`)) return false;
        setTemplates((prev) => prev.filter((item) => item.__ui_key !== uiKey));
        setDirty(true);
        setEditingKey((prev) => (prev === uiKey ? null : prev));
        return true;
    }, [setDraftNow]);

    /**
     * 新建卡牌。
     *
     * 只准备一张「草稿」并把模态打开，绝不往 templates 里塞东西。
     * 用户点了模态里的保存，草稿才会 merge 进模板表并提交后端（见 save）。
     * 直接关掉模态 = 草稿连同节点一起丢弃，墙上不会留下一张空卡。
     *
     * 已开盒时 scope 由盒子决定；未开盒时先让用户选一个池，选完顺带打开那个盒。
     */
    const createCard = useCallback((scope) => {
        // 退场动画进行中时先取消那次卸载：否则刚打开的模态会被同一支定时器
        // 在 320ms 后连 editingKey 一起清掉，用户会看到它闪一下就消失。
        if (modalClosing) {
            window.clearTimeout(modalTimerRef.current);
            setModalClosing(false);
        }
        // 已经有一张没保存的草稿时，直接回到那张草稿：
        // 再生成一张会把用户刚填了一半的内容无声丢掉。
        const pending = draftRef.current;
        if (pending) {
            setEditingKey(pending.__ui_key);
            setModalOrigin(null);
            return;
        }
        const created = normalizeItem(createEmptyTemplate(scope || ''));
        setDraftNow(created);
        const targetKey = scope || 'any';
        // 已经在退场中时不能再 commitDeck：那会把正在播放退场动画的盒直接换掉，
        // 卡片会在一半位置凭空消失。排队等 onClosed 统一处理。
        if (closing) {
            pendingDeckRef.current = openDeck === targetKey ? null : targetKey;
            pendingEditRef.current = created.__ui_key;
            return;
        }
        if (openDeck && openDeck !== targetKey) {
            pendingDeckRef.current = targetKey;
            pendingEditRef.current = created.__ui_key;
            setClosing(true);
            return;
        }
        if (!openDeck) commitDeck(targetKey);
        setEditingKey(created.__ui_key);
        setModalOrigin(null);
    }, [openDeck, closing, modalClosing, commitDeck, setDraftNow]);

    const handleAdd = useCallback((event) => {
        if (deck) {
            createCard(deck.scope);
            return;
        }
        // 未开盒：弹出作用域选择。
        setNewMenuAnchor(event ? event.currentTarget : null);
    }, [deck, createCard]);

    /**
     * 权重凑整 100。
     *
     * 缩放的是「本盒所属候选池的全部条目」，也就是包含混进池里的通用卡。
     * 只缩放本盒 scope 的条目会让总和变成 100 + 通用卡的权重。
     */
    const normalizeDeckWeights = useCallback(() => {
        if (!deck || !deck.poolKeys.length) return;
        const keySet = new Set(deck.poolKeys);
        const pool = templatesRef.current.filter((item) => (
            keySet.has(item.__ui_key) && !invalidReason(item)
        ));
        if (!pool.length) return;
        const nextWeights = normalizeToHundred(pool);
        setTemplates((prev) => prev.map((item) => (
            Object.prototype.hasOwnProperty.call(nextWeights, item.__ui_key)
                ? { ...item, weight: nextWeights[item.__ui_key] }
                : item
        )));
        setDirty(true);
        setSimulateResult(null);
        if (window.Toast) {
            window.Toast.success(
                `${pool.length} 张卡权重已缩放到总和 100`,
                { title: '凑整完成' },
            );
        }
    }, [deck]);

    // ---- 保存 / 导入导出 ----

    /**
     * 提交给后端前的模板快照：草稿存在时把它接在末尾。
     *
     * 保存、预览、抽样预演三条路径都必须用同一份快照，
     * 否则预览里点不到这张新卡（它的 id 还不在服务端列表里），
     * 而保存之后它又突然出现在概率面板上 —— 用户会以为数字乱跳。
     */
    const submittedTemplates = useCallback(() => {
        const pending = draftRef.current;
        const list = templatesRef.current;
        if (!pending) return list;
        if (list.some((item) => item.__ui_key === pending.__ui_key)) return list;
        return [...list, pending];
    }, []);

    const save = async () => {
        setSaving(true);
        setError('');
        loadSeqRef.current += 1;
        // 草稿在这里才真正入表：这是「用户点了保存」的唯一落地点。
        // 后端 normalize_templates 会原样保留客户端生成的 id，
        // 因此保存回来之后 adoptUiKeys 仍能把 __ui_key 对回去，
        // 正在编辑的模态不会因为 id 变化被卸载。
        const pending = draftRef.current;
        // 提交前的快照：回来时用它判断这期间用户有没有继续改东西。
        const snapshot = templatesRef.current;
        const submitted = submittedTemplates();
        try {
            const data = await api.updatePrompts(stripUiKey(submitted));
            // 保存会返回全新条目，必须回收旧键，否则 editingKey 失效、模态被卸载。
            setTemplates((prev) => {
                const serverItems = (data.templates || []).map(normalizeItem);
                // 请求期间用户可能还在改正文或删卡：只采用未被改动条目的服务端结果。
                const base = prev === snapshot
                    ? submitted
                    : mergeSavedTemplates(prev, submitted, serverItems);
                const next = adoptUiKeys(base, serverItems);
                if (!pending) return next;
                // 草稿刚进表。若 base 走的是 prev 分支，prev 里没有这张卡的 id，
                // adoptUiKeys 接不回它的键，正在编辑的模态会当场消失。
                // 这里按 id 手工把待保存时用的键接回来。
                return next.map((item) => (
                    item.id === pending.id ? { ...item, __ui_key: pending.__ui_key } : item
                ));
            });
            // 草稿已经进表，草稿槽随之清空（editingKey 继续指向同一张卡的新条目）。
            if (pending && draftRef.current === pending) setDraftNow(null);
            // 期间用户又改过其它卡时保留脏标记，否则那些改动会被当成已保存。
            if (templatesRef.current === snapshot) setDirty(false);
            setSavedAt(Date.now());
            if (window.Toast) {
                const warnings = data.warnings || [];
                if (warnings.length) {
                    window.Toast.warning(warnings[0], {
                        title: `已保存，但有 ${warnings.length} 条提示`,
                        description: warnings.slice(1, 3).join('\n'),
                    });
                } else if (pending) {
                    window.Toast.success(`「${pending.name || '未命名'}」已加入卡盒`, {
                        title: '新建成功',
                    });
                } else {
                    window.Toast.success('提示词已保存', { title: '保存成功' });
                }
            }
            return true;
        } catch (e) {
            setError(e.message || '保存失败');
            return false;
        } finally {
            setSaving(false);
        }
    };

    const saveRef = useRef(save);
    useEffect(() => { saveRef.current = save; });

    const simulate = async (sessionType) => {
        if (!sessionType) return;
        setSimulating(true);
        try {
            const data = await api.simulatePrompts({
                session_type: sessionType,
                rounds: 2000,
                // 有未保存改动或有草稿时都要带上草稿数据，否则预演结果与
                // 左侧概率面板不是同一份数据。
                templates: (dirtyRef.current || draftRef.current)
                    ? stripUiKey(submittedTemplates())
                    : undefined,
            });
            setSimulateResult(data);
        } catch (e) {
            setError(e.message || '预演失败');
        } finally {
            setSimulating(false);
        }
    };

    const previewSeqRef = useRef(0);

    const runPreview = async (item) => {
        if (!item) return;
        previewSeqRef.current += 1;
        const seq = previewSeqRef.current;
        setPreviewing(true);
        try {
            const data = await api.previewPrompt({
                session_type: item.scope === 'group' ? 'group' : 'friend',
                source_mode: 'conversation_history',
                unanswered_count: 0,
                // 锁定这张卡：模态里的预览永远就是这张卡，不再随机抽。
                template_id: item.id,
                // id 可能重复或为空，正文一起带上才能唯一定位到这张卡。
                template_text: item.text || '',
                // 草稿必须一起传，否则后端按 id 找不到这张还没进表的新卡，
                // 预览会直接返回空。
                templates: (dirtyRef.current || draftRef.current)
                    ? stripUiKey(submittedTemplates())
                    : undefined,
            });
            if (seq !== previewSeqRef.current) return;
            setPreview(data);
        } catch (e) {
            if (seq !== previewSeqRef.current) return;
            setError(e.message || '预览失败');
        } finally {
            if (seq === previewSeqRef.current) setPreviewing(false);
        }
    };

    const exportPayload = async () => api.exportPrompts();

    const doExport = async () => {
        setMenuAnchor(null);
        try {
            const data = await exportPayload();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `proactive-prompts-${new Date().toISOString().slice(0, 10)}.json`;
            link.click();
            URL.revokeObjectURL(url);
            if (window.Toast) window.Toast.success('模板已导出为 JSON 文件');
        } catch (e) {
            setError(e.message || '导出失败');
        }
    };

    const doCopy = async () => {
        setMenuAnchor(null);
        try {
            const data = await exportPayload();
            const text = JSON.stringify(data, null, 2);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                const area = document.createElement('textarea');
                area.value = text;
                area.style.position = 'fixed';
                area.style.opacity = '0';
                document.body.appendChild(area);
                area.select();
                document.execCommand('copy');
                document.body.removeChild(area);
            }
            if (window.Toast) window.Toast.success('模板 JSON 已复制到剪贴板');
        } catch (e) {
            setError(e.message || '复制失败');
        }
    };

    const doImport = async () => {
        let parsed;
        try {
            parsed = JSON.parse(importText);
        } catch (e) {
            setError('导入内容不是合法的 JSON');
            return;
        }
        loadSeqRef.current += 1;
        try {
            const data = await api.importPrompts(parsed);
            setTemplates((prev) => adoptUiKeys(prev, (data.templates || []).map(normalizeItem)));
            setDirty(false);
            setSavedAt(Date.now());
            setImportOpen(false);
            setImportText('');
            if (window.Toast) window.Toast.success(`已导入 ${data.imported} 条模板`);
        } catch (e) {
            setError(e.message || '导入失败');
        }
    };

    const doDiscard = async () => {
        setMenuAnchor(null);
        if (!window.confirm('丢弃所有未保存的改动，恢复到上次保存的版本？')) return;
        await load(false);
    };

    const doReset = async () => {
        setMenuAnchor(null);
        if (!window.confirm('将把所有模板恢复为出厂预设，当前改动会被覆盖。确定继续？')) {
            return;
        }
        loadSeqRef.current += 1;
        try {
            const data = await api.resetPrompts();
            setTemplates((prev) => adoptUiKeys(prev, (data.templates || []).map(normalizeItem)));
            setDirty(false);
            setSavedAt(Date.now());
            if (window.Toast) window.Toast.success(`已恢复 ${data.restored} 条出厂模板`);
        } catch (e) {
            setError(e.message || '恢复失败');
        }
    };

    // Cmd/Ctrl + S 保存。
    useEffect(() => {
        const onKeyDown = (event) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
                event.preventDefault();
                if (dirtyRef.current) saveRef.current();
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, []);

    // Esc 逐层退回：放大态 -> 卡池透视 -> 卡盒。
    const closeModalRef = useRef(closeModal);
    useEffect(() => { closeModalRef.current = closeModal; });

    useEffect(() => {
        const onKeyDown = (event) => {
            if (event.key !== 'Escape') return;
            if (editingKeyRef.current) {
                closeModalRef.current();
                return;
            }
            if (poolOpen) {
                setPoolOpen(false);
                return;
            }
            if (openDeck && !closing) {
                pendingDeckRef.current = null;
                setClosing(true);
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [openDeck, closing, poolOpen]);

    // 有未保存改动或未提交的草稿时拦截关页，避免误丢。
    useEffect(() => {
        const onBeforeUnload = (event) => {
            if (!dirtyRef.current && !draftRef.current) return undefined;
            event.preventDefault();
            event.returnValue = '';
            return '';
        };
        window.addEventListener('beforeunload', onBeforeUnload);
        return () => window.removeEventListener('beforeunload', onBeforeUnload);
    }, []);

    if (loading) {
        return (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
                <CircularProgress size={28} />
            </Box>
        );
    }

    /**
     * 盒内剩余张数。
     *
     * 摊开中的盒子按「总数 - 当前可见张数」算：被搜索隐藏的卡没有飞出来，
     * 仍然算在盒里，所以直接相减即可。
     *
     * 退场期间保持 0：卡片还在一张张缩回盒子，此时让迷你卡浮出来会和
     * 退场动画打架。等 onClosed 把 openDeck 置空，迷你卡才依次浮出。
     */
    const remainingOfDeck = (key) => {
        const target = decks[key];
        if (!target) return 0;
        if (openDeck !== key) return target.total;
        // 退场期间卡牌还在一张张收，迷你卡先保持空着；
        // onClosed 之后 openDeck 置空，迷你卡才按序弹出。
        if (closing) return 0;
        return Math.max(0, target.total - visibleCount);
    };

    // 抽屉里点标签换池：点当前池不关盒，点别的池才切。
    const switchPool = (key) => {
        if (key === openDeck) return;
        setSimulateResult(null);
        handleDeckClick(key);
    };

    /**
     * 权重凑整是否还有意义。
     * 先用凑整算法试算一遍：结果与当前权重逐一相同就说明已经凑整过，
     * 按钮保持不可操作，避免用户点了没有任何反馈。
     */
    const canNormalize = (() => {
        if (!deck || !deck.poolKeys.length) return false;
        const keySet = new Set(deck.poolKeys);
        const pool = templates.filter((item) => (
            keySet.has(item.__ui_key) && !invalidReason(item)
        ));
        if (!pool.length) return false;
        const nextWeights = normalizeToHundred(pool);
        return pool.some((item) => nextWeights[item.__ui_key] !== item.weight);
    })();

    return (
        <Box className="pv-page">
            {/* 页面底景：整页一层极淡的台面渐变，墙面与卡盒都挂在这一层之上。 */}
            <Box className="pv-backdrop" aria-hidden="true" />

            <Box className="pv-hero">
                <Box className="pv-hero-main">
                    <Box className="pv-hero-text">
                        <Typography component="h2" variant="h6" className="pv-hero-title">
                            💬 提示词管理
                        </Typography>
                        <Box className="pv-hero-pills">
                            <span className="pv-pill is-accent" title={PILL_HINTS.total}>
                                共 <strong>{templates.length}</strong> 套模板
                            </span>
                            <span className="pv-pill" title={PILL_HINTS.enabled}>
                                启用 <strong>{enabledCount}</strong>
                            </span>
                            <span className="pv-pill" title={PILL_HINTS.usable}>
                                可抽 <strong>{usableCount}</strong>
                            </span>
                        </Box>
                    </Box>

                    {/* 右侧一整条：动作组 + 常驻控件同处一行，
                        两者之间用一道极淡竖线分隔，不再是上下两排按钮。 */}
                    <Box className="pv-hero-bar">
                        {/* 动作组：只放真正改变数据的操作。 */}
                        <Box className="pv-hero-tools">
                            <Button
                                size="small"
                                variant="outlined"
                                className="pv-hero-btn is-primary"
                                onClick={handleAdd}
                            >
                                <span className="pv-hero-btn-icon" aria-hidden="true">＋</span>
                                新建卡牌
                            </Button>

                            <Tooltip title={canNormalize
                                ? '把这个卡盒里参与抽签的卡按原比例缩放，让权重合计正好 100'
                                : deck
                                    ? '权重已经凑整，无需调整'
                                    : '先点开一个卡盒，才能对着这一池凑整'}>
                                <span>
                                    <Button
                                        size="small"
                                        variant="outlined"
                                        className="pv-hero-btn"
                                        onClick={normalizeDeckWeights}
                                        disabled={!canNormalize}
                                    >
                                        <span className="pv-hero-btn-icon" aria-hidden="true">⇅</span>
                                        权重凑整
                                    </Button>
                                </span>
                            </Tooltip>

                            <Button
                                size="small"
                                variant="outlined"
                                className={`pv-hero-btn${poolOpen ? ' is-on' : ''}`}
                                onClick={() => setPoolOpen((prev) => !prev)}
                            >
                                <span className="pv-hero-btn-icon" aria-hidden="true">◎</span>
                                抽样透视
                            </Button>
                        </Box>

                        {/* 常驻控件：搜索、数据工具、保存状态。三者同高同圆角。 */}
                        <Box className="pv-hero-actions">
                            {/* 搜索常驻占位：展开只改宽度，不插入新节点，整行不会跳。 */}
                            <Box className={`pv-search${searchOpen ? ' is-open' : ''}`}>
                                <button
                                    type="button"
                                    className="pv-search-btn"
                                    onClick={() => setSearchOpen(true)}
                                    aria-label="搜索卡牌"
                                >
                                    <i aria-hidden="true" />
                                </button>
                                <input
                                    className="pv-search-input"
                                    type="text"
                                    value={keyword}
                                    placeholder="搜索名称或正文"
                                    tabIndex={searchOpen ? 0 : -1}
                                    onChange={(event) => setKeyword(event.target.value)}
                                    onBlur={() => {
                                        if (!keyword) setSearchOpen(false);
                                    }}
                                    onKeyDown={(event) => {
                                        if (event.key === 'Escape') {
                                            setKeyword('');
                                            setSearchOpen(false);
                                        }
                                    }}
                                />
                                {keyword && (
                                    <button
                                        type="button"
                                        className="pv-search-clear"
                                        onClick={() => { setKeyword(''); setSearchOpen(false); }}
                                        aria-label="清空搜索"
                                    >
                                        ✕
                                    </button>
                                )}
                            </Box>

                            <Tooltip title="导入、导出、恢复出厂等数据操作">
                                <IconButton
                                    size="small"
                                    className="pv-hero-icon-btn"
                                    onClick={(event) => setMenuAnchor(event.currentTarget)}
                                    aria-label="数据与工具"
                                >
                                    <span>⋯</span>
                                </IconButton>
                            </Tooltip>

                            {/* 保存区：洁净态只有一枚对勾，脏了才长出按钮。 */}
                            <Box className={`pv-save${dirty ? ' is-dirty' : ''}${saving ? ' is-saving' : ''}`}>
                                {dirty && !saving && (
                                    <Button
                                        size="small"
                                        variant="contained"
                                        className="pv-save-btn"
                                        onClick={save}
                                    >
                                        保存
                                    </Button>
                                )}
                                <span
                                    className="pv-save-mark"
                                    title={saving
                                        ? '正在写入配置'
                                        : dirty
                                            ? '有改动尚未保存'
                                            : `已保存${savedAt ? `于 ${timeLabel(savedAt)}` : ''}`}
                                    aria-label={dirty ? '有改动尚未保存' : '已保存'}
                                >
                                    {saving ? '···' : dirty ? '!' : '✓'}
                                </span>
                            </Box>
                        </Box>
                    </Box>
                </Box>
            </Box>

            {/* 新建时的作用域选择（仅在未开盒时出现） */}
            <Menu
                anchorEl={newMenuAnchor}
                open={Boolean(newMenuAnchor)}
                onClose={() => setNewMenuAnchor(null)}
            >
                {DECK_ORDER.map((key) => (
                    <MenuItem
                        key={key}
                        onClick={() => {
                            setNewMenuAnchor(null);
                            createCard(decks[key].scope);
                        }}
                    >
                        新建{scopeLabel(decks[key].scope)}卡牌
                    </MenuItem>
                ))}
            </Menu>

            <Menu
                anchorEl={menuAnchor}
                open={Boolean(menuAnchor)}
                onClose={() => setMenuAnchor(null)}
            >
                <MenuItem onClick={() => { setMenuAnchor(null); setImportOpen(true); }}>
                    <Box className="pv-menu-item">
                        导入 JSON…
                        <small>粘贴或整表替换现有模板</small>
                    </Box>
                </MenuItem>
                <MenuItem onClick={doExport}>
                    <Box className="pv-menu-item">
                        导出 JSON
                        <small>下载当前全部模板</small>
                    </Box>
                </MenuItem>
                <MenuItem onClick={doCopy}>
                    <Box className="pv-menu-item">
                        复制为 JSON
                        <small>直接进剪贴板，方便贴给 AI</small>
                    </Box>
                </MenuItem>
                <Divider />
                {dirty && (
                    <MenuItem onClick={doDiscard}>
                        <Box className="pv-menu-item">
                            丢弃未保存改动
                            <small>恢复到上次保存的版本</small>
                        </Box>
                    </MenuItem>
                )}
                <MenuItem onClick={doReset} sx={{ color: 'error.main' }}>
                    <Box className="pv-menu-item">
                        恢复出厂模板
                        <small>覆盖所有模板，不可撤销</small>
                    </Box>
                </MenuItem>
            </Menu>

            {error && (
                <Alert severity="error" onClose={() => setError('')}>{error}</Alert>
            )}

            {/* 舞台：洞洞板墙面 + 货架 + 卡牌区共用一块背景。
                墙面是常驻的，收起态就在，展开后卡片落在同一面墙上。
                动画时长由这里注入，卡片与迷你卡读同一组变量。 */}
            <Box
                className="pv-stage"
                ref={stageRef}
                style={{
                    '--pv-launch-dur': `${CARD_LAUNCH_DURATION}ms`,
                    '--pv-launch-step': `${CARD_LAUNCH_STEP}ms`,
                    '--pv-retract-dur': `${CARD_RETRACT_DURATION}ms`,
                    '--pv-retract-step': `${CARD_RETRACT_STEP}ms`,
                    '--pv-retract-lead': `${CARD_RETRACT_LEAD}ms`,
                    // 洞洞板要提前铺到「打开卡盒后」的长度，否则开盒瞬间墙面才被撑长，
                    // 背景看起来是后来延伸出来的。高度由 measureStageBudget 预先算好。
                    minHeight: stageMinHeight || undefined,
                }}
            >
                <Box className="pv-wall" aria-hidden="true" />

                {/* ===== 挂钩层：舞台常驻，与 openDeck 无关 =====
                    挂钩是墙上的固定件，收起态就该在墙上。之前画在 DeckSection 里，
                    而 DeckSection 只有开盒时才挂载 —— 于是挂钩跟着卡片一起出现消失，
                    收起态一个都看不到。现在改由舞台渲染，位置由 hookTop 注入。

                    分两层渲染（这是本轮的关键）：
                      .pv-hook-layer.is-under   卡片之前，z:0 -> 杆身被卡片盖住
                      .pv-hook-layer.is-over    卡片之后，z:3 -> 只有钩尖露在卡面上
                    卡片用 mask 在孔位置打了真孔（透明半径 5px），
                    所以「杆穿进孔」这件事由 under 层的杆 + 卡片的孔共同表达，
                    而钩尖压在卡面上，读作「钩子从孔里穿出来、翘在卡上」。
                    两层格子几何完全一致（同列定义、同 gap、同行高），
                    所以钩尖必然正对杆身中轴，不会错位。 */}
                {Array.from({ length: STAGE_MAX_ROWS }).map((unused, row) => (
                    <Box
                        key={`hook-under-${row}`}
                        className="pv-hook-layer is-under"
                        style={{ top: hookTop + row * (CARD_MIN_HEIGHT + CARD_SLOT_TOP_PAD + CARD_GRID_GAP) }}
                        aria-hidden="true"
                    >
                        <HookRow />
                    </Box>
                ))}
                {Array.from({ length: STAGE_MAX_ROWS }).map((unused, row) => (
                    <Box
                        key={`hook-over-${row}`}
                        className="pv-hook-layer is-over"
                        style={{ top: hookTop + row * (CARD_MIN_HEIGHT + CARD_SLOT_TOP_PAD + CARD_GRID_GAP) }}
                        aria-hidden="true"
                    >
                        <HookRow variant="tip" />
                    </Box>
                ))}

                <Box className="pv-shelf" ref={shelfRef}>
                    {/* 悬吊式层板：左右两条细链把木架吊在墙上，木架本身不落地。
                        链子与端头锚环都是 .pv-shelf 的直接子元素（与卡盒无关），
                        因为吊点是架子的固定件，不该跟着卡盒走。
                        链身 z:0 在台面/卡盒之后；锚环 z:2 画在台面之上，
                        扣在台面端头，把链底「接住」—— 两端连接点都可见。 */}
                    <span className="pv-shelf-chain is-left" aria-hidden="true">
                        <span className="pv-shelf-chain-body" />
                    </span>
                    <span className="pv-shelf-chain is-right" aria-hidden="true">
                        <span className="pv-shelf-chain-body" />
                    </span>
                    <span className="pv-shelf-anchor is-left" aria-hidden="true" />
                    <span className="pv-shelf-anchor is-right" aria-hidden="true" />

                    {DECK_ORDER.map((key) => (
                        <Box className="pv-shelf-cell" key={key}>
                            <DeckBox
                                deck={decks[key]}
                                open={openDeck === key}
                                closing={closing && openDeck === key}
                                remaining={remainingOfDeck(key)}
                                mouthRef={(el) => { mouthRefs.current[key] = el; }}
                                onClick={() => handleDeckClick(key)}
                            />
                        </Box>
                    ))}
                </Box>

                {deck && (
                    <DeckSection
                        key={deck.key}
                        deck={deck}
                        closing={closing}
                        anchor={mouthAnchor}
                        keyword={keyword}
                        instant={deckInstant}
                        onClosed={handleClosed}
                        onAdd={() => createCard(deck.scope)}
                        onOpenCard={openCard}
                    />
                )}
            </Box>

            {/* 抽屉与放大态都 Portal 到 body：只要留在 .pv-page 内，
                祖先的 transform / filter 就会让 fixed 相对它定位，遮罩盖不满视口。 */}
            {createPortal(
                <PoolDrawer
                    open={poolOpen}
                    deckKey={openDeck}
                    decks={decks}
                    dirty={dirty}
                    simulateResult={simulateResult
                        && simulateResult.session_type === openDeck
                        ? simulateResult
                        : null}
                    simulating={simulating}
                    onSimulate={simulate}
                    onSwitch={switchPool}
                    onClose={() => setPoolOpen(false)}
                />,
                document.body,
            )}

            {/* 放大态：从被点击的那张卡长出来 */}
            {createPortal(
                <PromptDrawer
                    item={editingItem}
                    // 新建语境：这张卡还没进表，主按钮文案与删除语义都要跟着变。
                    isDraft={Boolean(draft && editingItem && draft.__ui_key === editingItem.__ui_key)}
                    index={editingItem ? indexOf(editingItem) : 0}
                    probability={editingItem ? probabilityOf(editingItem) : 0}
                    share={editingItem ? shareOf(editingItem) : 0}
                    preview={preview}
                    previewing={previewing}
                    saving={saving}
                    closing={modalClosing}
                    origin={modalOrigin}
                    onPatch={(patch) => editingItem && patchByKey(editingItem.__ui_key, patch)}
                    onPreview={runPreview}
                    onClose={closeModal}
                    onRemove={() => {
                        // 删除后条目已不存在，不必再等退场动画，直接卸载。
                        if (editingItem && removeByKey(editingItem.__ui_key)) {
                            window.clearTimeout(modalTimerRef.current);
                            setModalClosing(false);
                        }
                    }}
                    onSave={save}
                />,
                document.body,
            )}

            <PromptImportDialog
                open={importOpen}
                text={importText}
                onChange={setImportText}
                onClose={() => setImportOpen(false)}
                onConfirm={doImport}
            />
        </Box>
    );
}

/**
 * 导入对话框：粘贴整表 JSON。
 * 单独拆出来只是因为 JSX 体积，没有其他状态。
 */
function PromptImportDialog({ open, text, onChange, onClose, onConfirm }) {
    const { Dialog, DialogTitle, DialogContent, DialogActions } = MaterialUI;
    return (
        <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
            <DialogTitle>导入模板</DialogTitle>
            <DialogContent>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                    粘贴导出的 JSON，导入会整表替换现有模板。
                </Typography>
                <TextField
                    fullWidth
                    multiline
                    minRows={10}
                    maxRows={20}
                    value={text}
                    onChange={(event) => onChange(event.target.value)}
                    placeholder='{"format": "astrbot-proactive-chat/prompts", "templates": [...]}'
                    InputProps={{ className: 'mono', sx: { fontSize: '0.78rem' } }}
                />
            </DialogContent>
            <DialogActions>
                <Button onClick={onClose}>取消</Button>
                <Button variant="contained" onClick={onConfirm} disabled={!text.trim()}>
                    导入
                </Button>
            </DialogActions>
        </Dialog>
    );
}

window.PromptsView = PromptsView;
})();
