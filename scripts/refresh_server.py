#!/usr/bin/env python3
"""
Stock Radar 刷新触发器。

启动方式：
  .venv/bin/python3 scripts/refresh_server.py [--port 8766]

提供端点：
  POST /refresh         异步触发一次数据更新
  GET  /status/<job_id> 查询任务状态
  GET  /health          健康检查

后端会用 subprocess 调 update_news.py，前端轮询 /status 拿结果。
"""
from __future__ import annotations
import argparse
import json
import logging
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPDATE_SCRIPT = ROOT / "scripts" / "update_news.py"

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")
CORS(app)

# 任务状态（内存）
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def run_update_job(job_id: str) -> None:
    """后台跑 update_news.py，更新 jobs[job_id] 状态。"""
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = time.time()

    log.info("[%s] start update_news.py", job_id)
    try:
        proc = subprocess.run(
            [sys.executable, str(UPDATE_SCRIPT), "--output-dir", str(DATA_DIR)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟上限
        )
        log.info("[%s] returncode=%d", job_id, proc.returncode)

        with jobs_lock:
            jobs[job_id]["finished_at"] = time.time()
            jobs[job_id]["stdout_tail"] = "\n".join(proc.stdout.splitlines()[-20:])
            jobs[job_id]["stderr_tail"] = "\n".join(proc.stderr.splitlines()[-20:])

        if proc.returncode == 0:
            with jobs_lock:
                jobs[job_id]["status"] = "success"
            # 读最新 generated_at
            data_file = DATA_DIR / "latest-24h.json"
            if data_file.exists():
                try:
                    d = json.loads(data_file.read_text(encoding="utf-8"))
                    with jobs_lock:
                        jobs[job_id]["total_items"] = d.get("total_items")
                        jobs[job_id]["generated_at"] = d.get("generated_at")
                except Exception as e:
                    log.warning("read latest-24h failed: %s", e)
        else:
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
    except subprocess.TimeoutExpired:
        with jobs_lock:
            jobs[job_id]["status"] = "timeout"
            jobs[job_id]["finished_at"] = time.time()
    except Exception as e:
        log.exception("[%s] crash", job_id)
        with jobs_lock:
            jobs[job_id]["status"] = "crashed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["finished_at"] = time.time()


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "stock-radar-refresh", "ts": time.time()})


@app.route("/refresh", methods=["POST"])
def trigger_refresh():
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "created_at": time.time(),
        }
    # 后台线程启动
    t = threading.Thread(target=run_update_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/status/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found", "job_id": job_id}), 404
    return jsonify(job)


@app.route("/jobs")
def list_jobs():
    with jobs_lock:
        return jsonify({"jobs": list(jobs.values())[-10:]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    log.info("Stock Radar refresh server on http://%s:%d", args.host, args.port)
    log.info("POST /refresh    GET /status/<id>    GET /health")
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
