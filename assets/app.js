/* Stock Radar 前端逻辑 */
const DATA_BASE = "data";
const state = {
  view: "signal",
  market: "all",
  label: "all",
  importance: "all",
  search: "",
  data: null,
  stories: null,
  status: null,
  daily: null,
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

function itemHTML(it) {
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
    if (state.search) {
      const q = state.search.toLowerCase();
      const blob = `${it.title} ${it.summary || ""} ${it.source}`.toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });
}

function renderSignal() {
  if (!state.data) return;
  const top = state.data.items.filter((i) => i.importance_label === "high").slice(0, 30);
  const filtered = applyFilters(top);
  el("#signal-list").innerHTML = filtered.map(itemHTML).join("") ||
    `<li class="story"><p>没有匹配的信号。试试切换市场或分类筛选。</p></li>`;
}

function renderAll() {
  if (!state.data) return;
  const filtered = applyFilters(state.data.items);
  el("#all-count").textContent = `${filtered.length} / ${state.data.items.length} 条`;
  el("#all-list").innerHTML = filtered.map(itemHTML).join("") ||
    `<li class="story"><p>没有匹配的条目。</p></li>`;
}

function renderStories() {
  if (!state.stories) return;
  const stories = applyFilters(state.stories.stories.map((s) => ({
    ...s,
    market: (s.markets && s.markets[0]) || "global",
    published_at: s.items?.[0]?.published_at || new Date().toISOString(),
    summary: "",
  })));
  el("#stories-list").innerHTML = stories.map(storyHTML).join("") ||
    `<li class="story"><p>暂无故事线。</p></li>`;
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
    signal: renderSignal, all: renderAll, stories: renderStories,
    daily: renderDaily, status: renderStatus,
  };
  renderers[v]?.();
}

function bindChips() {
  // market chips
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
  });
  els(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));
}

function rerender() {
  if (state.view === "signal") renderSignal();
  else if (state.view === "all") renderAll();
  else if (state.view === "stories") renderStories();
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
