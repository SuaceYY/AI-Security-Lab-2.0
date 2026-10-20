"""Local read-only visual dashboard for the dual-AI workflow.

Run: python -m tools.dual_ai.dashboard
No AI calls, GitHub mutations, or Worker restarts.
"""
from __future__ import annotations

import json
import re
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "issue_claimer"
STATE = ROOT / "data" / "dual_ai" / "coordinator.json"
LOG = DATA / "claimer.log"
PORT = 8765

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>双 AI 协作控制台</title>
<style>
:root{color-scheme:dark;font-family:system-ui,"Microsoft YaHei",sans-serif}
body{margin:0;background:#10131b;color:#e9edf5}header{padding:24px 5%;background:#191f2d;border-bottom:1px solid #313b50}
h1{margin:0 0 8px;font-size:25px}.muted{color:#9ba9bf}.wrap{max-width:1150px;margin:24px auto;padding:0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
.card{background:#1b2231;border:1px solid #343e50;border-radius:13px;padding:20px}
h2{font-size:17px;margin:0 0 14px}.big{font-size:24px;font-weight:700;margin:12px 0}
.pill{display:inline-block;padding:5px 12px;background:#30425c;border-radius:20px}
#events{max-height:55vh;overflow:auto}.event{border-left:3px solid #608bdb;padding:8px 14px;margin:8px 0;background:#232c3d;border-radius:4px;white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#83b5ff}button{background:#304b77;color:white;border:0;border-radius:8px;padding:9px 15px;cursor:pointer}
</style><header><h1>双 AI 协作控制台</h1><div class="muted">GLM 规划 → DeepSeek 执行 → GLM 审查 → 人工合并 · 只读实时视图</div></header>
<div class="wrap"><div class="grid">
<div class="card"><h2>GLM 协调器</h2><div id="phase" class="big">读取中…</div><div id="created" class="muted"></div></div>
<div class="card"><h2>DeepSeek Worker</h2><div id="worker" class="big">读取中…</div><div id="issue" class="muted"></div></div>
<div class="card"><h2>人工审核</h2><div class="big">禁止自动合并</div><a href="https://github.com/SuaceYY/AI-Security-Lab-2.0/pulls" target="_blank" rel="noopener">查看 GitHub PR ↗</a></div>
</div><section class="card" style="margin-top:18px"><h2>实时任务时间线 <span id="updated" class="muted"></span></h2>
<div class="muted">显示系统实际记录的阶段事件，不伪造模型内部思考。每 3 秒刷新。</div>
<div id="events"></div></section></div>
<script>
async function refresh(){
 try{
 const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);
 const d=await r.json();
 document.getElementById('phase').textContent=d.coordinator.phase||'idle';
 document.getElementById('created').textContent='协调器创建的 Issue：'+(d.coordinator.created||[]).map(n=>'#'+n).join('、');
 document.getElementById('worker').textContent=d.worker_stage||'等待事件';
 document.getElementById('issue').textContent=d.current_issue?'当前 Issue #'+d.current_issue:'尚无新任务事件';
 document.getElementById('events').replaceChildren(...d.events.map(e=>{
 const div=document.createElement('div');div.className='event';div.textContent=e;return div;
 }));
 document.getElementById('updated').textContent=' · 更新于 '+new Date().toLocaleTimeString();
 }catch(e){document.getElementById('updated').textContent=' · 读取失败：'+e.message}
}
refresh();setInterval(refresh,3000);
</script></html>"""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def snapshot() -> dict:
    try:
        # Read a bounded tail; avoid loading an indefinitely growing log.
        with LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 48000))
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()[-120:]
    except OSError:
        lines = []
    current_issue = None
    stage = "等待 Worker"
    for line in reversed(lines):
        match = re.search(r"(?:claimed issue #|for issue #)(\d+)", line)
        if match:
            current_issue = int(match.group(1))
            break
    if lines:
        last = lines[-1].lower()
        if "starting opencode attempt" in last:
            stage = "DeepSeek 执行中"
        elif "claimed issue" in last:
            stage = "任务已领取"
        elif "waiting" in last:
            stage = "等待下一轮任务"
        else:
            stage = "运行中（查看时间线）"
    events = [line for line in lines if
              any(k in line.lower() for k in
                  ("issue #", "opencode", "verification", "pull request", "waiting", "error", "failed"))][-65:]
    return {"coordinator": read_json(STATE), "worker_stage": stage,
            "current_issue": current_issue, "events": events}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/api/status":
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    address = ("127.0.0.1", PORT)
    with ThreadingHTTPServer(address, Handler) as server:
        print(f"双 AI 控制台已启动：http://127.0.0.1:{PORT}", flush=True)
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
