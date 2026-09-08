"""
zero-dependency dashboard — 표준 라이브러리 http.server 로 traces.jsonl 을 보여준다.
외부 JS / CSS / 폰트를 하나도 불러오지 않으므로 인터넷이 없는 사내망에서도 열린다.

    python dashboard.py                    # ./traces.jsonl, http://127.0.0.1:8790
    python dashboard.py --open             # 서버를 띄우고 기본 브라우저를 자동으로 연다
    python dashboard.py traces.jsonl 9000  # 파일·포트 지정
    start_dashboard.cmd                    # Windows: 더블클릭 = 위 --open 과 동일

화면은 2초마다 traces.jsonl 을 다시 읽으므로, 열어둔 채 demo_loop.py 를 돌리면 트레이스가 실시간으로 쌓이는 것이 보인다.

화면 구성
  좌측  트레이스 목록      (실행 1건 = 1행. 상태 · 시도 횟수 · 토큰 · 소요시간)
  우측  워터폴             (스팬을 시간축 위에 부모-자식 중첩으로 그린다)
  하단  노드별 통계        (이름별 호출 수 · 평균/최대 소요 · 오류 · 토큰)
  스팬을 클릭하면 속성(attributes)과 이벤트가 그대로 펼쳐진다.
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tracer import load_traces, trace_to_mermaid

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags = {a for a in sys.argv[1:] if a.startswith("--")}
TRACE_PATH = Path(_args[0]) if _args else Path(__file__).parent / "traces.jsonl"   # 어디서 실행해도 키트 폴더의 파일을 본다
PORT = int(_args[1]) if len(_args) > 1 else 8790
OPEN_BROWSER = "--open" in _flags

HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>zero-dep tracer</title>
<style>
:root{--bg:#0f1115;--panel:#161a22;--line:#262c38;--fg:#e6e9ef;--dim:#8b93a7;--ok:#3fb950;--err:#f85149;--warn:#d29922;--llm:#58a6ff;--sel:#1f2a3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 ui-monospace,Consolas,"Malgun Gothic",monospace}
header{display:flex;gap:24px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:14px;margin:0;font-weight:600}header .kpi{color:var(--dim)}header .kpi b{color:var(--fg);margin-left:4px}
main{display:grid;grid-template-columns:340px 1fr;height:calc(100vh - 44px)}
#list{border-right:1px solid var(--line);overflow:auto}
.row{padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer}.row:hover{background:#1a1f29}.row.sel{background:var(--sel)}
.row .n{font-weight:600}.row .m{color:var(--dim);font-size:12px;margin-top:2px}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:11px;margin-right:6px}
.tag.ok{background:#12351c;color:var(--ok)}.tag.err{background:#3d1515;color:var(--err)}.tag.warn{background:#3a2c0a;color:var(--warn)}
#right{display:grid;grid-template-rows:1fr auto;overflow:hidden}
#wf{overflow:auto;padding:12px 16px}
.sp{display:grid;grid-template-columns:300px 1fr;align-items:center;height:24px;cursor:pointer;border-radius:3px}
.sp:hover,.sp.sel{background:#1a1f29}
.sp .lbl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--fg)}
.sp .lbl .d{color:var(--dim);margin-left:6px;font-size:11px}
.sp .track{position:relative;height:14px;background:#0b0d11;border-radius:2px}
.sp .bar{position:absolute;top:0;height:14px;border-radius:2px;background:var(--ok);min-width:2px}
.sp .bar.err{background:var(--err)}.sp .bar.warn{background:var(--warn)}.sp .bar.fail{background:#e3742f}.sp .bar.llm{background:var(--llm)}
#detail{border-top:1px solid var(--line);background:var(--panel);max-height:42vh;overflow:auto;padding:12px 16px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
pre{margin:0;white-space:pre-wrap;word-break:break-all;color:#c9d1d9;font-size:12px}
h3{margin:0 0 6px;font-size:12px;color:var(--dim);font-weight:600;letter-spacing:.04em}
h3 button{background:#1f2a3a;color:var(--fg);border:1px solid var(--line);border-radius:3px;font:inherit;font-size:11px;padding:1px 8px;cursor:pointer;margin-left:8px}h3 button:hover{background:#26334a}
table{border-collapse:collapse;width:100%}td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right;font-size:12px}th{color:var(--dim);font-weight:600}td:first-child,th:first-child{text-align:left}
.alert{color:var(--warn)}.empty{color:var(--dim);padding:24px}
</style></head><body>
<header><h1>zero-dep tracer</h1>
<span class="kpi">traces<b id="k-n">0</b></span><span class="kpi">errors<b id="k-e">0</b></span>
<span class="kpi">alerts<b id="k-a">0</b></span><span class="kpi">tokens<b id="k-t">0</b></span>
<span class="kpi" style="margin-left:auto" id="k-src"></span></header>
<main><div id="list"></div><div id="right"><div id="wf"><div class="empty">트레이스를 선택하세요</div></div>
<div id="detail"><div><h3>SPAN <button id="btn-mm" title="선택한 트레이스의 실제 실행 경로를 mermaid 소스로 — GitHub/Notion 에 붙이면 그려집니다">mermaid</button></h3><pre id="d-span">스팬을 클릭하면 속성이 여기 표시됩니다</pre></div><div><h3>노드별 통계</h3><div id="stats"></div></div></div></div></main>
<script>
const $=s=>document.querySelector(s);let traces=[],selected=null,selSpan=null;
const fmt=ms=>ms>=1000?(ms/1000).toFixed(2)+' s':ms.toFixed(1)+' ms';
const tok=s=>(s.attributes['llm.input_tokens']||0)+(s.attributes['llm.output_tokens']||0);
const cls=s=>s.status==='ERROR'?'err':(s.attributes.alert?'warn':(s.attributes.verdict==='FAIL'?'fail':(s.name.startsWith('llm')?'llm':'ok')));
async function load(){
  const r=await fetch('/api/traces');const data=await r.json();$('#k-src').textContent=data.path;
  traces=data.traces.map(spans=>{
    spans.sort((a,b)=>a.start_time_unix_nano-b.start_time_unix_nano);
    const root=spans.find(s=>!s.parent_span_id)||spans[0];
    const end=Math.max(...spans.map(s=>s.end_time_unix_nano||s.start_time_unix_nano));
    return{id:root.trace_id,root,spans,start:spans[0].start_time_unix_nano,end,
      tokens:spans.reduce((n,s)=>n+tok(s),0),attempts:spans.filter(s=>s.name==='attempt').length,
      errors:spans.filter(s=>s.status==='ERROR').length,alerts:spans.filter(s=>s.attributes.alert).length};
  }).sort((a,b)=>b.start-a.start);
  $('#k-n').textContent=traces.length;$('#k-e').textContent=traces.filter(t=>t.errors).length;
  $('#k-a').textContent=traces.filter(t=>t.alerts).length;$('#k-t').textContent=traces.reduce((n,t)=>n+t.tokens,0).toLocaleString();
  if(selected===null&&traces.length)selected=traces[0].id;
  renderList();renderWf();renderStats();
}
function renderList(){
  $('#list').innerHTML=traces.map(t=>{
    const fs=t.root.attributes.final_status;const when=new Date(t.start/1e6).toLocaleTimeString();
    const tag=t.errors?'<span class="tag err">ERROR</span>':(t.alerts?'<span class="tag warn">ALERT</span>':'<span class="tag ok">OK</span>');
    return`<div class="row ${t.id===selected?'sel':''}" data-id="${t.id}"><div class="n">${tag}${t.root.name}${fs?' · '+fs:''}</div>
    <div class="m">${when} · ${fmt((t.end-t.start)/1e6)} · attempts ${t.attempts} · tokens ${t.tokens} · spans ${t.spans.length}</div></div>`;}).join('')||'<div class="empty">아직 트레이스가 없습니다. demo_loop.py 를 실행하세요.</div>';
  document.querySelectorAll('.row').forEach(el=>el.onclick=()=>{selected=el.dataset.id;selSpan=null;renderList();renderWf();});
}
function renderWf(){
  const t=traces.find(x=>x.id===selected);if(!t){return;}
  const total=Math.max(1,t.end-t.start);const depth={};
  const byId=Object.fromEntries(t.spans.map(s=>[s.span_id,s]));
  const d=s=>{if(depth[s.span_id]!=null)return depth[s.span_id];const p=byId[s.parent_span_id];return depth[s.span_id]=p?d(p)+1:0;};
  // 부모 → 자식 순서(DFS)로 정렬해 트리가 읽히게 한다
  const kids={};t.spans.forEach(s=>{(kids[s.parent_span_id||'root']=kids[s.parent_span_id||'root']||[]).push(s)});
  const order=[];const walk=id=>{(kids[id]||[]).forEach(s=>{order.push(s);walk(s.span_id)})};walk('root');
  t.spans.filter(s=>!order.includes(s)).forEach(s=>order.push(s));
  $('#wf').innerHTML=order.map(s=>{
    const l=(s.start_time_unix_nano-t.start)/total*100,w=Math.max(0.4,((s.end_time_unix_nano||s.start_time_unix_nano)-s.start_time_unix_nano)/total*100);
    const extra=[s.attributes.attempt!=null?'#'+s.attributes.attempt:'',s.attributes.verdict||'',tok(s)?tok(s)+' tok':'',s.attributes.alert?'⚠':''].filter(Boolean).join(' · ');
    return`<div class="sp ${selSpan===s.span_id?'sel':''}" data-id="${s.span_id}"><div class="lbl" style="padding-left:${d(s)*16}px">${s.status==='ERROR'?'✗ ':''}${s.name}<span class="d">${fmt(s.duration_ms)}${extra?' · '+extra:''}</span></div>
    <div class="track"><div class="bar ${cls(s)}" style="left:${l}%;width:${w}%"></div></div></div>`;}).join('');
  document.querySelectorAll('.sp').forEach(el=>el.onclick=()=>{selSpan=el.dataset.id;renderWf();
    const s=byId[selSpan];$('#d-span').textContent=JSON.stringify({name:s.name,status:s.status,status_message:s.status_message,duration_ms:s.duration_ms,attributes:s.attributes,events:s.events},null,2);});
}
function renderStats(){
  const agg={};traces.forEach(t=>t.spans.forEach(s=>{const a=agg[s.name]=agg[s.name]||{n:0,sum:0,max:0,err:0,tok:0};a.n++;a.sum+=s.duration_ms;a.max=Math.max(a.max,s.duration_ms);a.err+=s.status==='ERROR';a.tok+=tok(s);}));
  const rows=Object.entries(agg).sort((a,b)=>b[1].sum-a[1].sum).map(([n,a])=>`<tr><td>${n}</td><td>${a.n}</td><td>${fmt(a.sum/a.n)}</td><td>${fmt(a.max)}</td><td class="${a.err?'alert':''}">${a.err}</td><td>${a.tok}</td></tr>`).join('');
  $('#stats').innerHTML=`<table><tr><th>span</th><th>calls</th><th>avg</th><th>max</th><th>errors</th><th>tokens</th></tr>${rows}</table>`;
}
$('#btn-mm').onclick=async()=>{if(!selected)return;const r=await fetch('/api/mermaid?trace='+encodeURIComponent(selected));$('#d-span').textContent='```mermaid\n'+await r.text()+'\n```';};
load();setInterval(load,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/traces":
            payload = {"path": str(TRACE_PATH), "traces": list(load_traces(TRACE_PATH).values())}
            self._send(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/mermaid":
            # 선택한 트레이스의 실제 실행 경로를 mermaid 소스로. 렌더링은 GitHub/Notion 에 붙여서 — 대시보드는 오프라인을 지킨다.
            trace_id = parse_qs(urlparse(self.path).query).get("trace", [""])[0]
            spans = load_traces(TRACE_PATH).get(trace_id, [])
            self._send(200, "text/plain; charset=utf-8", trace_to_mermaid(spans).encode("utf-8"))
        elif path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 요청 로그로 터미널을 어지럽히지 않는다
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"dashboard  {url}   (reading {TRACE_PATH})   Ctrl+C to stop")
    if OPEN_BROWSER:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
