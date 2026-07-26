#!/usr/bin/env python3
"""Stock Radar local preview server.

Run from the project root:
    python3 scripts/serve.py

Then open the printed URL in your browser. Press Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import json
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    """多线程 HTTPServer —— 避免 Chrome/Safari 的 keep-alive 长连接占住
    单线程 server 的唯一 worker,导致后续请求永远排队等待。"""
    daemon_threads = True
    allow_reuse_address = True


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DATA_DIR = ROOT / "data"
DATA_SENTINEL = DATA_DIR / "stories-merged.json"

# 把 scripts/ 加进 sys.path,这样可以直接 import 兄弟模块 fetch_quotes
# (代替 subprocess 调脚本,省 ~500ms Python 启动 + 复用 requests.Session)
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    from fetch_quotes import fetch_quotes as _fetch_quotes  # type: ignore[import-not-found]
    _FETCH_QUOTES_OK = True
except Exception as _exc:  # ImportError 或依赖缺失都不致命 —— /api/quote 会降级返回 ok:false
    _fetch_quotes = None
    _FETCH_QUOTES_OK = False
    print(f"  ⚠️  fetch_quotes 不可用,/api/quote 会直接返回 error: {_exc}", file=sys.stderr)

# 限流 + 超时:每个 /api/quote 请求至多 8s,避免某个上游 API 卡死拖死线程
_QUOTE_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_QUOTE_TIMEOUT_S = 8


def ensure_demo_data() -> None:
    """Generate demo data on first run so the page is not blank.

    Only fires if no real data exists yet. Uses --skip-network so it works
    even without internet access.
    """
    if DATA_SENTINEL.exists() and DATA_SENTINEL.stat().st_size > 100:
        return
    print("  首次启动，生成 demo 数据……")
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "update_news.py"),
             "--skip-network", "--output-dir", str(DATA_DIR)],
            check=True,
            cwd=str(ROOT),
        )
        print("  demo 数据生成完成。\n")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  demo 数据生成失败 ({exc}); 页面可能为空，按 Ctrl+C 停。\n",
              file=sys.stderr)

    # 也生成大盘 demo 数据（不需要网络）
    mo_path = DATA_DIR / "market-overview.json"
    if not mo_path.exists():
        try:
            import random
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            demo_mo = {
                "generated_at": now,
                "status": "demo",
                "indices": [
                    {"code": "000001.SS", "name": "上证指数", "price": 3200 + random.randint(-50, 50), "change_pct": round(random.uniform(-1.5, 1.5), 2), "market": "cn"},
                    {"code": "399001.SZ", "name": "深证成指", "price": 10500 + random.randint(-200, 200), "change_pct": round(random.uniform(-2, 2), 2), "market": "cn"},
                    {"code": "^HSI", "name": "恒生指数", "price": 18000 + random.randint(-300, 300), "change_pct": round(random.uniform(-1.5, 1.5), 2), "market": "hk"},
                    {"code": "^SPX", "name": "标普500", "price": 5500 + random.randint(-80, 80), "change_pct": round(random.uniform(-1, 1), 2), "market": "us"},
                ],
                "sectors_top": [{"name": n, "change_pct": round(random.uniform(1, 3), 2)} for n in ["科技", "半导体", "新能源", "军工", "消费"]],
                "sectors_bottom": [{"name": n, "change_pct": round(random.uniform(-3, -1), 2)} for n in ["房地产", "建材", "银行", "农业", "公用事业"]],
            }
            mo_path.write_text(json.dumps(demo_mo, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  大盘 demo 数据生成完成。\n")
        except Exception as exc:
            print(f"  大盘 demo 数据生成失败 ({exc}); 大盘卡片将隐藏。\n", file=sys.stderr)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.address_string()}  {fmt % args}\n")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    # 路由分发:静态文件按原 SimpleHTTPRequestHandler 处理;
    # /api/quote 走本地 fetch_quotes 实时拉单只股票行情。
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        parsed = urlparse(self.path)
        if parsed.path == "/api/quote":
            return self._handle_api_quote(parse_qs(parsed.query or ""))
        return super().do_GET()

    def _handle_api_quote(self, query: dict) -> None:
        codes = [c.strip().upper() for c in (query.get("code") or []) if c.strip()]
        if not codes:
            return self._send_json({"ok": False, "error": "missing ?code= parameter"}, status=400)
        code = codes[0]
        if not _FETCH_QUOTES_OK or _fetch_quotes is None:
            return self._send_json(
                {"ok": False, "code": code, "error": "fetch_quotes unavailable on server"},
                status=503,
            )
        try:
            future = _QUOTE_EXECUTOR.submit(_fetch_quotes, [code])
            quotes = future.result(timeout=_QUOTE_TIMEOUT_S)
        except Exception as exc:
            return self._send_json({"ok": False, "code": code, "error": str(exc) or exc.__class__.__name__}, status=504)
        quote = (quotes or {}).get(code) or {}
        self._send_json({
            "ok": True,
            "code": code,
            "quote": quote,
            "has_quote": quote.get("price") is not None,
        })

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器在请求中途关掉连接很常见,silent drop
            pass

def open_browser_later(url: str, delay: float = 1.0) -> None:
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:  # pragma: no cover - best effort
            print(f"  (could not open browser: {exc})", file=sys.stderr)

    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    p = argparse.ArgumentParser(description="Serve Stock Radar locally for browser preview.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default 8765)")
    p.add_argument("--host", default=DEFAULT_HOST, help="bind host (default 127.0.0.1)")
    p.add_argument("--no-open", action="store_true", help="do not auto-open browser")
    args = p.parse_args()

    os.chdir(ROOT)
    ensure_demo_data()
    url = f"http://{args.host}:{args.port}/"

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"\n  bind {args.host}:{args.port} failed: {exc}", file=sys.stderr)
        print(f"  try:  python3 scripts/serve.py --port 9000\n", file=sys.stderr)
        return 1

    print()
    print("  Stock Radar  静态预览")
    print("  ------------------------")
    print(f"  URL  ........  {url}")
    print(f"  Root ........  {ROOT}")
    print("  Press Ctrl+C to stop.")
    print()

    if not args.no_open:
        open_browser_later(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
