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
    # A 股快讯（深度文章）
    {"id": "wallstcn_live",  "name": "华尔街见闻快讯",   "market": "cn",     "tier": 1, "kind": "json",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives"},
    {"id": "wallstcn_art",   "name": "华尔街见闻文章",   "market": "cn",     "tier": 1, "kind": "json",
     "url": "https://api-one.wallstcn.com/apiv1/content/articles"},
    # 港股（一手）
    {"id": "hkexnews",       "name": "港交所披露",     "market": "hk",     "tier": 0, "kind": "json",
     "url": "https://www1.hkexnews.hk/search/titleSearchServlet.do"},
    # 美股公告（一手）
    {"id": "sec_edgar_8k",   "name": "SEC EDGAR 8-K", "market": "us",     "tier": 0, "kind": "json",
     "url": "https://data.sec.gov/submissions/CIK0000320193.json"},
]

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
            # display_time 形如 "2026-06-19 11:49:06:549"
            ts_str = (it.get("display_time") or "").split(":")[0:3]
            ts_str = ":".join(ts_str)  # "2026-06-19 11:49:06"
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts < datetime.now(timezone.utc) - timedelta(hours=24):
                continue
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

# ---------- 港交所披露 ----------

def fetch_hkexnews(session: requests.Session, src: dict) -> list[dict]:
    """港交所披露易：titleSearchServlet 标题列表。"""
    out = []
    end = datetime.now()
    start = end - timedelta(days=1)
    params = {
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK",
        "stockId": "", "documentType": "-1",
        "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
        "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
        "rowRange": 30, "lang": "ZH",
    }
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www1.hkexnews.hk/"}
    try:
        r = session.get(src["url"], params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("hkexnews failed: %s", e)
        return out
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
    out = []
    ciks = ciks or DEFAULT_CIKS
    for cik, ticker in ciks:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        headers = {"User-Agent": f"Stock Radar admin@example.com (test project)"}
        try:
            r = session.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("sec edgar failed %s: %s", cik, e)
            continue
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        accs = recent.get("accessionNumber", []) or []
        primary_docs = recent.get("primaryDocument", []) or []
        for form, date, acc, doc in zip(forms, dates, accs, primary_docs):
            if form not in ("8-K", "10-Q", "10-K", "4"):
                continue
            try:
                ts = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts < datetime.now(timezone.utc) - timedelta(hours=36):
                continue
            acc_clean = acc.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc}"
            title = f"[{ticker}] {form} 公告"
            out.append(_mk_item(
                title, filing_url, ts, src,
                summary=f"{ticker} 于 {date} 提交 {form}",
                raw={"ticker": ticker, "form": form, "accession": acc},
            ))
        time.sleep(0.12)
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
