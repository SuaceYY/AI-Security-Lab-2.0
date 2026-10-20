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
<title>GLM × DeepSeek 协作会话</title>
<style>
:root{color-scheme:dark;font-family:system-ui,"Microsoft YaHei",sans-serif}
*{box-sizing:border-box}body{margin:0;background:#10131b;color:#e9edf5}
header{padding:19px 4%;border-bottom:1px solid #333d50;background:#191f2d}
h1{font-size:22px;margin:0 0 7px}.muted{color:#a2afc2;font-size:13px}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;padding:18px;max-width:1500px;margin:auto}
.panel{background:#1b2231;border:1px solid #344054;border-radius:14px;min-width:0;overflow:hidden}
.panel h2{padding:16px;margin:0;border-bottom:1px solid #354157;font-size:17px}
.feed{height:65vh;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:14px}
.bubble{max-width:96%;background:#2b364c;border-radius:13px;padding:13px 16px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}
.bubble.glm{border-left:4px solid #a18dff}.bubble.ds{border-left:4px solid #48c3a7}
.meta{font-size:12px;color:#b6c5dd;margin-bottom:7px;font-weight:bold}
.bottom{padding:15px 18px;border-top:1px solid #354157;color:#a2afc2;font-size:13px}
a{color:#8dbaff}.full{grid-column:1/-1;padding:16px}
@media(max-width:760px){.layout{grid-template-columns:1fr}.feed{height:45vh}}
</style>
<header><h1>GLM × DeepSeek · 协作会话</h1><div class="muted">真实任务与执行事件 · 左右分屏 · 3 秒自动刷新 · 只读 · 不展示未经采集的模型内部思考</div></header>
<div class="layout">
<section class="panel"><h2>🟣 GLM · 规划与审查</h2><div class="feed" id="glm"></div><div class="bottom" id="phase">加载中…</div></section>
<section class="panel"><h2>🟢 DeepSeek · 执行与交付</h2><div class="feed" id="ds"></div><div class="bottom" id="worker">加载中…</div></section>
<section class="panel full"><strong>人工合并闸门</strong> · <a href="https://github.com/SuaceYY/AI-Security-Lab-2.0/issues/15" target="_blank" rel="noopener">Issue #15 ↗</a> · <a href="https://github.com/SuaceYY/AI-Security-Lab-2.0/pull/16" target="_blank" rel="noopener">PR #16 ↗</a> <span class="muted">（界面不会合并 PR）</span></section>
</div>
<script>
function bubble(who,header,text){
 const el=document.createElement('article');el.className='bubble '+who;
 const title=document.createElement('div');title.className='meta';title.textContent=header;
 const body=document.createElement('div');body.textContent=text;el.append(title,body);return el;
}
let lastKey='';
async function refresh(){
 try{
 const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw Error(response.status);
 const d=await response.json(), glm=[],ds=[];
 const issue=d.current_issue||15;
 glm.push(bubble('glm','GLM · 任务交接','已创建 Issue #'+issue+'。任务详情与验收标准请查看 GitHub Issue。'));
 const items=d.events.filter(e=>e.includes('#'+issue)||e.includes('verification'));
 for(const line of items){
  const low=line.toLowerCase();
  if(low.includes('claimed issue'))ds.push(bubble('ds','DeepSeek · 领取任务','已领取 Issue #'+issue+'，准备执行。'));
  else if(low.includes('starting opencode'))ds.push(bubble('ds','DeepSeek · 开始执行','OpenCode 开始执行任务。'));
  else if(low.includes('session for issue'))ds.push(bubble('ds','DeepSeek · 会话已建立','已创建执行会话。'));
  else if(low.includes('running fixed verification'))ds.push(bubble('ds','DeepSeek · 验证','正在执行固定验证。'));
  else if(low.includes('succeeded with commit')){
    ds.push(bubble('ds','DeepSeek · 已交付','任务完成并创建 PR。\n'+line.match(/https:\/\/\S+/)?.[0]));
  }else if(low.includes('failed'))ds.push(bubble('ds','DeepSeek · 失败',line));
 }
 if(ds.length===0)ds.push(bubble('ds','DeepSeek · 等待','尚未采集到当前任务的执行事件。'));
 const key=JSON.stringify({items,phase:d.coordinator.phase,worker:d.worker_stage});
 if(key!==lastKey){for(const [id,arr] of [['glm',glm],['ds',ds]]){
 const box=document.getElementById(id);const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-80;
 box.replaceChildren(...arr);if(atBottom)box.scrollTop=box.scrollHeight;
 }lastKey=key;}
 document.getElementById('phase').textContent='GLM 状态：'+(d.coordinator.phase||'idle')+' · 审查内容将在接入审查记录后显示';
 document.getElementById('worker').textContent='Worker：'+d.worker_stage+' · 当前展示可核实的执行阶段，并非模型逐字对话';
 }catch(e){document.getElementById('worker').textContent='数据读取失败：'+e.message}
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
