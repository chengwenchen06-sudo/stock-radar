"""
Stock Radar 通知模块。

在数据更新完成后，自动将关注股的高重要性信号推送到指定渠道。

支持的渠道（通过环境变量配置）：
  - 企业微信机器人 webhook (WECHAT_WEBHOOK_URL)
  - Telegram Bot (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
  - Bark (iOS) (BARK_URL)

通用控制：
  NOTIFY_ENABLED=1         开启推送
  NOTIFY_MIN_SCORE=70      最低重要性分数（默认 70，即 high）

用法：
  from notify import push_alerts
  push_alerts(items, watchlist=["600519","NVDA"])
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any

import requests

log = logging.getLogger("stock_radar.notify")

# ── 渠道配置（从环境变量读取） ──────────────────────────

def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

NOTIFY_ENABLED = _env_str("NOTIFY_ENABLED", "").lower() in ("1", "true", "yes")
MIN_SCORE = _env_int("NOTIFY_MIN_SCORE", 70)

CHANNELS: dict[str, dict[str, Any]] = {}

if _env_str("WECHAT_WEBHOOK_URL"):
    CHANNELS["wechat"] = {"url": _env_str("WECHAT_WEBHOOK_URL")}

if _env_str("TELEGRAM_BOT_TOKEN") and _env_str("TELEGRAM_CHAT_ID"):
    CHANNELS["telegram"] = {
        "token": _env_str("TELEGRAM_BOT_TOKEN"),
        "chat_id": _env_str("TELEGRAM_CHAT_ID"),
    }

if _env_str("BARK_URL"):
    CHANNELS["bark"] = {"url": _env_str("BARK_URL")}


# ── 提取信号 ──────────────────────────────────

def _extract_code(item: dict) -> str:
    """从 item 中提取股票代码。"""
    # 优先 raw 字段
    raw = item.get("raw") or {}
    for k in ("code", "sec_code", "stock_code"):
        v = raw.get(k)
        if v:
            return v.strip().upper()
    # 从标题 [xxxxxx] 提取
    import re
    m = re.search(r"[\[【\(]\s*([0-9]{5,6}|[A-Z]{1,5}(\.[A-Z])?)\s*[\】\]\)]", item.get("title", ""))
    if m:
        return m.group(1).upper()
    return ""


def _market_emoji(market: str) -> str:
    return {"cn": "🇨🇳", "hk": "🇭🇰", "us": "🇺🇸", "global": "🌍"}.get(market, "📌")


def _importance_emoji(label: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(label, "⚪")


def find_alerts(
    items: list[dict],
    watchlist: list[str],
    min_score: int = MIN_SCORE,
) -> list[dict]:
    """从 items 中筛选出关注股的高重要性信号。

    返回按 (代码, importance_score desc) 排序的信号列表。
    """
    codes_set = {c.strip().upper() for c in watchlist if c.strip()}
    if not codes_set:
        return []

    alerts = []
    for it in items:
        score = it.get("importance_score", 0)
        if score < min_score:
            continue
        code = _extract_code(it)
        if code and code in codes_set:
            alerts.append(it)

    # 先按代码分组，组内按分数降序
    alerts.sort(key=lambda x: (x.get("importance_score", 0) or 0), reverse=True)
    return alerts


# ── 各渠道发送 ──────────────────────────────────

def _send_wechat(alerts: list[dict], watchlist: list[str]) -> bool:
    """企业微信机器人 markdown 消息。"""
    url = CHANNELS["wechat"]["url"]
    lines = [
        f"## 📡 Stock Radar 信号提醒\n",
        f"> 监测 {len(watchlist)} 只关注股 · {datetime.now().strftime('%H:%M')}\n",
    ]
    for it in alerts:
        mkt = _market_emoji(it.get("market", ""))
        imp = _importance_emoji(it.get("importance_label", ""))
        title = it.get("title", "")
        score = it.get("importance_score", 0)
        source = it.get("source", "")
        lines.append(f"- {imp} {mkt} **{title}** ({score}分 · {source})")
    lines.append(f"\n> 共 {len(alerts)} 条信号 · [查看详情](https://chengwenchen06-sudo.github.io/stock-radar)")

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": "\n".join(lines)},
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("wechat push OK (%d alerts)", len(alerts))
        return True
    except Exception as e:
        log.error("wechat push failed: %s", e)
        return False


def _send_telegram(alerts: list[dict], watchlist: list[str]) -> bool:
    """Telegram Bot 消息。"""
    token = CHANNELS["telegram"]["token"]
    chat_id = CHANNELS["telegram"]["chat_id"]

    lines = [f"📡 *Stock Radar 信号提醒*  ({len(alerts)} 条)\n"]
    for it in alerts:
        mkt = _market_emoji(it.get("market", ""))
        imp = _importance_emoji(it.get("importance_label", ""))
        title = it.get("title", "")
        score = it.get("importance_score", 0)
        source = it.get("source", "")
        lines.append(f"{imp} {mkt} *{title}*")
        lines.append(f"   {score}分 · {source}")
        lines.append("")

    if len(lines) > 100:
        # Telegram 单条消息有限制，截断
        lines = lines[:80]
        lines.append("… (已截断，查看完整列表请访问网站)")

    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        log.info("telegram push OK (%d alerts)", len(alerts))
        return True
    except Exception as e:
        log.error("telegram push failed: %s", e)
        return False


def _send_bark(alerts: list[dict], watchlist: list[str]) -> bool:
    """Bark (iOS) 推送。"""
    url = CHANNELS["bark"]["url"].rstrip("/")
    # Bark 要求 title + body 简短
    top = alerts[:5]
    lines = []
    for it in top:
        code = _extract_code(it)
        title = it.get("title", "")
        imp = it.get("importance_label", "")
        score = it.get("importance_score", 0)
        short_title = (title[:80] + "…") if len(title) > 80 else title
        lines.append(f"[{imp}][{code}] {short_title}")
    if len(alerts) > 5:
        lines.append(f"……等共 {len(alerts)} 条")

    body = "\n".join(lines) if lines else "无新信号"
    try:
        r = requests.get(
            f"{url}/📡StockRadar/{body}?sound=minuet&group=StockRadar",
            timeout=10,
        )
        r.raise_for_status()
        log.info("bark push OK (%d alerts)", len(alerts))
        return True
    except Exception as e:
        log.error("bark push failed: %s", e)
        return False


# ── 统一入口 ──────────────────────────────────

SENDERS = {"wechat": _send_wechat, "telegram": _send_telegram, "bark": _send_bark}


def push_alerts(
    items: list[dict],
    watchlist: list[str] | None = None,
    min_score: int | None = None,
) -> dict[str, Any]:
    """主入口：找出关注股信号并推送到所有已配置的渠道。

    Args:
        items: 数据条目列表（latest-24h.json 的 items）
        watchlist: 关注股代码列表，None 时从环境变量 WATCHLIST_CODES 读取（逗号分隔）
        min_score: 最低分数阈值，覆盖环境变量 NOTIFY_MIN_SCORE 和默认值 70

    Returns:
        {channel: bool} 各渠道的发送成功/失败状态
    """
    if not NOTIFY_ENABLED:
        log.info("notify disabled (set NOTIFY_ENABLED=1)")
        return {}

    if watchlist is None:
        raw = _env_str("WATCHLIST_CODES", "")
        watchlist = [c.strip() for c in raw.split(",") if c.strip()] if raw else []

    if not watchlist:
        log.info("no watchlist codes configured, skip notification")
        return {}

    if min_score is None:
        min_score = MIN_SCORE

    alerts = find_alerts(items, watchlist, min_score)
    if not alerts:
        log.info("no high-importance alerts for watchlist, skip notification")
        return {}

    log.info("found %d alerts for watchlist %s", len(alerts), watchlist)

    results = {}
    for ch in CHANNELS:
        sender = SENDERS.get(ch)
        if sender:
            try:
                results[ch] = sender(alerts, watchlist)
            except Exception as e:
                log.exception("sender %s crashed", ch)
                results[ch] = False

    return results


def push_failure_notification(error_msg: str) -> None:
    """数据更新失败时发送告警。"""
    for ch_name, ch_conf in CHANNELS.items():
        if ch_name == "telegram":
            token = ch_conf["token"]
            chat_id = ch_conf["chat_id"]
            text = f"⚠️ *Stock Radar 数据更新失败*\n\n{error_msg}"
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10,
                )
            except Exception as e:
                log.error("telegram failure notification failed: %s", e)
        elif ch_name == "wechat":
            url = ch_conf["url"]
            try:
                requests.post(url, json={
                    "msgtype": "markdown",
                    "markdown": {
                        "content": f"## ⚠️ Stock Radar 数据更新失败\n> {error_msg}\n> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    },
                }, timeout=10)
            except Exception as e:
                log.error("wechat failure notification failed: %s", e)


if __name__ == "__main__":
    # 独立测试：从 data/latest-24h.json 读取数据并推送
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    data_path = os.path.join(os.path.dirname(__file__), "..", "data", "latest-24h.json")
    try:
        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error("load data failed: %s", e)
        sys.exit(1)

    items = data.get("items", [])
    result = push_alerts(items)
    print(json.dumps(result, ensure_ascii=False, indent=2))
