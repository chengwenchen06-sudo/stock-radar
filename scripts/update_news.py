#!/usr/bin/env python3
"""
Stock Radar 主流程：
  1. 抓内置源（财经媒体RSS + 巨潮 + SEC EDGAR + 港交所）
  2. 抓私有 OPML RSS（若提供）
  3. 过滤 24h 窗口，去重
  4. 分类 + 重要性打分
  5. 输出：
       data/latest-24h.json   （默认主入口，含 stock-relevant 条目）
       data/latest-24h-all.json（含全部）
       data/source-status.json
       data/daily-brief.json  （精选 top N）
       data/stories-merged.json
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import requests

sys.path.insert(0, str(Path(__file__).parent))
import fetchers
import classify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock_radar")

WINDOW_HOURS_DEFAULT = 24


def _hash_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def collect_all(session: requests.Session, opml_path: str | None, window_hours: int) -> tuple[list[dict], dict]:
    """返回 (items, source_status)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    tasks = []
    for src in fetchers.BUILTIN_SOURCES:
        if src["id"] == "cninfo":
            tasks.append(("cninfo", src, fetchers.fetch_cninfo))
        elif src["id"] == "sec_edgar_8k":
            tasks.append(("sec_edgar_8k", src, fetchers.fetch_sec_edgar))
        elif src["id"] == "hkexnews":
            tasks.append(("hkexnews", src, fetchers.fetch_hkexnews))
        elif src["kind"] == "rss":
            tasks.append((src["id"], src, fetchers.fetch_rss_source))

    if opml_path and Path(opml_path).exists():
        tasks.append(("opml", {"id": "opml", "name": "私有OPML", "market": "global", "tier": 3, "url": opml_path},
                      lambda s, _: fetchers.fetch_opml_rss(session, opml_path)))

    all_items: list[dict] = []
    status: dict[str, dict] = {}

    for sid, src, fn in tasks:
        t0 = datetime.now()
        try:
            items = fn(session, src)
            ok = True
            err = ""
        except Exception as e:
            log.exception("fetcher %s crashed", sid)
            items = []
            ok = False
            err = str(e)
        elapsed = (datetime.now() - t0).total_seconds()
        status[sid] = {
            "site_id": sid,
            "site_name": src.get("name", sid),
            "ok": ok,
            "error": err,
            "item_count": len(items),
            "elapsed_seconds": round(elapsed, 2),
        }
        log.info("fetcher %-20s %s %d items in %.1fs", sid, "OK" if ok else "FAIL", len(items), elapsed)
        all_items.extend(items)

    # 过滤窗口 + 去重
    seen = set()
    filtered = []
    for it in all_items:
        try:
            ts = datetime.fromisoformat(it["published_at"].replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        h = _hash_id(it["url"])
        if h in seen:
            continue
        seen.add(h)
        # 打分类 + 重要性
        label = classify.classify(it.get("title", ""), it.get("summary", ""))
        imp_label, imp_score = classify.importance(it)
        it.update({
            "id": h,
            "label": label,
            "label_zh": classify.LABEL_ZH.get(label, label),
            "market_zh": classify.MARKET_ZH.get(it.get("market", "global"), "全球"),
            "importance_label": imp_label,
            "importance_score": imp_score,
        })
        filtered.append(it)

    return filtered, status


def merge_stories(items: list[dict]) -> list[dict]:
    """把同一事件（标题相似）合并成故事线。简易实现：按 label + 前 6 个汉字做 key。"""
    from collections import defaultdict
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        # 去掉股票代码前缀：[000001 XXX]
        t = it["title"]
        t = t.replace("[", "").replace("]", "")
        # 取 label + 头 8 个有效字符（去标点）
        import re
        head = re.sub(r"\s+", "", t)[:8]
        key = (it["label"], head)
        buckets[key].append(it)
    stories = []
    for (label, head), group in buckets.items():
        group.sort(key=lambda i: i["importance_score"], reverse=True)
        top = group[0]
        stories.append({
            "story_id": _hash_id(f"{label}-{head}"),
            "title": top["title"],
            "label": label,
            "label_zh": top["label_zh"],
            "importance_label": top["importance_label"],
            "importance_score": max(i["importance_score"] for i in group),
            "source_count": len(group),
            "primary_url": top["url"],
            "markets": sorted({i["market"] for i in group}),
            "items": [
                {"source": i["source"], "url": i["url"], "published_at": i["published_at"], "importance_score": i["importance_score"]}
                for i in group[:5]
            ],
        })
    stories.sort(key=lambda s: -s["importance_score"])
    return stories


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="data")
    ap.add_argument("--window-hours", type=int, default=WINDOW_HOURS_DEFAULT)
    ap.add_argument("--rss-opml", default=None)
    ap.add_argument("--skip-network", action="store_true",
                    help="跳过实际抓取，仅用本地 demo 数据（用于本地冒烟测试）")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": fetchers.UA})

    if args.skip_network:
        log.info("skip-network mode: loading demo items")
        items, status = load_demo()
    else:
        items, status = collect_all(session, args.rss_opml, args.window_hours)

    # 排序 + 写文件
    def _ts(i):
        try:
            return datetime.fromisoformat(i["published_at"].replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda i: (i["source_tier_rank"], -i["importance_score"], -_ts(i).timestamp()))
    generated_at = datetime.now(timezone.utc).isoformat()

    def _save(name: str, payload: dict):
        path = out_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("wrote %s (%d KB)", path, path.stat().st_size // 1024)

    _save("latest-24h.json", {
        "generated_at": generated_at,
        "window_hours": args.window_hours,
        "total_items": len(items),
        "source_count": len(status),
        "items": items,
    })
    _save("latest-24h-all.json", {
        "generated_at": generated_at,
        "window_hours": args.window_hours,
        "total_items": len(items),
        "source_count": len(status),
        "items": items,
    })
    _save("source-status.json", {
        "generated_at": generated_at,
        "successful_sites": sum(1 for s in status.values() if s["ok"]),
        "failed_sites": sum(1 for s in status.values() if not s["ok"]),
        "zero_item_sites": sum(1 for s in status.values() if s["ok"] and s["item_count"] == 0),
        "sites": list(status.values()),
    })

    # 故事线
    stories = merge_stories(items)
    _save("stories-merged.json", {
        "generated_at": generated_at,
        "total_stories": len(stories),
        "stories": stories,
    })

    # 日报：按 importance 排序取前 20
    daily = sorted(items, key=lambda i: -i["importance_score"])[:20]
    _save("daily-brief.json", {
        "generated_at": generated_at,
        "total_items": len(daily),
        "items": daily,
    })

    log.info("done: %d items, %d stories, %d sources", len(items), len(stories), len(status))


def load_demo():
    """跳过网络时的 demo 数据，方便本地测试前端和分类逻辑。"""
    now = datetime.now(timezone.utc)
    # 先用内置源做底，但走 collect_all 路径会发请求——所以 demo 模式手填 10 条并补齐字段
    base_items: list[dict] = []
    raw = [
        {"title": "[600519 贵州茅台] 2024年三季报点评：营收同比增长15.2%，净利润增长13.1%",
         "url": "https://example.com/maotai-q3", "summary": "茅台发布三季度报告",
         "market": "cn", "source": "巨潮资讯", "source_id": "cninfo", "source_tier_rank": 0,
         "published_at": (now - timedelta(hours=3)).isoformat()},
        {"title": "[AAPL] Apple Q4 earnings beat expectations, revenue $94.9B",
         "url": "https://example.com/aapl-q4", "summary": "Apple reported quarterly results",
         "market": "us", "source": "SEC EDGAR 8-K", "source_id": "sec_edgar_8k", "source_tier_rank": 0,
         "published_at": (now - timedelta(hours=5)).isoformat()},
        {"title": "美联储12月议息会议：维持利率不变，鲍威尔暗示2026年降息节奏放缓",
         "url": "https://example.com/fed-dec", "summary": "美联储议息会议",
         "market": "us", "source": "Reuters Biz", "source_id": "reuters_biz", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=2)).isoformat()},
        {"title": "英伟达拟收购以色列AI芯片公司Mellanox剩余股份，交易金额50亿美元",
         "url": "https://example.com/nvda-acq", "summary": "并购重组",
         "market": "us", "source": "MarketWatch", "source_id": "marketwatch", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=8)).isoformat()},
        {"title": "[00700 腾讯控股] 宣布回购100亿港元股票",
         "url": "https://example.com/tencent-buyback", "summary": "回购",
         "market": "hk", "source": "港交所披露", "source_id": "hkexnews", "source_tier_rank": 0,
         "published_at": (now - timedelta(hours=6)).isoformat()},
        {"title": "证监会发布上市公司治理新规，要求独立董事比例提升至1/3",
         "url": "https://example.com/csrc-rule", "summary": "监管政策",
         "market": "cn", "source": "证券时报", "source_id": "stcn", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=10)).isoformat()},
        {"title": "特斯拉Model Y 2026款发布，起售价上调2000美元",
         "url": "https://example.com/tesla-y", "summary": "产品发布",
         "market": "us", "source": "CNBC", "source_id": "cnbc", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=12)).isoformat()},
        {"title": "中国11月CPI同比上涨0.5%，高于市场预期",
         "url": "https://example.com/cpi-nov", "summary": "宏观数据",
         "market": "cn", "source": "财新网", "source_id": "caixin", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=14)).isoformat()},
        {"title": "比亚迪11月新能源汽车销量50.6万辆，同比增长67%",
         "url": "https://example.com/byd-sales", "summary": "销量",
         "market": "cn", "source": "华尔街见闻", "source_id": "wallstreetcn", "source_tier_rank": 1,
         "published_at": (now - timedelta(hours=16)).isoformat()},
        {"title": "[02318 中国平安] 董事会审议通过50亿元H股回购方案",
         "url": "https://example.com/pingan-buyback", "summary": "回购方案",
         "market": "hk", "source": "AAStocks", "source_id": "aastocks", "source_tier_rank": 2,
         "published_at": (now - timedelta(hours=18)).isoformat()},
    ]
    # 给 demo 条目补齐 id / label / importance 等字段，保持与 collect_all 一致
    for it in raw:
        label = classify.classify(it.get("title", ""), it.get("summary", ""))
        imp_label, imp_score = classify.importance(it)
        it.update({
            "id": _hash_id(it["url"]),
            "label": label,
            "label_zh": classify.LABEL_ZH.get(label, label),
            "market_zh": classify.MARKET_ZH.get(it.get("market", "global"), "全球"),
            "importance_label": imp_label,
            "importance_score": imp_score,
        })
        base_items.append(it)

    status = {
        s["id"]: {"site_id": s["id"], "site_name": s["name"], "ok": True, "error": "",
                  "item_count": 0, "elapsed_seconds": 0.1}
        for s in fetchers.BUILTIN_SOURCES if s["kind"] == "rss"
    }
    for it in base_items:
        sid = it["source_id"]
        if sid in status:
            status[sid]["item_count"] += 1
    return base_items, status


if __name__ == "__main__":
    main()