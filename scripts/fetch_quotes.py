#!/usr/bin/env python3
"""
Stock Radar 行情数据抓取。

从 akshare / yfinance 拉取 A 股 / 港股 / 美股的实时行情，
输出 data/latest-quotes.json，供前端展示。

用法：
  .venv/bin/python scripts/fetch_quotes.py [--codes 600519,00700,NVDA] [--output-dir data]

如果 --codes 未指定，默认抓取 WATCHLIST 中所有股票。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock_radar.quotes")

# ── 默认关注股 ──
DEFAULT_CODES = [
    # A 股
    "600519", "000001", "000002", "600036", "601318", "300750", "688981",
    "002594", "000858", "000333",
    # 港股
    "00700", "09988", "01810", "03690", "02318",
    # 美股
    "NVDA", "AAPL", "MSFT", "TSLA", "GOOGL", "AMZN", "META",
]

# ── 市场代码前缀 ──
US_STOCKS = {"NVDA", "AAPL", "MSFT", "TSLA", "GOOGL", "GOOG", "AMZN", "META",
             "JPM", "BRK.B", "BRK.A", "AMD", "INTC", "CRM", "ADBE", "NFLX"}


def fetch_a_spot(session: requests.Session, codes: list[str]) -> dict[str, dict]:
    """通过东方财富行情 API 批量获取 A 股实时数据。

    Eastmoney API: https://push2.eastmoney.com/api/qt/ulist.np/get
    """
    a_codes = [c for c in codes if c.isdigit() and len(c) == 6]
    if not a_codes:
        return {}

    # 东方财富格式：1.股票代码 (1 = 上交所, 0 = 深交所)
    secids = []
    for c in a_codes:
        prefix = "1." if c.startswith(("6", "9")) else "0."
        secids.append(prefix + c)

    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?fltt=2&invt=2&fields=f2,f3,f12,f14"
        f"&secids={','.join(secids)}"
    )
    try:
        r = session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("eastmoney API failed: %s", e)
        return {}

    result = {}
    for item in data.get("data", {}).get("diff", []):
        code = str(item.get("f12", "")).strip()
        price = item.get("f2")
        change_pct = item.get("f3")
        name = item.get("f14", "")
        if code and price is not None:
            result[code] = {
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "market": "cn",
            }
    log.info("A quotes: %d/%d fetched", len(result), len(a_codes))
    return result


def fetch_hk_spot(session: requests.Session, codes: list[str]) -> dict[str, dict]:
    """通过新浪财经 API 获取港股实时数据。

    API: https://hq.sinajs.cn/list=hk00700,hk09988,...
    """
    hk_codes = [c for c in codes if c.isdigit() and len(c) == 5]
    if not hk_codes:
        return {}

    hk_params = ",".join(f"hk{c}" for c in hk_codes)
    url = f"https://hq.sinajs.cn/list={hk_params}"
    try:
        headers = {"Referer": "https://finance.sina.com.cn"}
        r = session.get(url, timeout=10, headers=headers)
        r.encoding = "gbk"
        lines = r.text.strip().split("\n")
    except Exception as e:
        log.warning("sinajs HK API failed: %s", e)
        return {}

    result = {}
    for line in lines:
        # 格式: var hq_str_hk00700="腾讯控股,380.000,-0.500,-0.13%..."
        if not line.startswith("var hq_str_hk"):
            continue
        try:
            code = line.split("hk")[1].split('"')[0].strip()
            parts = line.split('"')[1].split(",")
            name = parts[0]
            price = float(parts[1]) if parts[1] else None
            change_pct_str = parts[3].replace("%", "").strip()
            change_pct = float(change_pct_str) if change_pct_str and change_pct_str != "--" else None
            if price is not None:
                result[code] = {
                    "name": name,
                    "price": price,
                    "change_pct": change_pct,
                    "market": "hk",
                }
        except (IndexError, ValueError) as e:
            log.debug("parse HK line failed: %s | %s", line[:50], e)

    log.info("HK quotes: %d/%d fetched", len(result), len(hk_codes))
    return result


def fetch_us_spot(session: requests.Session, codes: list[str]) -> dict[str, dict]:
    """通过 Yahoo Finance (yfapi.net) 或直接请求 API 获取美股数据。

    免费方案：使用 Yahoo Finance 的 download/quote API。
    """
    us_codes = [c for c in codes if c in US_STOCKS or (c.isalpha() and len(c) <= 5)]
    if not us_codes:
        return {}

    # Yahoo Finance v7 quote API
    symbols = ",".join(us_codes)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        r = session.get(url, timeout=10, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("Yahoo Finance API failed: %s", e)
        return {}

    result = {}
    for quote in data.get("quoteResponse", {}).get("result", []):
        symbol = quote.get("symbol", "")
        price = quote.get("regularMarketPrice")
        prev_close = quote.get("regularMarketPreviousClose")
        change_pct = None
        if price is not None and prev_close and prev_close > 0:
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        if symbol and price is not None:
            result[symbol] = {
                "name": quote.get("shortName") or quote.get("longName") or symbol,
                "price": price,
                "change_pct": change_pct,
                "market": "us",
            }

    log.info("US quotes: %d/%d fetched", len(result), len(us_codes))
    return result


def fetch_quotes(codes: list[str] | None = None) -> dict[str, dict]:
    """主入口：拉取所有股票的行情。"""
    if codes is None:
        codes = DEFAULT_CODES

    codes = [c.strip().upper() for c in codes if c.strip()]
    codes = list(dict.fromkeys(codes))  # dedup, preserve order

    log.info("fetching quotes for %d codes: %s", len(codes), codes[:10])

    session = requests.Session()
    session.headers.update({"User-Agent": "Stock-Radar/0.2"})

    quotes = {}
    quotes.update(fetch_a_spot(session, codes))
    quotes.update(fetch_hk_spot(session, codes))
    quotes.update(fetch_us_spot(session, codes))

    return quotes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔的股票代码")
    ap.add_argument("--output-dir", default="data")
    args = ap.parse_args()

    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    quotes = fetch_quotes(codes)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(quotes),
        "quotes": quotes,
    }
    path = out_dir / "latest-quotes.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s (%d quotes)", path, len(quotes))


if __name__ == "__main__":
    main()
