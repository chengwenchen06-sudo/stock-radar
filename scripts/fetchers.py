"""
stock-radar fetchers.

每个 fetcher 接口统一：fetch_<name>(session, now) -> list[RawItem]
RawItem = dict(title, url, published_at, source, source_tier_rank, market, raw)

信源分层 source_tier_rank：
  0 = 官方一手（交易所/公司公告/监管）
  1 = 主流财经媒体（财新/路透/华尔街日报等）
  2 = 二线财经（AAStocks/东方财富等）
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

UA = "Stock-Radar/0.1 (+https://github.com/LearnPrompt/stock-radar)"

# ---------- 内置源定义（fetch_xxx 通过这里的元数据共享） ----------

BUILTIN_SOURCES = [
    # 财经媒体 RSS
    {"id": "caixin",        "name": "财新网",      "market": "cn",     "tier": 1, "kind": "rss",  "url": "https://www.caixin.com/feed/"},
    {"id": "wallstreetcn",  "name": "华尔街见闻",   "market": "cn",     "tier": 1, "kind": "rss",  "url": "https://wallstreetcn.com/rss"},
    {"id": "stcn",          "name": "证券时报",     "market": "cn",     "tier": 1, "kind": "rss",  "url": "https://www.stcn.com/rss/stcn.xml"},
    {"id": "reuters_biz",   "name": "Reuters Biz", "market": "global", "tier": 1, "kind": "rss",  "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"id": "marketwatch",   "name": "MarketWatch",  "market": "us",     "tier": 1, "kind": "rss",  "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"id": "cnbc",          "name": "CNBC",         "market": "us",     "tier": 1, "kind": "rss",  "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"id": "aastocks",      "name": "AAStocks",     "market": "hk",     "tier": 2, "kind": "rss",  "url": "https://www.aastocks.com/sc/stocks/analysis/company-fundamental/news/rss.aspx"},
    # 官方一手：交易所/监管
    {"id": "cninfo",        "name": "巨潮资讯",     "market": "cn",     "tier": 0, "kind": "json", "url": "http://www.cninfo.com.cn/new/hisAnnouncement/query"},
    {"id": "sec_edgar_8k",  "name": "SEC EDGAR 8-K","market": "us",     "tier": 0, "kind": "json", "url": "https://data.sec.gov/submissions/CIK0000320193.json"},
    {"id": "hkexnews",      "name": "港交所披露",   "market": "hk",     "tier": 0, "kind": "json", "url": "https://www1.hkexnews.hk/search/titleSearchServlet.do"},
]

# ---------- 通用 RSS fetcher ----------

def _parse_rss(url: str, source_name: str, market: str, tier: int, limit: int = 30) -> list[dict]:
    out = []
    try:
        d = feedparser.parse(url, agent=UA)
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
        out.append({
            "title": title,
            "url": link,
            "published_at": ts.isoformat(),
            "source": source_name,
            "source_id": source_name,
            "source_tier_rank": tier,
            "market": market,
            "summary": (e.get("summary") or "")[:500],
            "raw": {},
        })
    return out

def fetch_rss_source(session: requests.Session, src: dict) -> list[dict]:
    return _parse_rss(src["url"], src["name"], src["market"], src["tier"])

# ---------- 巨潮资讯 (A股公告) ----------

def fetch_cninfo(session: requests.Session, src: dict) -> list[dict]:
    """巨潮历史公告查询。注意：商业站点，无鉴权但有频控，建议每分钟 ≤ 10 次。"""
    out = []
    # 抓最近 1 天
    end = datetime.now()
    start = end - timedelta(days=1)
    payload = {
        "stock": "",
        "tabName": "fulltext",
        "pageSize": 30,
        "pageNum": 1,
        "column": "sse",
        "category": "category_ndbg_szsh;category_yjdbg_szsh;category_zf_szsh;category_gqbd_szsh",
        "plate": "",
        "seDate": f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}",
        "searchkey": "",
        "secid": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {"User-Agent": UA, "Referer": "http://www.cninfo.com.cn/"}
    try:
        r = session.post(src["url"], data=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("cninfo failed: %s", e)
        return out
    for a in data.get("announcements") or []:
        title = (a.get("announcementTitle") or "").strip()
        if not title:
            continue
        # 去掉标题中的 HTML 标签
        title = re.sub(r"<[^>]+>", "", title)
        pdf_url = a.get("adjunctUrl") or ""
        if pdf_url and not pdf_url.startswith("http"):
            pdf_url = "http://static.cninfo.com.cn/" + pdf_url
        # cninfo 用 announcementId / announcementTime（毫秒时间戳）
        ts_ms = a.get("announcementTime") or 0
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
        sec_code = a.get("secCode") or ""
        sec_name = a.get("secName") or ""
        full_title = f"[{sec_code} {sec_name}] {title}" if sec_code else title
        out.append({
            "title": full_title,
            "url": pdf_url or src["url"],
            "published_at": ts.isoformat(),
            "source": src["name"],
            "source_id": src["id"],
            "source_tier_rank": src["tier"],
            "market": src["market"],
            "summary": a.get("announcementContent") or "",
            "raw": {"sec_code": sec_code, "sec_name": sec_name},
        })
    return out

# ---------- SEC EDGAR (美股 8-K) ----------

# 演示用 CIK 表（AAPL/MSFT/NVDA/TSLA/GOOG/AMZN/META/BRK.B/JPM）
DEFAULT_CIKS = [
    ("0000320193", "AAPL"),
    ("0000789019", "MSFT"),
    ("0001045810", "NVDA"),
    ("0001318605", "TSLA"),
    ("0001652044", "GOOGL"),
    ("0001018724", "AMZN"),
    ("0001326801", "META"),
    ("0001067983", "BRK.B"),
    ("0000019617", "JPM"),
]

def fetch_sec_edgar(session: requests.Session, src: dict, ciks: list[tuple[str, str]] | None = None) -> list[dict]:
    """抓 SEC submissions，按 8-K 过滤最近 24h。注意 SEC 限流 10 req/s，需带 UA。"""
    out = []
    ciks = ciks or DEFAULT_CIKS
    for cik, ticker in ciks:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        headers = {"User-Agent": UA}
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
            if form not in ("8-K", "10-Q", "10-K", "4", "13F-HR"):
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
            out.append({
                "title": title,
                "url": filing_url,
                "published_at": ts.isoformat(),
                "source": src["name"],
                "source_id": src["id"],
                "source_tier_rank": src["tier"],
                "market": src["market"],
                "summary": f"{ticker} 于 {date} 提交 {form}",
                "raw": {"ticker": ticker, "form": form, "accession": acc},
            })
        time.sleep(0.15)  # SEC 礼貌节流
    return out

# ---------- 港交所披露 ----------

def fetch_hkexnews(session: requests.Session, src: dict) -> list[dict]:
    """港交所披露易：titleSearchServlet 返回标题列表。轻量抓取，仅用于 demo。"""
    out = []
    end = datetime.now()
    start = end - timedelta(days=1)
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": "",
        "documentType": "-1",
        "fromDate": start.strftime("%Y%m%d"),
        "toDate": end.strftime("%Y%m%d"),
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": 30,
        "lang": "ZH",
    }
    headers = {"User-Agent": UA, "Referer": "https://www1.hkexnews.hk/"}
    try:
        r = session.get(src["url"], params=params, headers=headers, timeout=15)
        r.raise_for_status()
        # 接口返回的是 JSON 字符串（带 Result 字段），需要再解一层
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
        # 日期格式 YYYYMMDD
        date_str = it.get("DATE_TIME") or it.get("dateTime") or ""
        ts = datetime.now(timezone.utc)
        try:
            ts = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({
            "title": title,
            "url": doc_link,
            "published_at": ts.isoformat(),
            "source": src["name"],
            "source_id": src["id"],
            "source_tier_rank": src["tier"],
            "market": src["market"],
            "summary": "",
            "raw": it,
        })
    return out

# ---------- OPML 私有源（用户自加 RSS） ----------

def load_opml(path: str) -> list[dict]:
    """解析 OPML，转成 src 列表（kind=rss）。"""
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
        market = "global"
        # 简单启发：根据 title 推断市场
        if any(k in title for k in ["财新", "华尔街见闻", "证券", "巨潮", "新浪财经", "东方财富"]):
            market = "cn"
        elif any(k in title for k in ["Reuters", "WSJ", "CNBC", "Bloomberg", "MarketWatch", "Yahoo", "Seeking"]):
            market = "global"
        elif any(k in title for k in ["HKEX", "AAStocks", "港交所"]):
            market = "hk"
        srcs.append({"id": title, "name": title, "market": market, "tier": 3, "kind": "rss", "url": xml_url})
    return srcs

def fetch_opml_rss(session: requests.Session, opml_path: str) -> list[dict]:
    srcs = load_opml(opml_path)
    out = []
    for s in srcs:
        out.extend(fetch_rss_source(session, s))
    return out