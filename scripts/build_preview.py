#!/usr/bin/env python3
"""Generate preview.html — single-file offline preview of stock-radar.
All data/*.json files are inlined as window.__RADAR_DATA__ so the page
works when opened directly via file:// (no HTTP server needed).

Usage: python3 scripts/build_preview.py
Output: preview.html (project root)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "preview.html"

DATA_FILES = [
    "latest-24h.json",
    "stories-merged.json",
    "source-status.json",
    "daily-brief.json",
    "latest-quotes.json",
    "market-overview.json",
]

SHIM = """
<script>
  // preview shim: serve JSON from window.__RADAR_DATA__
  (function () {
    var _orig = window.fetchJSON;
    window.fetchJSON = async function (path) {
      if (path in window.__RADAR_DATA__) return window.__RADAR_DATA__[path];
      return _orig(path);
    };
    // Replace the refresh button with a clone to strip the listener that
    // app.js already attached (via bindChips during init). Then attach a
    // preview-mode hint handler instead. cloneNode is the simplest reliable
    // way to detach handlers without holding a reference to the original fn.
    (function patchRefresh() {
      var btn = document.getElementById("refresh-btn");
      if (!btn) return;
      var fresh = btn.cloneNode(true);
      if (btn.parentNode) btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener("click", function () {
        if (typeof showRefreshToast === "function") {
          showRefreshToast(
            "预览模式",
            "刷新功能需在终端运行 scripts/refresh_server.py 后才可用。当前数据已 inline,重新生成请跑 scripts/build_preview.py。",
            ""
          );
        }
      });
    })();
    console.log("[preview] shim active — serving", Object.keys(window.__RADAR_DATA__).length, "datasets from inlined data");
  })();
</script>
"""

def main() -> None:
    template = (ROOT / "index.html").read_text(encoding="utf-8")

    blocks = ["<script>", "window.__RADAR_DATA__ = {"]
    for name in DATA_FILES:
        path = ROOT / "data" / name
        if not path.exists():
            print(f"WARN: missing {path}, skipping")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"WARN: {name} is not valid JSON ({e}), skipping")
            continue
        print(f"  + {name} ({path.stat().st_size / 1024:.1f} KB)")
        blocks.append(f'  "{name}": {json.dumps(data, ensure_ascii=False)},')
    blocks.append("};")
    blocks.append("</script>")
    inline_data = "\n".join(blocks)

    if "<body>" not in template:
        raise SystemExit("index.html has no <body> tag")
    template = template.replace("<body>", "<body>\n" + inline_data, 1)

    last_script_close = template.rfind("</script>")
    if last_script_close == -1:
        raise SystemExit("index.html has no </script> tag")
    insert_at = last_script_close + len("</script>")
    template = template[:insert_at] + "\n" + SHIM + template[insert_at:]

    OUT.write_text(template, encoding="utf-8")
    print(f"\nWrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")
    print("Open it with: open preview.html   (or double-click in Finder)")

if __name__ == "__main__":
    main()
