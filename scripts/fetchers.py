"""
stock-radar fetchers.

每个 fetcher 接口统一：fetch_<name>(session, now) -> list[RawItem]
RawItem = dict(title, url, published_at, source, source_tier_rank, market, summary, raw)

信源分层 source_tier_rank：
  0 = 官方一手（交易所/公司公告/监管）
  1 = 主流财经媒体（华尔街见闻/财联社/Reuters 等）
  2 = 二线财经
  3 = RSS/OPML 兜底
  5 = 社交/聚合参考

市场 market：cn / hk / us / global
"""
from __future__ import annotations
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from xml.etree import ElementTree as ET

import feedparser
import requests

log = logging.getLogger("stock_radar.fetchers")

# 浏览器风格 UA，绕过大部分国内站对默认 UA 的拦截
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA = f"Stock-Radar/0.2 ({BROWSER_UA})"

# ---------- 内置源定义 ----------

BUILTIN_SOURCES = [
    # A 股公告（一手）
    {"id": "eastmoney_ann",  "name": "东方财富公告",    "market": "cn",     "tier": 0, "kind": "json",
     "url": "https://np-anotice-stock.eastmoney.com/api/security/ann"},
    {"id": "cninfo",         "name": "巨潮资讯公告",    "market": "cn",     "tier": 0, "kind": "json",
     "url": "http://www.cninfo.com.cn/new/hisAnnouncement/query"},
    # A 股快讯（深度文章）
    {"id": "wallstcn_live",  "name": "华尔街见闻快讯",   "market": "cn",     "tier": 1, "kind": "json",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives"},
    {"id": "wallstcn_art",   "name": "华尔街见闻文章",   "market": "cn",     "tier": 1, "kind": "json",
     "url": "https://api-one.wallstcn.com/apiv1/content/articles"},
    # 注释掉的源（接口下线/被代理挡，保留代码作 fallback）：
    # - cls_telegraph: 财联社 nodeapi 接口下线（404）
    # {"id": "cls_telegraph",  "name": "财联社电报",     "market": "cn",     "tier": 1, "kind": "json",
    #  "url": "https://www.cls.cn/nodeapi/updateTelegraphList"},
    # 港股（一手）
    {"id": "hkexnews",       "name": "港交所披露",     "market": "hk",     "tier": 0, "kind": "json",
     "url": "https://www1.hkexnews.hk/search/titleSearchServlet.do"},
    # 美股公告（一手）
    {"id": "sec_edgar_8k",   "name": "SEC EDGAR 8-K", "market": "us",     "tier": 0, "kind": "json",
     "url": "https://data.sec.gov/submissions/CIK0000320193.json"},
    # AkShare 异动层（A 股实时）
    {"id": "akshare_zt",     "name": "AkShare 涨停池",   "market": "cn", "tier": 1, "kind": "akshare", "url": "zt"},
    {"id": "akshare_zbgc",   "name": "AkShare 炸板池",   "market": "cn", "tier": 1, "kind": "akshare", "url": "zbgc"},
    # - akshare_sector_flow: push2.eastmoney.com 被代理挡（ProxyError），HTML 入口 404
    # {"id": "akshare_sector_flow", "name": "AkShare 板块资金流", "market": "cn", "tier": 1, "kind": "akshare", "url": "sector_flow"},
    {"id": "akshare_lhb",    "name": "AkShare 龙虎榜",   "market": "cn", "tier": 2, "kind": "akshare", "url": "lhb"},
    {"id": "akshare_eco",    "name": "AkShare 财经日历", "market": "global", "tier": 1, "kind": "akshare", "url": "eco"},
    {"id": "akshare_news",   "name": "AkShare 个股新闻", "market": "cn", "tier": 2, "kind": "akshare", "url": "news"},
]

# AkShare 个股新闻关注列表（持仓/自选股）
WATCHLIST = ["600519", "000001", "000002", "600036", "601318", "300750", "688981"]


# ---------- 通用 fetcher ----------

def _mk_item(title: str, url: str, ts: datetime, src: dict, summary: str = "", raw: dict | None = None) -> dict:
    return {
        "title": title.strip(),
        "url": url.strip(),
        "published_at": ts.isoformat(),
        "source": src["name"],
        "source_id": src["id"],
        "source_tier_rank": src["tier"],
        "market": src["market"],
        "summary": (summary or "")[:500],
        "raw": raw or {},
    }

# ---------- 东方财富 公告（A 股一手） ----------

# 公告分类映射：东财 column_code -> 我们 label
EM_COLUMN_TO_LABEL = {
    "050001": "earnings",    # 年度报告
    "050002": "earnings",    # 半年度报告
    "050003": "other",       # 投资者关系活动
    "050004": "mna",         # 重大事项
    "050005": "earnings",    # 季度报告
    "050006": "capital",     # 利润分配/分红
    "050007": "management",  # 董事会/监事会
    "050008": "other",       # 重大合同
    "050009": "mna",         # 收购/合并
    "050010": "policy",      # 监管问询函
}

def fetch_eastmoney_ann(session: requests.Session, src: dict, page_size: int = 50) -> list[dict]:
    """抓东方财富公告列表。仅取最近 24h 内。"""
    out = []
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://data.eastmoney.com/"}
    # 多页拉一些，覆盖率高
    for page in range(1, 4):
        params = {
            "sr": -1, "page_size": page_size, "page_index": page,
            "ann_type": "A", "client_source": "web", "f_node": 0, "s_node": 0,
        }
        try:
            r = session.get(src["url"], params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or {}
        except Exception as e:
            log.warning("eastmoney ann page %d failed: %s", page, e)
            continue
        for it in data.get("list", []):
            # display_time 形如 "2026-06-19 11:49:06:549"（最后一段是毫秒）
            ts_str = (it.get("display_time") or "").rsplit(":", 1)[0]  # 去毫秒
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            # fetcher 内不过滤，交给 collect_all 统一按 window-hours 处理
            # 这里设 96h 避免误丢，但实际过滤在外层
            codes = it.get("codes") or []
            sec = codes[0] if codes else {}
            stock_code = sec.get("stock_code", "")
            short_name = sec.get("short_name", "")
            market_code = sec.get("market_code", "")
            market = "cn"
            title = (it.get("title") or "").strip()
            if not title:
                continue
            # 完整标题加股票前缀
            full_title = f"[{stock_code} {short_name}] {title}" if stock_code else title
            # 详情链接（art_code）
            art_code = it.get("art_code", "")
            detail_url = f"https://data.eastmoney.com/notices/detail/{art_code}.html" if art_code else src["url"]
            # 栏目
            columns = it.get("columns") or []
            column_code = columns[0].get("column_code") if columns else ""
            raw_label = EM_COLUMN_TO_LABEL.get(column_code, "other")
            out.append(_mk_item(
                full_title, detail_url, ts, src,
                summary=f"东财栏目:{columns[0].get('column_name','') if columns else ''}",
                raw={"stock_code": stock_code, "short_name": short_name,
                     "art_code": art_code, "column_code": column_code, "raw_label": raw_label,
                     "market_code": market_code},
            ))
        time.sleep(0.2)
    return out

# ---------- 巨潮资讯 (A股公告) ----------

def fetch_cninfo(session: requests.Session, src: dict) -> list[dict]:
    """巨潮历史公告查询。无鉴权但有频控，建议每分钟 ≤ 10 次。
    关键经验：column 必须用 szse 或 sse（szsh 返回 0 条）。
    announcementTime 是 unix ms 时间戳，按 UTC 解释但对应北京时间发布瞬间，
    因此用 48h 宽松窗口过滤。
    """
    out = []
    end = datetime.now()
    start = end - timedelta(days=2)
    headers = {"User-Agent": BROWSER_UA, "Referer": "http://www.cninfo.com.cn/"}
    for column in ("szse", "sse"):  # 沪深两市分别拉
        payload = {
            "stock": "", "tabName": "fulltext",
            "pageSize": 50, "pageNum": 1,
            "column": column, "category": "",
            "plate": "",
            "seDate": f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}",
            "searchkey": "", "secid": "",
            "sortName": "", "sortType": "",
            "isHLtitle": "true",
        }
        try:
            r = session.post(src["url"], data=payload, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("cninfo column=%s failed: %s", column, e)
            continue
        for a in data.get("announcements") or []:
            title = re.sub(r"<[^>]+>", "", (a.get("announcementTitle") or "").strip())
            if not title:
                continue
            pdf_url = a.get("adjunctUrl") or ""
            if pdf_url and not pdf_url.startswith("http"):
                pdf_url = "http://static.cninfo.com.cn/" + pdf_url
            ts_ms = a.get("announcementTime") or 0
            if ts_ms:
                # 巨潮 announcementTime 是 unix ms 但代表北京时间。
                # 直接当 UTC parse 会偏移 -8h，导致所有 ts 早 8h，错过 24h 窗口。
                # 修正：parse 后 +8h 还原为真实发布时间。
                ts_utc_naive = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                ts = ts_utc_naive + timedelta(hours=8)
            else:
                ts = datetime.now(timezone.utc)
            # 周末没有公告，窗口放宽到 60h 确保周五发布的也能保留
            if ts < datetime.now(timezone.utc) - timedelta(hours=60):
                continue
            sec_code = a.get("secCode") or ""
            sec_name = a.get("secName") or ""
            full_title = f"[{sec_code} {sec_name}] {title}" if sec_code else title
            out.append(_mk_item(
                full_title, pdf_url or src["url"], ts, src,
                summary=a.get("announcementContent") or "",
                raw={"sec_code": sec_code, "sec_name": sec_name, "column": column},
            ))
        time.sleep(0.3)
    return out

# ---------- 华尔街见闻 lives（实时快讯） ----------

# 内容类型映射：wallstcn content -> 我们 label
def _wallstcn_label(content_text: str, title: str) -> str:
    blob = f"{title} {content_text}".lower()
    if any(k in blob for k in ["财报", "业绩", "净利润", "营收", "earnings", "revenue"]):
        return "earnings"
    if any(k in blob for k in ["回购", "buyback", "分红", "dividend"]):
        return "capital"
    if any(k in blob for k in ["收购", "并购", "acqui", "merger"]):
        return "mna"
    if any(k in blob for k in ["美联储", "央行", "降息", "加息", "fed", "rate"]):
        return "policy"
    if any(k in blob for k in ["cpi", "ppi", "通胀", "失业", "非农", "gdp"]):
        return "macro"
    if any(k in blob for k in ["开盘", "收盘", "指数", "恒指", "大盘"]):
        return "market"
    return "other"

def fetch_wallstcn_live(session: requests.Session, src: dict, limit: int = 40) -> list[dict]:
    """抓华尔街见闻 7x24 快讯。多 channel + 多页确保 24h 内覆盖。"""
    out = []
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://wallstcn.com/"}
    channels = [
        ("global-channel", "global"),
        ("a-stock-channel", "cn"),
        ("us-stock-channel", "us"),
        ("hk-stock-channel", "hk"),
    ]
    for channel, market in channels:
        # 分页：先 cursor=0 拉最新一批，再用 next_cursor 拉更早的
        cursor = ""
        pages = 0
        while pages < 3:  # 每个 channel 最多 3 页
            params = {"channel": channel, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            try:
                r = session.get(src["url"], params=params, headers=headers, timeout=15)
                r.raise_for_status()
                payload = r.json().get("data") or {}
            except Exception as e:
                log.warning("wallstcn live channel %s page %d failed: %s", channel, pages, e)
                break
            items = payload.get("items") or []
            if not items:
                break
            for it in items:
                try:
                    ts = datetime.fromtimestamp(int(it["display_time"]), tz=timezone.utc)
                except Exception:
                    continue
                if ts < datetime.now(timezone.utc) - timedelta(hours=24):
                    continue
                title = (it.get("content_text") or "").strip().replace("\n", " ")
                if not title:
                    continue
                uri = it.get("uri") or f"/livenews/{it.get('id','')}"
                url = ("https://wallstcn.com" + uri) if uri.startswith("/") else uri
                raw_label = _wallstcn_label(title, "")
                out.append(_mk_item(
                    title[:200], url, ts, {**src, "market": market},
                    summary=f"华尔街见闻 · {channel}",
                    raw={"channel": channel, "id": it.get("id"), "raw_label": raw_label},
                ))
            cursor = payload.get("next_cursor") or ""
            pages += 1
            if not cursor:
                break
            time.sleep(0.2)
        time.sleep(0.15)
    return out

# ---------- 华尔街见闻 articles（深度文章） ----------

def fetch_wallstcn_articles(session: requests.Session, src: dict, limit: int = 20) -> list[dict]:
    """抓华尔街见闻深度文章。"""
    out = []
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://wallstcn.com/"}
    for channel in ["global-channel", "a-stock-channel", "us-stock-channel", "hk-stock-channel"]:
        market = {"a-stock-channel": "cn", "us-stock-channel": "us",
                  "hk-stock-channel": "hk", "global-channel": "global"}.get(channel, "global")
        try:
            r = session.get(src["url"], params={"limit": limit, "channel": channel},
                            headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or {}
        except Exception as e:
            log.warning("wallstcn art channel %s failed: %s", channel, e)
            continue
        for it in data.get("items", []):
            try:
                ts = datetime.fromtimestamp(int(it["display_time"]), tz=timezone.utc)
            except Exception:
                continue
            if ts < datetime.now(timezone.utc) - timedelta(hours=24):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            uri = it.get("uri") or ""
            url = "https://wallstcn.com" + uri if uri.startswith("/") else (uri or src["url"])
            short = (it.get("content_short") or "")[:300]
            raw_label = _wallstcn_label(title, short)
            out.append(_mk_item(
                title, url, ts, {**src, "market": market},
                summary=short, raw={"channel": channel, "id": it.get("id"), "raw_label": raw_label},
            ))
        time.sleep(0.15)
    return out

# ---------- 财联社电报 (A股 7x24 快讯) ----------

def fetch_cls_telegraph(session: requests.Session, src: dict, limit: int = 30) -> list[dict]:
    """财联社 7x24 实时电报，作为 wallstcn_live 的独立备份。"""
    out = []
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www.cls.cn/"}
    # last_time 是 24h 前的 unix 秒，CLS 用它做增量返回
    last_time = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    payload = {
        "last_time": last_time,
        "rn": limit,
        "os": "web",
        "sv": "7.7.5",
    }
    try:
        r = session.post(src["url"], data=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug("cls telegraph unavailable: %s", e)
        return out
    roll = ((data.get("data") or {})).get("roll_data") or []
    for tg in roll:
        title = (tg.get("title") or tg.get("brief") or "").strip()
        if not title:
            continue
        ctime = tg.get("ctime") or 0
        try:
            ts = datetime.fromtimestamp(int(ctime), tz=timezone.utc)
        except Exception:
            continue
        if ts < datetime.now(timezone.utc) - timedelta(hours=24):
            continue
        tg_id = tg.get("id", "")
        url = f"https://www.cls.cn/detail/{tg_id}" if tg_id else src["url"]
        subject = tg.get("subject") or ""
        out.append(_mk_item(
            title[:200], url, ts, src,
            summary=f"财联社电报 · {subject}" if subject else "财联社电报",
            raw={"id": tg_id, "subject": subject},
        ))
    return out

# ---------- 港交所披露 ----------

def fetch_hkexnews(session: requests.Session, src: dict) -> list[dict]:
    """港交所披露易：titleSearchServlet 标题列表。"""
    out = []
    # 跨 2 天避免时区问题：start = 昨天 00:00, end = 今天 23:59
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    params = {
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK",
        "stockId": "", "documentType": "-1",
        "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
        "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
        "rowRange": 30, "lang": "ZH",
    }
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www1.hkexnews.hk/"}
    try:
        # macOS 上 www1.hkexnews.hk 证书链不被信任，禁用验证
        r = session.get(src["url"], params=params, headers=headers,
                        timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("hkexnews failed: %s", e)
        return out
    # 抑制 urllib3 InsecureRequestWarning
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    result = data.get("result") or ""
    try:
        inner = json.loads(result)
    except Exception:
        return out
    for it in inner[:30]:
        title = (it.get("TITLE") or it.get("title") or "").strip()
        doc_link = it.get("FILE_LINK") or it.get("fileLink") or ""
        if not title:
            continue
        date_str = it.get("DATE_TIME") or it.get("dateTime") or ""
        ts = datetime.now(timezone.utc)
        try:
            ts = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        if ts < datetime.now(timezone.utc) - timedelta(hours=36):
            continue
        out.append(_mk_item(title, doc_link, ts, src, raw=it))
    return out

# ---------- SEC EDGAR (美股 8-K 等) ----------

DEFAULT_CIKS = [
    ("0000320193", "AAPL"), ("0000789019", "MSFT"), ("0001045810", "NVDA"),
    ("0001318605", "TSLA"), ("0001652044", "GOOGL"), ("0001018724", "AMZN"),
    ("0001326801", "META"), ("0001067983", "BRK.B"), ("0000019617", "JPM"),
    ("0000051143", "IBM"), ("0000034088", "XOM"), ("0000078003", "PFE"),
    ("0000080424", "WMT"), ("0000101829", "CVX"), ("0000093410", "NFLX"),
]

def fetch_sec_edgar(session: requests.Session, src: dict, ciks: list[tuple[str, str]] | None = None) -> list[dict]:
    """SEC EDGAR 全文搜索（最近 48h 8-K / 10-K / 10-Q）。
    用 efts.sec.gov/LATEST/search-index 拿全市场最新 8-K，
    比按 CIK 列表轮询覆盖率更高（任何公司提交都能捕获）。
    """
    out = []
    headers = {"User-Agent": "Stock Radar admin@example.com (test project)"}
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    # efts 接受多 form 用 | 分隔。q 必须为空（不能 %22%22），否则 0 hits
    hits = []
    for form_filter in ("8-K", "10-K", "10-Q"):
        try:
            url = (
                f"https://efts.sec.gov/LATEST/search-index?q="
                f"&dateRange=custom&startdt={start}&enddt={end}"
                f"&forms={form_filter}"
            )
            r = session.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            hits.extend(data.get("hits", {}).get("hits", []))
        except Exception as e:
            log.debug("sec edgar efts %s failed: %s", form_filter, e)
            continue
        time.sleep(0.2)  # 礼貌限流
    if not hits:
        return out
    for h in hits[:80]:  # 限制最多 80 条避免噪音
        src_field = h.get("_source", {})
        # 提取公司信息
        display_names = src_field.get("display_names", [])
        company = display_names[0] if display_names else "Unknown"
        # 解析 ticker (e.g. "Apple Inc.  (AAPL)  (CIK 0000320193)" -> "AAPL")
        ticker = ""
        m = re.search(r"\(([A-Z]{1,5})\)", company)
        if m: ticker = m.group(1)
        # 提交日期作为发布时间
        file_date = src_field.get("file_date", "")  # 形如 "2026-06-22"
        try:
            ts = datetime.strptime(file_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < datetime.now(timezone.utc) - timedelta(hours=48):
            continue
        form = src_field.get("form", "8-K")
        adsh = src_field.get("adsh", "")
        cik_list = src_field.get("ciks", [])
        cik = cik_list[0] if cik_list else ""
        # filing URL: Archives/edgar/data/{cik}/{adsh去掉横线}/
        acc_clean = adsh.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/" if cik else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
        # 标题：优先 ticker，否则公司名
        prefix = f"[{ticker}] " if ticker else ""
        title = f"{prefix}{company} 提交 {form}"
        # 取第一条 highlight 作 summary
        hlts = h.get("highlight", {}).get("content", [])
        summary = hlts[0][:300] if hlts else f"{company} 于 {file_date} 提交 {form} 公告"
        out.append(_mk_item(
            title, filing_url, ts, src,
            summary=summary,
            raw={"ticker": ticker, "form": form, "company": company,
                 "adsh": adsh, "ciks": cik_list},
        ))
    return out

# ---------- OPML 私有 RSS（兜底） ----------

def load_opml(path: str) -> list[dict]:
    srcs = []
    try:
        tree = ET.parse(path)
    except Exception as e:
        log.warning("opml parse failed %s: %s", path, e)
        return srcs
    root = tree.getroot()
    body = root.find("body")
    if body is None:
        return srcs
    for outline in body.iter("outline"):
        xml_url = outline.attrib.get("xmlUrl")
        title = outline.attrib.get("text") or outline.attrib.get("title") or ""
        if not xml_url:
            continue
        srcs.append({"id": title, "name": title, "market": "global", "tier": 3, "kind": "rss", "url": xml_url})
    return srcs

def fetch_opml_rss(session: requests.Session, opml_path: str) -> list[dict]:
    srcs = load_opml(opml_path)
    out = []
    for s in srcs:
        out.extend(_parse_rss(s["url"], s["name"], s["market"], s["tier"]))
    return out

def _parse_rss(url: str, source_name: str, market: str, tier: int, limit: int = 30) -> list[dict]:
    out = []
    try:
        d = feedparser.parse(url, agent=BROWSER_UA)
    except Exception as e:
        log.warning("rss parse failed %s: %s", url, e)
        return out
    for e in d.entries[:limit]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        published = e.get("published_parsed") or e.get("updated_parsed")
        if published:
            ts = datetime(*published[:6], tzinfo=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        out.append(_mk_item(
            title, link, ts,
            {"id": source_name, "name": source_name, "market": market, "tier": tier, "kind": "rss"},
            summary=(e.get("summary") or "")[:500],
        ))
    return out

# ---------- AkShare 异动层（A 股实时） ----------

def _ak_dt_now():
    return datetime.now(timezone.utc)

def _ak_dt_today(hour: int, minute: int = 0):
    now = datetime.now(timezone.utc)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

def fetch_akshare_zt(session, src) -> list[dict]:
    """涨停池：当日涨停个股清单（异动信号 / 当日实时）。"""
    out = []
    try:
        import akshare as ak
        today = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=today)
    except Exception as e:
        log.warning("akshare zt failed: %s", e)
        return out
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code or not name:
            continue
        change = row.get("涨跌幅", 0)
        first_time = str(row.get("首次封板时间", "")).strip()
        ts = _ak_dt_now()
        try:
            if len(first_time) == 6 and first_time.isdigit():
                ts = _ak_dt_today(int(first_time[:2]), int(first_time[2:4]))
        except Exception:
            pass
        seal = row.get("封板资金", 0)
        limit_n = row.get("连板数", 0)
        industry = str(row.get("所属行业", "")).strip()
        title = f"[{code} {name}] 涨停 +{change:.2f}% 连板{limit_n}板（{industry}）"
        summary = f"首次封板 {first_time or '-'} · 封板资金 {seal/1e8:.2f}亿"
        url = f"https://quote.eastmoney.com/concept/{'sh' if str(code).startswith('6') else 'sz'}{code}.html"
        out.append(_mk_item(
            title, url, ts, src, summary=summary,
            raw={"code": code, "name": name, "change": float(change),
                 "limit_n": int(limit_n), "industry": industry, "seal_money": float(seal)},
        ))
    return out

def fetch_akshare_zbgc(session, src) -> list[dict]:
    """炸板池：触及涨停后开板的个股（异动信号）。"""
    out = []
    try:
        import akshare as ak
        today = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_zbgc_em(date=today)
    except Exception as e:
        log.warning("akshare zbgc failed: %s", e)
        return out
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code or not name:
            continue
        change = row.get("涨跌幅", 0)
        first_time = str(row.get("首次封板时间", "")).strip()
        zb_n = row.get("炸板次数", 0)
        industry = str(row.get("所属行业", "")).strip()
        ts = _ak_dt_now()
        try:
            if len(first_time) == 6 and first_time.isdigit():
                ts = _ak_dt_today(int(first_time[:2]), int(first_time[2:4]))
        except Exception:
            pass
        title = f"[{code} {name}] 炸板 {zb_n}次 涨{change:.2f}%（{industry}）"
        summary = f"首次封板 {first_time or '-'} · 炸板 {zb_n} 次"
        url = f"https://quote.eastmoney.com/concept/{'sh' if str(code).startswith('6') else 'sz'}{code}.html"
        out.append(_mk_item(
            title, url, ts, src, summary=summary,
            raw={"code": code, "name": name, "change": float(change),
                 "zb_n": int(zb_n), "industry": industry},
        ))
    return out

def fetch_akshare_sector_flow(session, src) -> list[dict]:
    """板块资金流排名（今日）。给盘中异动加一个「资金面」维度。"""
    out = []
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator="今日")
    except Exception as e:
        log.debug("akshare sector flow unavailable: %s", e)
        return out
    if df is None or len(df) == 0:
        return out
    for _, row in df.iterrows():
        name = str(row.get("名称", "")).strip()
        if not name:
            continue
        try:
            change = float(row.get("今日涨跌幅", 0) or 0)
            net = float(row.get("今日主力净流入-净额", 0) or 0)
        except Exception:
            continue
        ts = _ak_dt_now()
        title = f"[{name}] 板块涨{change:+.2f}% 主力净流入{net/1e8:+.2f}亿"
        summary = f"涨跌幅 {change:+.2f}% · 主力净额 {net/1e8:+.2f}亿"
        url = "https://data.eastmoney.com/bkzj/hy.html"
        out.append(_mk_item(
            title, url, ts, src, summary=summary,
            raw={"sector": name, "change": change, "net_inflow": net},
        ))
    return out

def fetch_akshare_lhb(session, src) -> list[dict]:
    """龙虎榜详情（新浪源）。原 stock_lhb_detail_em 经常返回空，
    改用 stock_lhb_detail_daily_sina / ggtj_sina。
    """
    out = []
    try:
        import akshare as ak
        # 只用 stock_lhb_detail_daily_sina (当日详情 ~55 条)
        # stock_lhb_ggtj_sina 是累计榜单 (250+ 条太冗)
        df = ak.stock_lhb_detail_daily_sina()
    except Exception as e:
        log.debug("akshare lhb unavailable: %s", e)
        return out
    if df is None or len(df) == 0:
        return out
    ts = _ak_dt_now()
    # 列名映射：detail_daily_sina 和 ggtj_sina 列名不同
    code_col = "股票代码" if "股票代码" in df.columns else "代码"
    name_col = "股票名称" if "股票名称" in df.columns else "名称"
    reason_col = "指标" if "指标" in df.columns else "上榜原因"
    change_col = "对应值" if "对应值" in df.columns else None
    net_col = "净额" if "净额" in df.columns else None
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip()
        if not code or not name:
            continue
        reason = str(row.get(reason_col, "")).strip()
        try:
            change = float(row.get(change_col, 0)) if change_col else 0.0
        except Exception:
            change = 0.0
        try:
            net = float(row.get(net_col, 0)) if net_col else 0.0
        except Exception:
            net = 0.0
        title = f"[{code} {name}] 龙虎榜 偏离{change:+.2f}%"
        if net:
            title += f" 净额{net/1e4:+.2f}万"
        summary = f"{reason} · 偏离值 {change:+.2f}%"
        url = f"https://vip.stock.finance.sina.com.cn/q/go.php/vLHBData/kind/ggtj/index.phtml"
        out.append(_mk_item(
            title, url, ts, src, summary=summary,
            raw={"code": code, "name": name, "reason": reason,
                 "change": change, "net": net},
        ))
    return out

def fetch_akshare_eco(session, src) -> list[dict]:
    """财经日历：未来 7 天重要经济数据 / 央行决议。"""
    out = []
    try:
        import akshare as ak
        from datetime import date as _date, timedelta as _td
        dfs = []
        for d in range(0, 8):
            ds = (_date.today() + _td(days=d)).strftime("%Y%m%d")
            try:
                df = ak.news_economic_baidu(date=ds)
                if df is not None and len(df):
                    dfs.append(df)
            except Exception:
                continue
        if not dfs:
            return out
        import pandas as pd
        full = pd.concat(dfs, ignore_index=True)
    except Exception as e:
        log.warning("akshare eco failed: %s", e)
        return out
    for _, row in full.iterrows():
        region = str(row.get("地区", "")).strip()
        event = str(row.get("事件", "")).strip()
        importance = row.get("重要性", 1)
        date_s = str(row.get("日期", "")).strip()
        time_s = str(row.get("时间", "")).strip()
        if not event:
            continue
        try:
            imp_val = float(importance) if importance not in (None, "") else 1
        except Exception:
            imp_val = 1
        # 数据重要性只有 1-2，所以全收，按地区分级：
        # 美国 / 中国 全收；其他地区只收重要性 = 2
        if region not in ("美国", "中国") and imp_val < 2:
            continue
        ts = _ak_dt_now()
        try:
            if date_s and time_s:
                ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        title = f"[{region}] {event}"
        summary = f"公布:{row.get('公布','-')} 预期:{row.get('预期','-')} 前值:{row.get('前值','-')} 重要性:{imp_val}"
        market = "us" if region in ("美国", "北美洲") else ("cn" if region in ("中国",) else "global")
        src_market = {**src, "market": market}
        out.append(_mk_item(
            title, "https://www.cls.cn/telegraph", ts, src_market, summary=summary,
            raw={"region": region, "importance": imp_val, "event": event,
                 "date": date_s, "time": time_s},
        ))
    return out

def fetch_akshare_news(session, src) -> list[dict]:
    """关注列表个股新闻（按 watchlist）。"""
    out = []
    try:
        import akshare as ak
        watch = WATCHLIST
    except Exception as e:
        log.warning("akshare news init failed: %s", e)
        return out
    for code in watch:
        try:
            df = ak.stock_news_em(symbol=code)
        except Exception as e:
            log.warning("akshare news %s failed: %s", code, e)
            continue
        for _, row in df.iterrows():
            title = str(row.get("新闻标题", "")).strip()
            url = str(row.get("新闻链接", "")).strip()
            src_name = str(row.get("文章来源", "")).strip()
            pub = str(row.get("发布时间", "")).strip()
            ts = _ak_dt_now()
            try:
                ts = datetime.strptime(pub, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                pass
            if not title:
                continue
            full_title = f"[{code}] {title}"
            out.append(_mk_item(
                full_title, url or f"https://so.eastmoney.com/news/s?keyword={code}",
                ts, src, summary=f"来源:{src_name}",
                raw={"code": code, "source_name": src_name},
            ))
        time.sleep(0.3)
    return out
