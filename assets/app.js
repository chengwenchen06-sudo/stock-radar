/* Stock Radar 前端逻辑
 * 设计目标：承载 ~700 条真实数据而不卡顿。
 *  - requestAnimationFrame 分块渲染大列表
 *  - 「全部」分页（每页 50 + 加载更多）
 *  - watchlist 可编辑（localStorage 持久化）
 */
const DATA_BASE = "data";
const PAGE_SIZE = 50;
const RENDER_CHUNK = 30;
const WATCHLIST_KEY = "stock-radar:watchlist:v1";
const REFRESH_API = "http://127.0.0.1:8766";  // refresh_server.py 端口
let _refreshAvailable = false;

// 内置默认关注股（用户清空 localStorage 或首次访问时使用）
const DEFAULT_WATCHLIST = [
  // A 股
  { code: "600519", name: "贵州茅台" },
  { code: "300750", name: "宁德时代" },
  { code: "002594", name: "比亚迪" },
  { code: "601318", name: "中国平安" },
  { code: "688981", name: "中芯国际" },
  // 港股
  { code: "00700", name: "腾讯控股" },
  { code: "09988", name: "阿里巴巴" },
  // 美股
  { code: "NVDA", name: "英伟达" },
  { code: "AAPL", name: "苹果" },
  { code: "TSLA", name: "特斯拉" },
];

const state = {
  view: "signal",
  market: "all",
  label: "all",
  importance: "all",
  stock: "",
  search: "",
  data: null,
  stories: null,
  status: null,
  daily: null,
  quotes: null,          // { code: {price, change_pct, market} }
  allPage: 0,
  allRendered: [],
  watchlist: [],
};

const el = (sel) => document.querySelector(sel);
const els = (sel) => Array.from(document.querySelectorAll(sel));

async function fetchJSON(path) {
  const r = await fetch(`${DATA_BASE}/${path}`);
  if (!r.ok) throw new Error(`fetch ${path} failed: ${r.status}`);
  return r.json();
}

function relTime(iso) {
  const t = new Date(iso);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  return `${Math.floor(diff / 86400)}天前`;
}

function fmtPrice(p) {
  if (p == null) return "—";
  return Number(p).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtPct(p) {
  if (p == null) return "—";
  const sign = p >= 0 ? "+" : "";
  return `${sign}${Number(p).toFixed(2)}%`;
}

async function loadMarketOverview() {
  const section = el("#market-overview");
  if (!section) return;
  let data;
  try {
    data = await fetchJSON("market-overview.json");
  } catch (_) {
    section.hidden = true;
    return;
  }
  if (!data || data.status === "failed") {
    section.hidden = true;
    return;
  }
  renderMarketOverview(data);
  section.hidden = false;
}

function renderMarketOverview(data) {
  el("#mo-time").textContent = data.generated_at
    ? new Date(data.generated_at).toLocaleString("zh-CN", { hour12: false })
    : "";

  const idxEl = el("#mo-indices");
  idxEl.innerHTML = "";
  for (const idx of data.indices || []) {
    const dir = (idx.change_pct ?? 0) >= 0 ? "up" : "down";
    const arrow = dir === "up" ? "▲" : "▼";
    idxEl.insertAdjacentHTML("beforeend", `
      <div class="mo-index ${dir}">
        <div class="mo-index-name">${escapeHTML(idx.name)}</div>
        <div class="mo-index-price">${fmtPrice(idx.price)}</div>
        <div class="mo-index-pct">${arrow} ${fmtPct(idx.change_pct)}</div>
      </div>
    `);
  }

  const b = data.breadth || {};
  const breadthEl = el("#mo-breadth");
  if (b.total) {
    breadthEl.innerHTML = `
      <span class="mo-stat up">📈 涨 <strong>${b.up ?? 0}</strong></span>
      <span class="mo-stat down">📉 跌 <strong>${b.down ?? 0}</strong></span>
      <span class="mo-stat">平 <strong>${b.flat ?? 0}</strong></span>
      <span class="mo-stat up">🚀 涨停 <strong>${b.limit_up ?? 0}</strong></span>
      <span class="mo-stat down">💥 跌停 <strong>${b.limit_down ?? 0}</strong></span>
    `;
  } else {
    breadthEl.innerHTML = `<span class="mo-empty">数据暂不可用</span>`;
  }

  const sectorsEl = el("#mo-sectors");
  const top = data.sectors_top || [];
  const bot = data.sectors_bottom || [];
  if (top.length || bot.length) {
    const item = (s) =>
      `<span class="mo-sector-item ${s.change_pct >= 0 ? "up" : "down"}">${escapeHTML(s.name)} <strong>${fmtPct(s.change_pct)}</strong></span>`;
    sectorsEl.innerHTML = `
      <div>
        <div class="mo-sector-list-title">🔥 领涨</div>
        ${top.length ? top.map(item).join("") : `<span class="mo-empty">暂无</span>`}
      </div>
      <div>
        <div class="mo-sector-list-title">💧 领跌</div>
        ${bot.length ? bot.map(item).join("") : `<span class="mo-empty">暂无</span>`}
      </div>
    `;
  } else {
    sectorsEl.innerHTML = `<span class="mo-empty">板块数据暂不可用</span>`;
  }
}

function renderStockCard(q) {
  const card = el("#stock-info-card");
  if (!card) return;
  if (!/^[0-9]{5,6}$|^[A-Za-z]{1,5}(\.[A-Za-z])?$/.test(q.trim())) {
    card.hidden = true;
    return;
  }
  const code = q.trim().toUpperCase();
  const quote = (state.quotes || {})[code];
  const name = (quote && quote.name) || lookupStockName(code) || "";
  // Bug fix: 之前这里会把没匹配到名称/也没行情的代码卡藏起来,
  // 导致用户搜冷门代码(300144、002415 等)时卡"消失"。
  // 搜股票代码 = 用户意图明确,卡必须显示(哪怕只是"未识别股票 [code] + 加入关注")。
  const displayName = (quote && quote.name) || name || code;
  const inWl = state.watchlist.some((w) => w.code === code);

  let quoteHtml = "";
  if (quote && quote.price != null) {
    const dir = (quote.change_pct ?? 0) > 0 ? "up" : (quote.change_pct < 0 ? "down" : "flat");
    const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "—";
    quoteHtml = `
      <div class="sc-quote ${dir}">
        <span class="sc-price">${fmtPrice(quote.price)}</span>
        <span class="sc-pct">${arrow} ${fmtPct(quote.change_pct)}</span>
      </div>
    `;
  }

  const actionHtml = inWl
    ? `<span class="sc-in-wl">✓ 已在关注列表</span>`
    : `<button class="primary-btn sc-add" data-code="${escapeHTML(code)}" data-name="${escapeHTML(displayName)}">+ 加入「我的关注」</button>`;

  const hasQuote = !!(quote && quote.price != null);
  card.innerHTML = `
    <div class="sc-info">
      <span class="sc-name">📊 ${escapeHTML(displayName)}</span>
      <span class="sc-code">${escapeHTML(code)}</span>
      ${quoteHtml}
    </div>
    <div class="sc-actions">${actionHtml}</div>
    <span class="sc-error"></span>
    ${hasQuote ? "" : `<span class="sc-hint">${escapeHTML(displayName)} 最近没有 48h 内的公告、新闻或异动信号。加入关注后,有相关信号时会立即显示。</span>`}
  `;

  const addBtn = card.querySelector(".sc-add");
  if (addBtn) {
    addBtn.addEventListener("click", () => {
      const r = addStock(addBtn.dataset.code, addBtn.dataset.name);
      const errEl = card.querySelector(".sc-error");
      if (errEl) errEl.textContent = r.ok ? "" : (r.error || "加入失败");
      renderStockCard(state.search);
    });
  }
  card.hidden = false;
}

function tierLabel(t) {
  return ["官方一手", "主流财经", "二线财经", "RSS/OPML", "", "社交聚合"][t] || `tier${t}`;
}

function marketTag(m) {
  return { cn: "A股", hk: "港股", us: "美股", global: "全球" }[m] || m;
}

function escapeHTML(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- 共享过滤逻辑 ---------- */

function itemSearchBlob(it) {
  return [
    it.title || "",
    it.summary || "",
    it.source || "",
    (it.raw && it.raw.code) || "",
    (it.raw && it.raw.sec_code) || "",
    (it.raw && it.raw.stock_code) || "",
    (it.raw && it.raw.short_name) || "",
    (it.raw && it.raw.sec_name) || "",
    (it.raw && it.raw.name) || "",
    (it.raw && it.raw.industry) || "",
    (it.raw && it.raw.sector) || "",
  ].join(" ").toLowerCase();
}

function itemMatchesSearch(it, rawQuery) {
  if (!rawQuery || !rawQuery.trim()) return true;
  const q = rawQuery.toLowerCase().trim();
  const blob = itemSearchBlob(it);
  const rawTokens = q.split(/\s+/).filter(Boolean);
  return rawTokens.every((origTok) => {
    const cands = [origTok];
    if (/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(origTok.toUpperCase())) {
      const aliases = lookupAliases(origTok.toUpperCase());
      for (const a of aliases) cands.push(a.toLowerCase());
    }
    return cands.some((c) => blob.includes(c));
  });
}

function itemMatchesFilters(it) {
  if (state.market !== "all" && it.market !== state.market) return false;
  if (state.label !== "all" && it.label !== state.label) return false;
  if (state.importance !== "all" && it.importance_label !== state.importance) return false;
  if (state.stock) {
    const code = extractStockCode(it.title);
    const rawCode = (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
    if (code !== state.stock && rawCode !== state.stock) return false;
  }
  return true;
}

function filterItems(items) {
  return items.filter((it) => {
    if (!itemMatchesFilters(it)) return false;
    if (!itemMatchesSearch(it, state.search)) return false;
    return true;
  });
}

/* ---------- 行情显示 ---------- */

function quoteTag(code) {
  // 没行情数据时显示占位 — 让卡片头部不会因为缺数据而"消失"
  if (!state.quotes) return `<span class="tag price empty" title="行情数据未加载(file:// 下 localStorage/origin 可能受限,请用 HTTP server)">—</span>`;
  const q = state.quotes[code];
  if (!q || q.price == null) return `<span class="tag price empty" title="该股票暂无实时行情报价">—</span>`;
  const price = q.market === "hk" ? `HK$${q.price.toFixed(3)}`
               : q.market === "us" ? `$${q.price.toFixed(2)}`
               : `¥${q.price.toFixed(2)}`;
  const pct = q.change_pct;
  if (pct == null) return `<span class="tag price">${escapeHTML(price)}</span>`;
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
  const sign = pct > 0 ? "+" : "";
  return `<span class="tag price ${cls}">${escapeHTML(price)} ${sign}${pct.toFixed(2)}%</span>`;
}

/* ---------- Watchlist 持久化 ---------- */

function loadWatchlist() {
  try {
    const raw = localStorage.getItem(WATCHLIST_KEY);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length > 0 && arr.every((x) => x.code)) {
        return arr;
      }
    }
  } catch (_) { /* fall through */ }
  return DEFAULT_WATCHLIST.slice();
}

function saveWatchlist(arr) {
  try {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(arr));
  } catch (_) { /* quota or private mode, just skip */ }
}

function addStock(code, name) {
  code = (code || "").trim();
  name = (name || "").trim();
  if (!code) return { ok: false, error: "代码不能为空" };
  // 跟前端 renderStockCard 的代码识别正则保持一致:5-6 位数字 + 1-5 字母 + 可选 .X
  if (!/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(code)) {
    return { ok: false, error: "代码格式:5-6 位数字(A 股 / 港股)或 1-5 位字母(美股)" };
  }
  if (state.watchlist.some((w) => w.code === code)) {
    return { ok: false, error: "已在关注列表中" };
  }
  state.watchlist.push({ code, name: name || lookupStockName(code) || "" });
  saveWatchlist(state.watchlist);
  rebuildWatchlistChips();
  rerender();
  fetchQuoteFor(code);
  return { ok: true };
}

// 加股后顺手通过本地 /api/quote 拉一次实时行情,不用等下一个全量刷新周期。
// fetch-and-forget,失败就静默 —— quoteTag 会继续显示 "—",用户重试或下个周期再补。
async function fetchQuoteFor(code) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), 9000);
  try {
    const r = await fetch(`/api/quote?code=${encodeURIComponent(code)}`, { signal: ctrl.signal });
    if (!r.ok) return;
    const data = await r.json();
    if (!data || data.code !== code) return;
    const q = data.quote || {};
    if (data.has_quote && q.price != null) {
      state.quotes = state.quotes || {};
      state.quotes[code] = q;
      rerender();
    }
  } catch (_) { /* 网络/超时/服务器不可达 —— "—" 占位就让它占着 */ }
  finally { clearTimeout(tid); }
}

function removeStock(code) {
  state.watchlist = state.watchlist.filter((w) => w.code !== code);
  saveWatchlist(state.watchlist);
  if (state.stock === code) state.stock = "";
  rebuildWatchlistChips();
  rerender();
}

function resetWatchlist() {
  state.watchlist = DEFAULT_WATCHLIST.slice();
  saveWatchlist(state.watchlist);
  state.stock = "";
  rebuildWatchlistChips();
  rerender();
}

/* ---------- 匹配 ---------- */

function extractStockCode(title) {
  // A 股 / 港股 5-6 位数字 或 美股 1-5 位字母（+可选 .B）
  // 注意：右方括号 ] 在 JS regex 字符类中要放在第一位，否则被解析为结束符
  const m = (title || "").match(/[\[【\(]\s*([0-9]{5,6}|[A-Z]{1,5}(\.[A-Z])?)[\s】\]\)]/);
  return m ? m[1] : "";
}

function isInWatchlist(it) {
  const code = extractStockCode(it.title);
  if (code && state.watchlist.some((w) => w.code === code)) return true;
  const rawCode = (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
  if (rawCode && state.watchlist.some((w) => w.code === rawCode)) return true;
  return false;
}

/* ---------- HTML 渲染 ---------- */

function itemHTML(it) {
  const code = extractStockCode(it.title);
  const isWatched = code && state.watchlist.some((w) => w.code === code);
  const stockTag = code
    ? (isWatched
        ? `<span class="tag watchlist-hit">⭐ ${code}</span>`
        : `<span class="tag">${code}</span>`)
    : "";
  const qTag = code ? quoteTag(code) : "";
  return `
    <li class="item importance-${it.importance_label}">
      <div class="item-header">
        <h3 class="item-title"><a href="${escapeHTML(it.url)}" target="_blank" rel="noopener">${escapeHTML(it.title)}</a></h3>
      </div>
      <div class="item-meta">
        <span class="tag market-${it.market}">${marketTag(it.market)}</span>
        <span class="tag tier-${it.source_tier_rank}">${tierLabel(it.source_tier_rank)}</span>
        <span class="tag">${escapeHTML(it.label_zh || it.label)}</span>
        <span class="tag importance-${it.importance_label}">重要性 · ${it.importance_label} (${it.importance_score})</span>
        <span class="tag">${escapeHTML(it.source)}</span>
        ${stockTag}
        ${qTag}
        <span class="tag">${relTime(it.published_at)}</span>
      </div>
      ${it.summary ? `<div class="item-summary">${escapeHTML(it.summary)}</div>` : ""}
    </li>
  `;
}

function storyHTML(s) {
  const links = s.items.map((i) => `<a href="${escapeHTML(i.url)}" target="_blank" rel="noopener">${escapeHTML(i.source)}</a>`).join("");
  return `
    <li class="story">
      <div class="story-header">
        <h3><a href="${escapeHTML(s.primary_url)}" target="_blank" rel="noopener">${escapeHTML(s.title)}</a></h3>
        <span class="tag importance-${s.importance_label}">${s.importance_label} · ${s.importance_score}</span>
      </div>
      <div class="item-meta" style="margin-bottom:8px">
        <span class="tag">${escapeHTML(s.label_zh)}</span>
        <span class="tag">${s.source_count} 个信源</span>
        <span class="tag">${s.markets.map(marketTag).join(" / ")}</span>
      </div>
      <div class="story-sources">${links}</div>
    </li>
  `;
}

function applyFilters(items) {
  return filterItems(items);
}

function renderInChunks(container, items, buildHTML, chunkSize = RENDER_CHUNK) {
  container.innerHTML = "";
  let i = 0;
  function step() {
    if (i >= items.length) return;
    const slice = items.slice(i, i + chunkSize);
    container.insertAdjacentHTML("beforeend", slice.map(buildHTML).join(""));
    i += chunkSize;
    if (i < items.length) requestAnimationFrame(step);
  }
  step();
}

/* ---------- 视图 ---------- */

function renderSignal() {
  if (!state.data) return;
  // 有搜索 query 时显示所有匹配（不限 high importance）
  // 否则只显示 high importance（前 60 条）
  let items;
  if (state.search && state.search.trim()) {
    items = applyFilters(state.data.items).slice(0, 60);
  } else {
    items = state.data.items.filter((i) => i.importance_label === "high");
    items = applyFilters(items).slice(0, 60);
  }
  el("#signal-list").innerHTML = items.map(itemHTML).join("") ||
    `<li class="story empty-state">
      <p>${state.search ? '未搜索到匹配条目' : '暂无高重要性信号'}</p>
      <p class="empty-hint">${state.search
        ? '检查搜索关键词是否正确，或试试热门代码按钮。也可切换到「📰 全部」tab 查看所有条目。'
        : '切换市场/分类筛选，或尝试搜索感兴趣的关键词/代码。'}</p>
    </li>`;
}

function renderAll(reset = true) {
  if (!state.data) return;
  if (reset) {
    state.allPage = 0;
    state.allRendered = applyFilters(state.data.items);
    el("#all-list").innerHTML = "";
  }
  const start = state.allPage * PAGE_SIZE;
  const slice = state.allRendered.slice(start, start + PAGE_SIZE);
  el("#all-list").insertAdjacentHTML("beforeend", slice.map(itemHTML).join(""));
  state.allPage++;
  el("#all-count").textContent = `共 ${state.allRendered.length} 条 · 已显示 ${Math.min(state.allPage * PAGE_SIZE, state.allRendered.length)}`;
  const loadmore = el("#all-loadmore");
  if (state.allPage * PAGE_SIZE < state.allRendered.length) {
    loadmore.style.display = "block";
  } else {
    loadmore.style.display = "none";
  }
}

function updateSearchStatus() {
  const box = el("#search-status");
  if (!box) return;
  renderStockCard(state.search);
  const q = state.search.trim();
  if (!q) {
    box.className = "search-status";
    box.textContent = "";
    return;
  }
  if (!state.data) return;
  const matched = state.data.items.filter((it) => itemMatchesSearch(it, state.search));
  const bySrc = {};
  for (const it of matched) bySrc[it.source] = (bySrc[it.source] || 0) + 1;
  if (matched.length === 0) {
    box.className = "search-status empty";
    const isCode = /^[0-9]{5,6}$/.test(q) || /^[A-Za-z]{1,5}(\.[A-Za-z])?$/.test(q);
    const hint = isCode
      ? `48 小时数据窗口内「<span class="code-chip">${escapeHTML(q)}</span>」没有公告/新闻/异动,以上是股票最新信息。`
      : `未找到与「<span class="code-chip">${escapeHTML(q)}</span>」相关的条目。换个关键词试试,或使用股票代码(如 600519 / 00700 / NVDA)搜索。`;
    box.innerHTML = hint;
    return;
  }
  box.className = "search-status ok";
  const srcs = Object.entries(bySrc)
    .sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `${escapeHTML(s)} ${n}`)
    .join(" · ");
  box.innerHTML = `找到 <strong>${matched.length}</strong> 条匹配「<span class="code-chip">${escapeHTML(q)}</span>」 · ${srcs}`;
}

function renderWatchlist() {
  const groups = {};
  for (const w of state.watchlist) groups[w.code] = [];
  const filtered = filterItems((state.data && state.data.items) || []);
  for (const it of filtered) {
    const code = extractStockCode(it.title) ||
                 (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
    if (groups[code]) groups[code].push(it);
  }

  const cards = el("#watchlist-cards");
  cards.innerHTML = "";
  let totalHits = 0;
  let activeStocks = 0;
  for (const w of state.watchlist) {
    const items = groups[w.code];
    totalHits += items.length;
    if (items.length > 0) activeStocks++;
    items.sort((a, b) => (a.source_tier_rank - b.source_tier_rank) ||
                          (-b.importance_score - -a.importance_score));
    const topItems = items.slice(0, 5);
    const recentEvents = items.length
      ? topItems.map(itemHTML).join("")
      : `<li class="story empty"><p>暂无相关条目</p></li>`;
    const card = document.createElement("div");
    card.className = "watchlist-card";
    card.innerHTML = `
      <div class="watchlist-card-header">
        <h3>
          <span class="stock-code">${escapeHTML(w.code)}</span>
          <span class="stock-name">${escapeHTML(w.name || lookupStockName(w.code) || "—")}</span>
          ${quoteTag(w.code)}
          <button class="stock-remove" data-code="${escapeHTML(w.code)}" title="移除">×</button>
        </h3>
        <span class="badge ${items.length > 0 ? 'has' : 'empty'}">${items.length} 条</span>
      </div>
      <ul class="item-list compact">${recentEvents}</ul>
      ${items.length > 5 ? `<div class="watchlist-more">还有 ${items.length - 5} 条</div>` : ""}
    `;
    cards.appendChild(card);
  }

  // 绑定每张卡片的删除按钮
  cards.querySelectorAll(".stock-remove").forEach((btn) => {
    btn.addEventListener("click", () => removeStock(btn.dataset.code));
  });

  el("#watchlist-summary").textContent =
    `${state.watchlist.length} 只关注股 · ${activeStocks} 只有信号 · 共 ${totalHits} 条命中`;
}

function renderStories() {
  if (!state.stories) return;
  const stories = applyFilters(state.stories.stories.map((s) => ({
    ...s,
    market: (s.markets && s.markets[0]) || "global",
    published_at: s.items?.[0]?.published_at || new Date().toISOString(),
    summary: "",
  })));
  renderInChunks(el("#stories-list"), stories, storyHTML);
}

function renderDaily() {
  if (!state.daily) return;
  el("#daily-list").innerHTML = state.daily.items.map(itemHTML).join("") ||
    `<li class="story"><p>暂无日报条目。</p></li>`;
}

function renderStatus() {
  if (!state.status) return;
  const tbody = el("#status-table tbody");
  tbody.innerHTML = state.status.sites.map((s) => `
    <tr>
      <td>${escapeHTML(s.site_name)}</td>
      <td><span class="${s.ok ? "ok" : "fail"}">${s.ok ? "✓ OK" : "✗ 失败"}</span></td>
      <td>${s.item_count}</td>
      <td>${s.elapsed_seconds}s</td>
      <td>${escapeHTML(s.error || "")}</td>
    </tr>
  `).join("");
}

let _lastGeneratedAt = "";

function updateStaleWarning(elapsedSec) {
  let banner = el("#stale-banner");
  if (elapsedSec < 6 * 3600) {
    if (banner) banner.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "stale-banner";
    banner.style.cssText = "padding:10px 20px;font-size:13px;text-align:center;border-bottom:1px solid";
    const topbar = el(".topbar");
    if (topbar) topbar.after(banner);
    else document.body.prepend(banner);
  }
  if (elapsedSec < 24 * 3600) {
    banner.style.background = "rgba(210,153,34,0.12)";
    banner.style.color = "#d29922";
    banner.style.borderColor = "rgba(210,153,34,0.3)";
    banner.textContent = `⚠️ 数据已 ${Math.floor(elapsedSec / 3600)} 小时未更新，下次更新约在 ${nextCIRun()}`;
  } else {
    banner.style.background = "rgba(248,81,73,0.12)";
    banner.style.color = "#f85149";
    banner.style.borderColor = "rgba(248,81,73,0.3)";
    banner.textContent = `🚨 数据已超过 24 小时未更新（${Math.floor(elapsedSec / 3600)} 小时前），请检查 CI 状态`;
  }
}

function nextCIRun() {
  const now = new Date();
  const next = new Date(now);
  next.setUTCHours(22, 0, 0, 0);
  if (now > next) next.setUTCDate(next.getUTCDate() + 1);
  const diff = Math.round((next - now) / 3600000);
  return diff > 1 ? `${diff} 小时后` : "即将";
}

function updateMeta() {
  if (!state.data) return;
  const d = state.data;
  const ts = new Date(d.generated_at);
  const elapsed = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
  const timeStr = elapsed < 60 ? `${elapsed}秒前`
    : elapsed < 3600 ? `${Math.floor(elapsed / 60)}分钟前`
    : `${Math.floor(elapsed / 3600)}小时前`;
  el("#data-meta").textContent =
    `更新于 ${timeStr} · ${d.total_items} 条 · ${d.source_count} 个信源`;
  updateStaleWarning(elapsed);
}

/* ---------- 自动刷新 ---------- */

let autoRefreshInterval = null;
let metaTimer = null;

function startAutoRefresh() {
  metaTimer = setInterval(updateMeta, 1000);

  autoRefreshInterval = setInterval(async () => {
    try {
      const oldGen = _lastGeneratedAt;
      await loadAllData();
      if (_lastGeneratedAt && _lastGeneratedAt !== oldGen) {
        const elm = el("#data-meta");
        elm.style.transition = "color 0.3s";
        elm.style.color = "var(--accent)";
        setTimeout(() => { elm.style.color = ""; }, 800);
      }
    } catch (_) {
    }
  }, 600_000);
}

function stopAutoRefresh() {
  if (metaTimer) clearInterval(metaTimer);
  if (autoRefreshInterval) clearInterval(autoRefreshInterval);
}

function setView(v) {
  state.view = v;
  els(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  els(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  const renderers = {
    signal: renderSignal,
    watchlist: renderWatchlist,
    all: () => renderAll(true),
    stories: renderStories,
    daily: renderDaily,
    status: renderStatus,
  };
  renderers[v]?.();
}

/* ---------- watchlist chips ---------- */

function rebuildWatchlistChips() {
  const container = el("#watchlist-chips");
  container.innerHTML = "";
  const allBtn = document.createElement("button");
  allBtn.className = "chip" + (state.stock === "" ? " active" : "");
  allBtn.dataset.stock = "";
  allBtn.textContent = "全部";
  container.appendChild(allBtn);
  for (const w of state.watchlist) {
    const btn = document.createElement("button");
    btn.className = "chip" + (state.stock === w.code ? " active" : "");
    btn.dataset.stock = w.code;
    btn.textContent = `${w.code}${w.name ? " " + w.name : ""}`;
    container.appendChild(btn);
  }
  bindWatchlistChipEvents();
}

function bindWatchlistChipEvents() {
  els("#watchlist-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.stock = c.dataset.stock || "";
      els("#watchlist-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      rerender();
    });
  });
}

function bindChips() {
  rebuildWatchlistChips();

  els("#market-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.market = c.dataset.market;
      els("#market-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      rerender();
    });
  });
  els("#label-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.label = c.dataset.label;
      els("#label-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      rerender();
    });
  });
  els("#importance-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.importance = c.dataset.importance;
      els("#importance-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      rerender();
    });
  });
  el("#search").addEventListener("input", (e) => {
    state.search = e.target.value.trim();
    rerender();
    updateSearchStatus();
  });
  el("#search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      state.search = e.target.value.trim();
      rerender();
      updateSearchStatus();
      // 滚动到列表顶部（让用户立即看到结果）
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  });
  el("#search-btn").addEventListener("click", () => {
    const v = el("#search").value.trim();
    state.search = v;
    rerender();
    updateSearchStatus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  el("#search-clear-btn").addEventListener("click", () => {
    el("#search").value = "";
    state.search = "";
    rerender();
    updateSearchStatus();
  });
  // 搜索无结果时，「加入关注」快捷按钮
  el("#search-status").addEventListener("click", (e) => {
    const btn = e.target.closest(".add-watchlist-hint");
    if (btn) {
      const code = btn.dataset.code;
      const name = btn.dataset.name;
      addStock(code, name || code);
      el("#search").value = code;
      state.search = code;
      rerender();
      updateSearchStatus();
      // 切到 watchlist tab 让用户看到
      setView("watchlist");
    }
  });
  els(".hot-chip").forEach((c) => {
    c.addEventListener("click", () => {
      const q = c.dataset.q;
      el("#search").value = q;
      state.search = q;
      rerender();
      updateSearchStatus();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });
  els(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));

  const lm = el("#loadmore-btn");
  if (lm) lm.addEventListener("click", () => renderAll(false));

  // watchlist 编辑 UI
  const addBtn = el("#watchlist-add-btn");
  const codeInput = el("#watchlist-code-input");
  const nameInput = el("#watchlist-name-input");
  const resetBtn = el("#watchlist-reset-btn");
  const errBox = el("#watchlist-error");

  if (addBtn) {
    addBtn.addEventListener("click", () => {
      errBox.textContent = "";
      const r = addStock(codeInput.value, nameInput.value);
      if (!r.ok) {
        errBox.textContent = r.error;
        return;
      }
      codeInput.value = "";
      nameInput.value = "";
    });
  }
  if (codeInput) {
    codeInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") addBtn && addBtn.click();
    });
  }
  if (nameInput) {
    nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") addBtn && addBtn.click();
    });
  }
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      if (confirm("确认重置关注股列表为内置默认值？")) {
        resetWatchlist();
      }
    });
  }

  // 刷新数据按钮
  const refreshBtn = el("#refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", triggerRefresh);
  }
}

/* ---------- 刷新数据 ---------- */

function showRefreshToast(title, detail, kind) {
  let toast = el("#refresh-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "refresh-toast";
    toast.className = "refresh-toast";
    toast.innerHTML = `<div class="toast-title"></div><div class="toast-detail"></div>`;
    document.body.appendChild(toast);
  }
  toast.className = "refresh-toast show " + (kind || "");
  toast.querySelector(".toast-title").textContent = title;
  toast.querySelector(".toast-detail").textContent = detail;
}

function hideRefreshToast() {
  const toast = el("#refresh-toast");
  if (toast) toast.classList.remove("show");
}

async function checkRefreshServer() {
  try {
    const r = await fetch(REFRESH_API, { method: "HEAD", signal: AbortSignal.timeout(2000) });
    _refreshAvailable = r.ok;
  } catch {
    _refreshAvailable = false;
  }
}

async function triggerRefresh() {
  const btn = el("#refresh-btn");
  if (btn && btn.classList.contains("spinning")) return;

  if (!_refreshAvailable) {
    showRefreshToast(
      "🚀 触发远程更新",
      "本地刷新服务未运行。将在下次 CI 定时任务（每日 UTC 22:00）自动更新，或去 GitHub 手动触发：https://github.com/chengwenchen06-sudo/stock-radar/actions",
      ""
    );
    return;
  }

  if (btn) {
    btn.classList.add("spinning");
    btn.textContent = "刷新中…";
  }
  showRefreshToast("正在抓取数据…", "已提交到本地刷新服务，预计 30-90 秒", "");

  try {
    const r = await fetch(`${REFRESH_API}/refresh`, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { job_id } = await r.json();
    pollJobStatus(job_id);
  } catch (e) {
    showRefreshToast("❌ 刷新失败", `无法连接刷新服务: ${e.message}。请确认 scripts/refresh_server.py 已在 8766 端口运行。`, "err");
    if (btn) {
      btn.classList.remove("spinning");
      btn.textContent = "🔄 刷新";
    }
  }
}

async function pollJobStatus(jobId) {
  const btn = el("#refresh-btn");
  const start = Date.now();
  const poll = async () => {
    try {
      const r = await fetch(`${REFRESH_API}/status/${jobId}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const job = await r.json();
      if (job.status === "running" || job.status === "pending") {
        const elapsed = Math.floor((Date.now() - start) / 1000);
        showRefreshToast(
          "正在抓取数据…",
          `已用时 ${elapsed}s · 仍在运行中`,
          ""
        );
        setTimeout(poll, 1500);
        return;
      }
      // 终态
      if (job.status === "success") {
        showRefreshToast(
          "✅ 刷新成功",
          `共 ${job.total_items || "?"} 条 · 数据时间: ${new Date(job.generated_at).toLocaleString("zh-CN")}`,
          "ok"
        );
        // 自动重新加载数据
        setTimeout(async () => {
          await loadAllData();
          hideRefreshToast();
        }, 1500);
      } else if (job.status === "failed" || job.status === "timeout" || job.status === "crashed") {
        showRefreshToast(
          "❌ 刷新失败",
          `状态: ${job.status}${job.stderr_tail ? " · " + job.stderr_tail.split("\n").slice(-1)[0] : ""}`,
          "err"
        );
      }
      if (btn) {
        btn.classList.remove("spinning");
        btn.textContent = "🔄 刷新";
      }
    } catch (e) {
      showRefreshToast("❌ 轮询失败", e.message, "err");
      if (btn) {
        btn.classList.remove("spinning");
        btn.textContent = "🔄 刷新";
      }
    }
  };
  poll();
}

async function loadAllData() {
  const [data, stories, status, daily] = await Promise.all([
    fetchJSON("latest-24h.json"),
    fetchJSON("stories-merged.json"),
    fetchJSON("source-status.json"),
    fetchJSON("daily-brief.json"),
  ]);
  _lastGeneratedAt = state.data ? state.data.generated_at : "";
  state.data = data;
  state.stories = stories;
  state.status = status;
  state.daily = daily;

  // 尝试加载行情数据（可选）
  try {
    const q = await fetchJSON("latest-quotes.json");
    state.quotes = q.quotes || {};
  } catch (_) {
    state.quotes = null;
  }

  updateMeta();
  rerender();
}

function rerender() {
  const r = {
    signal: renderSignal,
    watchlist: renderWatchlist,
    all: () => renderAll(true),
    stories: renderStories,
  };
  r[state.view]?.();
}

async function init() {
  state.watchlist = loadWatchlist();
  bindChips();
  try {
    const [data, stories, status, daily] = await Promise.all([
      fetchJSON("latest-24h.json"),
      fetchJSON("stories-merged.json"),
      fetchJSON("source-status.json"),
      fetchJSON("daily-brief.json"),
    ]);
    state.data = data;
    state.stories = stories;
    state.status = status;
    state.daily = daily;

    // 尝试加载行情数据（可选）
    try {
      const q = await fetchJSON("latest-quotes.json");
      state.quotes = q.quotes || {};
    } catch (_) {
      state.quotes = null;
    }

    // 大盘复盘卡（可选，失败静默）
    loadMarketOverview().catch(() => {});

    checkRefreshServer();
    updateMeta();
    startAutoRefresh();
    setView("signal");
  } catch (e) {
    // 之前这里会把整个 body 清掉,等于告诉用户"不用 UI 了"——体验太差。
    // 改成:顶部塞一个红色 banner + 让 state 用空结构 fallback,UI 框架照旧
    // (搜索框、watchlist 编辑器、hot-chip 都能用)。
    // 这样 file:// 也能开,用户至少能看到明确提示。
    document.body.insertAdjacentHTML("afterbegin", `
      <div class="init-banner" role="alert">
        <strong>数据加载失败</strong>:${escapeHTML(e.message || "无法加载本地数据")}<br>
        本地预览请运行 <code>python3 scripts/serve.sh</code> 后访问
        <a href="http://127.0.0.1:8765/">http://127.0.0.1:8765/</a>
        (系统浏览器打开,Codex 内嵌拦 localhost)。当前页面已降级展示,搜索/加关注/编辑依然可用。
      </div>`);
    state.data = state.data || { items: [], window_hours: 48, total_items: 0, source_count: 0 };
    state.stories = state.stories || { stories: [] };
    state.status = state.status || { sites: [] };
    state.daily = state.daily || { items: [] };
    try { startAutoRefresh(); setView("signal"); } catch (_) {}
  }
}

init();
