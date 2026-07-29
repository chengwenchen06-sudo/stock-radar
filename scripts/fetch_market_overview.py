#!/usr/bin/env python3
"""Fetch market overview: indices, breadth, and sector leaders using real APIs.

Outputs data/market-overview.json for the hero card on index.html.

Sources:
  - A-share indices + sectors: AkShare (东方财富)
  - HK index: Yahoo Finance v7 quote API
  - US indices: Yahoo Finance v7 quote API
  - Limit up/down: AkShare 涨停/跌停池
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("market_overview")

INDICES = [
    # (akshare code, display name, market) — None means skip akshare
    ("上证指数", "上证指数", "cn"),
    ("深证成指", "深证成指", "cn"),
    ("创业板指", "创业板指", "cn"),
    ("沪深300", "沪深300", "cn"),
    ("科创50", "科创50", "cn"),
]

# US/HK indices via Yahoo Finance
YAHOO_INDICES = [
    ("^HSI", "恒生指数", "hk"),
    ("^SPX", "标普500", "us"),
    ("^DJI", "道琼斯", "us"),
    ("^IXIC", "纳斯达克", "us"),
]


def _col_name(cols: list[str], *candidates: str) -> str | None:
    """从 DataFrame columns 中找出第一个匹配的列名。"""
    for c in candidates:
        if c in cols:
            return c
    return None


def fetch_akshare_indices() -> list[dict]:
    """通过 AkShare 获取 A 股主要指数实时行情。"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_em()
    except Exception as e:
        log.warning("akshare indices failed: %s", e)
        return []

    cols = list(df.columns)
    name_col = _col_name(cols, "index_name", "名称")
    code_col = _col_name(cols, "index_code", "代码")
    price_col = _col_name(cols, "最新价", "最新价")
    change_col = _col_name(cols, "涨跌幅", "涨跌幅")
    if not name_col or not price_col or not change_col:
        log.warning("akshare indices: unknown columns %s", cols)
        return []

    name_map = {row[name_col]: row for _, row in df.iterrows()}
    results = []
    for name, display, market in INDICES:
        row = name_map.get(name)
        if row is None:
            continue
        try:
            price = float(row.get(price_col, 0))
            change_pct = float(row.get(change_col, 0))
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        results.append({
            "code": row.get(code_col, name) if code_col else name,
            "name": display,
            "price": price,
            "change_pct": round(change_pct, 2),
            "market": market,
        })
    log.info("akshare indices: %d fetched", len(results))
    return results


def fetch_yahoo_indices() -> list[dict]:
    """通过 Yahoo Finance v7 quote API 获取港股/美股指数（含重试）。"""
    symbols = [s[0] for s in YAHOO_INDICES]
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(symbols)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    data = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                log.warning("yahoo 429, retrying in %ds (attempt %d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            log.warning("yahoo indices attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
                continue
            return []

    if data is None:
        return []

    quote_map = {}
    for q in data.get("quoteResponse", {}).get("result", []):
        quote_map[q.get("symbol", "")] = q

    results = []
    for sym, display, market in YAHOO_INDICES:
        q = quote_map.get(sym)
        if q is None:
            continue
        price = q.get("regularMarketPrice")
        prev_close = q.get("regularMarketPreviousClose")
        if price is None or price <= 0:
            continue
        change_pct = 0.0
        if prev_close and prev_close > 0:
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        results.append({
            "code": sym,
            "name": display,
            "price": price,
            "change_pct": change_pct,
            "market": market,
        })
    log.info("yahoo indices: %d fetched", len(results))
    return results


def fetch_akshare_sectors(top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """通过 AkShare 获取 A 股行业板块涨跌榜。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
    except Exception as e:
        log.warning("akshare sectors failed: %s", e)
        return [], []

    cols = list(df.columns)
    name_col = _col_name(cols, "板块名称", "名称")
    change_col = _col_name(cols, "涨跌幅", "涨跌幅")
    if not name_col or not change_col:
        log.warning("akshare sectors: unknown columns %s", cols)
        return [], []

    sectors = []
    for _, row in df.iterrows():
        try:
            change = float(row.get(change_col, 0))
            name = str(row.get(name_col, "")).strip()
        except (ValueError, TypeError):
            continue
        if not name:
            continue
        sectors.append({"name": name, "change_pct": round(change, 2)})

    sectors.sort(key=lambda x: x["change_pct"], reverse=True)
    top = sectors[:top_n]
    bottom = sectors[-top_n:] if len(sectors) >= top_n else []
    # bottom should be ascending (worst first)
    bottom.reverse()
    log.info("akshare sectors: top=%s bottom=%s",
             [s["name"] for s in top], [s["name"] for s in bottom])
    return top, bottom


def fetch_limit_pool() -> tuple[int, int]:
    """通过 AkShare 获取当日涨停/跌停数量。"""
    limit_up = 0
    limit_down = 0
    try:
        import akshare as ak
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        zt_df = ak.stock_zt_pool_em(date=today)
        limit_up = len(zt_df) if zt_df is not None else 0
    except Exception as e:
        log.debug("akshare zt pool failed: %s", e)
    try:
        import akshare as ak
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        dt_df = ak.stock_zt_pool_dtgc_em(date=today)
        limit_down = len(dt_df) if dt_df is not None else 0
    except Exception as e:
        log.debug("akshare dt pool failed: %s", e)
    return limit_up, limit_down


def build_overview() -> dict:
    cn_indices = fetch_akshare_indices()
    us_hk_indices = fetch_yahoo_indices()
    indices = cn_indices + us_hk_indices
    sectors_top, sectors_bottom = fetch_akshare_sectors()
    limit_up, limit_down = fetch_limit_pool()

    status = "ok" if indices else "failed"

    # Give a rough breadth estimate from A-share index direction
    breadth = {}
    cn_change = 0.0
    for idx in cn_indices:
        if idx["name"] == "上证指数":
            cn_change = idx["change_pct"]
            break
    # Rough heuristic: count all A-share stocks ~5000, direction follows index
    if cn_indices:
        est_total = 5000
        if cn_change > 1:
            est_up = int(est_total * 0.65)
            est_down = int(est_total * 0.20)
        elif cn_change > 0:
            est_up = int(est_total * 0.55)
            est_down = int(est_total * 0.30)
        elif cn_change > -1:
            est_up = int(est_total * 0.30)
            est_down = int(est_total * 0.55)
        else:
            est_up = int(est_total * 0.20)
            est_down = int(est_total * 0.65)
        est_flat = est_total - est_up - est_down
        breadth = {
            "total": est_total,
            "up": est_up,
            "down": est_down,
            "flat": est_flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "note": "estimated breadth from index direction; limit up/down from AkShare",
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "breadth": breadth,
        "sectors_top": sectors_top,
        "sectors_bottom": sectors_bottom,
        "status": status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch market overview.")
    ap.add_argument("--output-dir", default="data")
    args = ap.parse_args()

    overview = build_overview()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "market-overview.json"
    out_path.write_text(
        json.dumps(overview, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("wrote %s (status=%s, %d indices)", out_path, overview["status"], len(overview["indices"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
