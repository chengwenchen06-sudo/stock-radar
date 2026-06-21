/* Stock Radar 前端逻辑
 * 设计目标：承载 ~700 条真实数据而不卡顿。
 *  - 用 requestAnimationFrame 分块渲染大列表
 *  - 「全部」视图分页（每页 50 + 加载更多）
 *  - 「我的关注」视图按股票分组（watchlist 卡片）
 */
const DATA_BASE = "data";
const PAGE_SIZE = 50;
const RENDER_CHUNK = 30;

// 与 scripts/fetchers.py 中 WATCHLIST 保持一致
const WATCHLIST = [
  { code: "600519", name: "贵州茅台" },
  { code: "000001", name: "平安银行" },
  { code: "000002", name: "万科A" },
  { code: "600036", name: "招商银行" },
  { code: "601318", name: "中国平安" },
  { code: "300750", name: "宁德时代" },
  { code: "688981", name: "中芯国际" },
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
  // 「全部」分页状态
  allPage: 0,
  allRendered: [],
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

/** 提取标题里的股票代码（[] 中的数字） */
function extractStockCode(title) {
  const m = (title || "").match(/[\[【\(]\s*([0-9]{6})\s*[】\]\)]/);
  return m ? m[1] : "";
}

/** 匹配关注股 */
function isInWatchlist(it) {
  const code = extractStockCode(it.title);
  if (code && WATCHLIST.some((w) => w.code === code)) return true;
  // 也支持 raw 里的 code
  const rawCode = (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
  if (rawCode && WATCHLIST.some((w) => w.code === rawCode)) return true;
  return false;
}

function itemHTML(it) {
  const code = extractStockCode(it.title);
  const stockTag = code
    ? (WATCHLIST.find((w) => w.code === code)
        ? `<span class="tag watchlist-hit">⭐ ${code}</span>`
        : `<span class="tag">${code}</span>`)
    : "";
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
  return items.filter((it) => {
    if (state.market !== "all" && it.market !== state.market) return false;
    if (state.label !== "all" && it.label !== state.label) return false;
    if (state.importance !== "all" && it.importance_label !== state.importance) return false;
    if (state.stock) {
      // 按股票代码过滤：标题里有该代码，或 raw.code 等
      const code = extractStockCode(it.title);
      const rawCode = (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
      if (code !== state.stock && rawCode !== state.stock) return false;
    }
    if (state.search) {
      const q = state.search.toLowerCase();
      const blob = `${it.title} ${it.summary || ""} ${it.source}`.toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });
}

/* ---------- 分块渲染工具 ---------- */

function renderInChunks(container, items, buildHTML, chunkSize = RENDER_CHUNK) {
  container.innerHTML = "";
  let i = 0;
  function step() {
    if (i >= items.length) return;
    const slice = items.slice(i, i + chunkSize);
    const html = slice.map(buildHTML).join("");
    container.insertAdjacentHTML("beforeend", html);
    i += chunkSize;
    if (i < items.length) {
      requestAnimationFrame(step);
    }
  }
  step();
}

/* ---------- 各视图渲染 ---------- */

function renderSignal() {
  if (!state.data) return;
  // 高重要性 + 各市场分布均衡（每个市场前 N 条）
  const high = state.data.items.filter((i) => i.importance_label === "high");
  const filtered = applyFilters(high).slice(0, 60);
  el("#signal-list").innerHTML = filtered.map(itemHTML).join("") ||
    `<li class="story"><p>没有匹配的信号。试试切换市场或分类筛选。</p></li>`;
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
  const html = slice.map(itemHTML).join("");
  el("#all-list").insertAdjacentHTML("beforeend", html);
  state.allPage++;
  el("#all-count").textContent = `共 ${state.allRendered.length} 条 · 已显示 ${Math.min(state.allPage * PAGE_SIZE, state.allRendered.length)}`;
  const loadmore = el("#all-loadmore");
  if (state.allPage * PAGE_SIZE < state.allRendered.length) {
    loadmore.style.display = "block";
  } else {
    loadmore.style.display = "none";
  }
}

function renderWatchlist() {
  if (!state.data) return;
  // 找出所有匹配 watchlist 的条目，按股票代码分组
  const groups = {};
  for (const w of WATCHLIST) groups[w.code] = [];
  for (const it of state.data.items) {
    const code = extractStockCode(it.title) ||
                 (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
    if (groups[code]) {
      // 套用市场/分类/重要性/搜索过滤（但忽略 stock 自身）
      if (state.market !== "all" && it.market !== state.market) continue;
      if (state.label !== "all" && it.label !== state.label) continue;
      if (state.importance !== "all" && it.importance_label !== state.importance) continue;
      if (state.search) {
        const q = state.search.toLowerCase();
        const blob = `${it.title} ${it.summary || ""}`.toLowerCase();
        if (!blob.includes(q)) continue;
      }
      groups[code].push(it);
    }
  }

  // 渲染每只股的卡片
  const cards = el("#watchlist-cards");
  cards.innerHTML = "";
  let totalHits = 0;
  let activeStocks = 0;

  for (const w of WATCHLIST) {
    const items = groups[w.code];
    totalHits += items.length;
    if (items.length > 0) activeStocks++;
    items.sort((a, b) => (a.source_tier_rank - b.source_tier_rank) ||
                          (-b.importance_score - -a.importance_score));
    const card = document.createElement("div");
    card.className = "watchlist-card";
    const topItems = items.slice(0, 5);
    const recentEvents = items.length
      ? topItems.map(itemHTML).join("")
      : `<li class="story empty"><p>暂无相关条目</p></li>`;
    card.innerHTML = `
      <div class="watchlist-card-header">
        <h3>
          <span class="stock-code">${w.code}</span>
          <span class="stock-name">${w.name}</span>
        </h3>
        <span class="badge ${items.length > 0 ? 'has' : 'empty'}">${items.length} 条</span>
      </div>
      <ul class="item-list compact">${recentEvents}</ul>
      ${items.length > 5 ? `<div class="watchlist-more">还有 ${items.length - 5} 条 · 切换到「全部」并按代码过滤查看</div>` : ""}
    `;
    cards.appendChild(card);
  }

  el("#watchlist-summary").textContent =
    `${WATCHLIST.length} 只关注股 · ${activeStocks} 只有信号 · 共 ${totalHits} 条命中`;
}

function renderStories() {
  if (!state.stories) return;
  const stories = applyFilters(state.stories.stories.map((s) => ({
    ...s,
    market: (s.markets && s.markets[0]) || "global",
    published_at: s.items?.[0]?.published_at || new Date().toISOString(),
    summary: "",
  })));
  // 故事线优先 importance 排序 + 分块渲染（避免一次 394 条卡顿）
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

function updateMeta() {
  if (!state.data) return;
  const d = state.data;
  el("#data-meta").textContent =
    `数据: ${new Date(d.generated_at).toLocaleString("zh-CN")} · ${d.total_items} 条 · ${d.source_count} 个信源`;
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

function buildWatchlistChips() {
  const container = el("#watchlist-chips");
  for (const w of WATCHLIST) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.dataset.stock = w.code;
    btn.textContent = `${w.code} ${w.name}`;
    container.appendChild(btn);
  }
}

function bindChips() {
  buildWatchlistChips();

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
  els("#watchlist-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.stock = c.dataset.stock || "";
      els("#watchlist-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      rerender();
    });
  });
  el("#search").addEventListener("input", (e) => {
    state.search = e.target.value.trim();
    rerender();
  });
  els(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));

  // 加载更多按钮
  const lm = el("#loadmore-btn");
  if (lm) lm.addEventListener("click", () => renderAll(false));
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
    updateMeta();
    setView("signal");
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px;text-align:center;color:#f85149">数据加载失败：${e.message}<br><br>本地预览请运行 <code>python3 -m http.server 8080</code> 后访问 <a href="http://localhost:8080">localhost:8080</a></div>`;
  }
}

init();
