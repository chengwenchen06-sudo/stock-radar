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

// 内置默认关注股（用户清空 localStorage 或首次访问时使用）
const DEFAULT_WATCHLIST = [
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
  if (!/^[0-9]{6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(code)) {
    return { ok: false, error: "代码格式：6 位数字（A 股 / 港股）或 1-5 位字母（美股）" };
  }
  if (state.watchlist.some((w) => w.code === code)) {
    return { ok: false, error: "已在关注列表中" };
  }
  state.watchlist.push({ code, name: name || lookupStockName(code) || "" });
  saveWatchlist(state.watchlist);
  rebuildWatchlistChips();
  rerender();
  return { ok: true };
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
      const code = extractStockCode(it.title);
      const rawCode = (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
      if (code !== state.stock && rawCode !== state.stock) return false;
    }
    if (state.search) {
      const q = state.search.toLowerCase().trim();
      if (q) {
        const blob = [
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
        // 多 token AND；每个 token 可展开为自身 + 代码别名
        const rawTokens = q.split(/\s+/).filter(Boolean);
        const expandedTokens = rawTokens.flatMap((t) => {
          const out = [t];
          // 如果 token 看起来像股票代码，自动加入名称别名（如 "300750" → ["300750","宁德时代","时代"]）
          if (/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(t.toUpperCase())) {
            const aliases = lookupAliases(t.toUpperCase());
            for (const a of aliases) out.push(a.toLowerCase());
          }
          return out;
        });
        // 一个 token 命中它自己 OR 任意别名即可
        const ok = rawTokens.every((origTok) => {
          const cands = [origTok];
          if (/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(origTok.toUpperCase())) {
            const aliases = lookupAliases(origTok.toUpperCase());
            for (const a of aliases) cands.push(a.toLowerCase());
          }
          return cands.some((c) => blob.includes(c));
        });
        if (!ok) return false;
      }
    }
    return true;
  });
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
    `<li class="story"><p>没有匹配的条目。试试切换市场/分类筛选，或清空搜索。</p></li>`;
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
  const q = state.search.trim();
  if (!q) {
    box.className = "search-status";
    box.textContent = "";
    return;
  }
  if (!state.data) return;
  const tokens = q.toLowerCase().split(/\s+/).filter(Boolean);
  const matched = state.data.items.filter((it) => {
    const blob = [
      it.title || "", it.summary || "", it.source || "",
      (it.raw && it.raw.code) || "",
      (it.raw && it.raw.sec_code) || "",
      (it.raw && it.raw.stock_code) || "",
      (it.raw && it.raw.short_name) || "",
      (it.raw && it.raw.sec_name) || "",
      (it.raw && it.raw.name) || "",
      (it.raw && it.raw.industry) || "",
      (it.raw && it.raw.sector) || "",
    ].join(" ").toLowerCase();
    return tokens.every((tok) => {
      const cands = [tok];
      if (/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(tok.toUpperCase())) {
        for (const a of lookupAliases(tok.toUpperCase())) cands.push(a.toLowerCase());
      }
      return cands.some((c) => blob.includes(c));
    });
  });
  // 按来源统计
  const bySrc = {};
  for (const it of matched) bySrc[it.source] = (bySrc[it.source] || 0) + 1;
  if (matched.length === 0) {
    box.className = "search-status empty";
    // 给出可能的建议
    const codeLike = q.match(/^[0-9]{5,6}|[A-Z]{1,5}/i);
    let hint = `未找到与「<span class="code-chip">${escapeHTML(q)}</span>」相关的条目`;
    if (codeLike) {
      const code = codeLike[0].toUpperCase();
      const name = lookupStockName(code);
      hint += `。`;
      if (name) {
        hint += `<br>💡 你搜的是 <span class="code-chip">${code}</span> ${escapeHTML(name)} —— 可试试热门代码按钮，或加到「我的关注」。`;
      } else {
        hint += `<br>💡 <span class="code-chip">${code}</span> 不在常用代码表里，今天 (${new Date().toLocaleDateString("zh-CN")}) 该股可能没有公告/新闻/异动。`;
      }
    }
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
  if (!state.data) return;
  const groups = {};
  for (const w of state.watchlist) groups[w.code] = [];
  for (const it of state.data.items) {
    const code = extractStockCode(it.title) ||
                 (it.raw && (it.raw.code || it.raw.sec_code || it.raw.stock_code)) || "";
    if (groups[code]) {
      if (state.market !== "all" && it.market !== state.market) continue;
      if (state.label !== "all" && it.label !== state.label) continue;
      if (state.importance !== "all" && it.importance_label !== state.importance) continue;
      if (state.search) {
        const q = state.search.toLowerCase().trim();
        if (q) {
          const blob = [
            it.title || "", it.summary || "", it.source || "",
            (it.raw && it.raw.code) || "",
            (it.raw && it.raw.sec_code) || "",
            (it.raw && it.raw.short_name) || "",
            (it.raw && it.raw.sec_name) || "",
            (it.raw && it.raw.industry) || "",
          ].join(" ").toLowerCase();
          const rawTokens = q.split(/\s+/).filter(Boolean);
          const ok = rawTokens.every((origTok) => {
            const cands = [origTok];
            if (/^[0-9]{5,6}$|^[A-Z]{1,5}(\.[A-Z])?$/.test(origTok.toUpperCase())) {
              const aliases = lookupAliases(origTok.toUpperCase());
              for (const a of aliases) cands.push(a.toLowerCase());
            }
            return cands.some((c) => blob.includes(c));
          });
          if (!ok) continue;
        }
      }
      groups[code].push(it);
    }
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
    updateMeta();
    setView("signal");
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px;text-align:center;color:#f85149">数据加载失败：${e.message}<br><br>本地预览请运行 <code>python3 -m http.server 8080</code> 后访问 <a href="http://localhost:8080">localhost:8080</a></div>`;
  }
}

init();
