"""Small local web UI for starting and monitoring an exploration."""

from __future__ import annotations

import ipaddress
import json
import re
import queue
import secrets
import sqlite3
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .database import GraphDatabase
from .device import AndroidDevice
from .explorer import Explorer, ExplorerConfig
from .export import export_graph


class ExplorerService:
    def __init__(self):
        self.lock = threading.RLock()
        self.events: queue.Queue[dict] = queue.Queue(maxsize=1000)
        self.status: dict = {"running": False, "message": "Ready", "error": None, "states": 0, "edges": 0}
        self.explorer: Explorer | None = None
        self.output_dir: Path | None = None
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.api_token: str | None = None

    def publish(self, message: str, kind: str = "log") -> None:
        event = {"kind": kind, "message": message, "time": time.time()}
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(event)
            except queue.Empty:
                pass
        with self.lock:
            self.status["message"] = message
            explorer = self.explorer
            if explorer is not None:
                try:
                    # Progress callbacks run on the explorer worker thread,
                    # which owns this SQLite connection.
                    self.status["states"], self.status["edges"] = explorer.db.counts()
                except Exception:
                    pass

    def control_token(self) -> str:
        with self.lock:
            if self.api_token is None:
                self.api_token = secrets.token_urlsafe(32)
            return self.api_token

    def snapshot(self) -> dict:
        with self.lock:
            status = dict(self.status)
            output_dir = self.output_dir
        return {
            **status,
            "output": str(output_dir) if output_dir else None,
        }

    def start(self, values: dict[str, str], token: str | None = None) -> tuple[bool, str]:
        with self.lock:
            if self.status["running"]:
                return False, "An exploration is already running."
            if self.api_token is not None and not secrets.compare_digest(token or "", self.api_token):
                return False, "Invalid control token."
        try:
            package = values["package"].strip()
            output_dir = Path(values["output"]).expanduser().resolve()
            if not package:
                return False, "Package ID is required."
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
                return False, "Package ID format is invalid."
            config = ExplorerConfig(
                package=package,
                output_dir=output_dir,
                max_depth=int(values.get("max_depth", 5)),
                max_states=int(values.get("max_states", 100)),
                max_actions_per_state=int(values.get("max_actions_per_state", 80)),
                settle_seconds=float(values.get("settle_seconds", 0.6)),
                phash_distance=int(values.get("phash_distance", 8)),
                ssim_threshold=float(values.get("ssim_threshold", 0.9)),
                structure_threshold=float(values.get("structure_threshold", 0.9)),
                navigation_only=values.get("navigation_only", "").lower() in {"1", "true", "yes", "on"},
                seed_activities=values.get("seed_activities", "").lower() in {"1", "true", "yes", "on"},
            )
            serial = values.get("device", "").strip() or None
            apk_text = values.get("apk", "").strip()
            if apk_text and not Path(apk_text).expanduser().is_file():
                return False, f"APK file does not exist: {Path(apk_text).expanduser()}"
        except (KeyError, ValueError) as exc:
            return False, f"Invalid settings: {exc}"

        with self.lock:
            if self.status["running"]:
                return False, "An exploration is already running."
            self.cancel.clear()
            self.output_dir = output_dir
            self.status = {"running": True, "message": "Starting…", "error": None, "states": 0, "edges": 0}
        self.thread = threading.Thread(
            target=self._run,
            args=(config, serial, Path(apk_text).expanduser() if apk_text else None),
            daemon=True,
        )
        try:
            self.thread.start()
        except RuntimeError as exc:
            with self.lock:
                self.status.update(running=False, error=str(exc), message=f"Failed to start worker: {exc}")
            return False, f"Could not start explorer: {exc}"
        return True, "Exploration started."

    def _run(self, config: ExplorerConfig, serial: str | None, apk: Path | None) -> None:
        explorer = None
        error = None
        try:
            if serial is None:
                devices = AndroidDevice().list_devices()
                if not devices:
                    raise RuntimeError("No authorized Android device found. Start an emulator or connect a device.")
                if len(devices) > 1:
                    available = ", ".join(item["serial"] for item in devices)
                    raise RuntimeError(f"Multiple devices are connected; select one: {available}")
                serial = devices[0]["serial"]
            device = AndroidDevice(serial=serial)
            if apk:
                device.install(apk)
            explorer = Explorer(device, config, progress=self.publish)
            with self.lock:
                self.explorer = explorer
                already_stopped = self.cancel.is_set()
            if already_stopped:
                explorer.request_stop()
            states, edges = explorer.run()
            with self.lock:
                self.status["states"], self.status["edges"] = states, edges
            files = export_graph(explorer.db, config.output_dir)
            if explorer.stop_requested:
                self.publish(f"Stopped: saved partial graph with {states} states and {edges} transitions. HTML: {files['html']}", "complete")
            else:
                self.publish(f"Complete: {states} states, {edges} edges. HTML: {files['html']}", "complete")
        except Exception as exc:  # surfaced in the UI and terminal process log
            error = str(exc)
            self.publish(f"Failed: {error}", "error")
        finally:
            if explorer is not None:
                try:
                    # Keep a browsable partial graph even when device interaction fails.
                    export_graph(explorer.db, config.output_dir)
                except Exception:
                    pass
                try:
                    # Keep only durable canonical/variant assets and remove scratch captures.
                    working = config.output_dir / "states" / ".working"
                    if working.exists():
                        shutil.rmtree(working, ignore_errors=True)
                    explorer.close()
                except Exception:
                    pass
            with self.lock:
                self.explorer = None
                self.status.update(running=False, error=error)

    def stop(self, token: str | None = None) -> bool:
        if self.api_token is not None and not secrets.compare_digest(token or "", self.api_token):
            return False
        self.cancel.set()
        with self.lock:
            explorer = self.explorer
            if self.status["running"]:
                self.status["message"] = "Stop requested (finishes current action)."
        if explorer is not None:
            explorer.request_stop()
        return True

    def graph_data(self) -> dict:
        with self.lock:
            output_dir = self.output_dir
        if output_dir is None or not (output_dir / "graph.db").is_file():
            return {"states": [], "edges": []}
        database_path = output_dir / "graph.db"
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro", uri=True, timeout=1.0
        )
        connection.row_factory = sqlite3.Row
        try:
            states = [dict(row) for row in connection.execute("SELECT * FROM states ORDER BY id")]
            variants_by_state: dict[int, list[dict]] = {}
            for row in connection.execute("SELECT * FROM variants ORDER BY id"):
                variants_by_state.setdefault(int(row["state_id"]), []).append(
                    {
                        "screenshot_path": row["screenshot_path"],
                        "xml_path": row["xml_path"],
                        "phash": row["phash"],
                        "created_at": row["created_at"],
                    }
                )
            for state in states:
                state["variants"] = variants_by_state.get(int(state["id"]), [])
            edges = connection.execute(
                "SELECT e.from_state, e.to_state, a.data_json FROM edges e "
                "JOIN actions a ON a.id = e.action_id ORDER BY e.id"
            ).fetchall()
            return {
                "states": states,
                "edges": [
                    {
                        "from": row["from_state"],
                        "to": row["to_state"],
                        "action": json.loads(row["data_json"]),
                    }
                    for row in edges
                ],
            }
        finally:
            connection.close()

    def screenshot(self, relative_path: str) -> Path | None:
        with self.lock:
            output_dir = self.output_dir
        if output_dir is None:
            return None
        root = (output_dir / "states").resolve()
        candidate = (output_dir / relative_path).resolve()
        if not candidate.is_relative_to(root) or candidate.suffix.lower() != ".png":
            return None
        return candidate if candidate.is_file() else None

    def drain_events(self) -> list[dict]:
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result


_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Android App Graph Explorer</title>
<style>
:root{color-scheme:light;--bg:#f3f6fb;--panel:#fff;--ink:#172033;--muted:#65748b;--line:#dfe6f0;--blue:#335dff;--green:#11845b;--red:#c34242}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{height:70px;padding:0 30px;display:flex;align-items:center;justify-content:space-between;background:#fff;border-bottom:1px solid var(--line)}.brand{font-size:18px;font-weight:750;letter-spacing:-.3px}.brand span{color:var(--blue)}.badge{border:1px solid var(--line);border-radius:99px;padding:5px 10px;color:var(--muted);font-size:12px}
main{max-width:1180px;margin:28px auto;padding:0 22px}.intro{margin-bottom:22px}.intro h1{font-size:26px;letter-spacing:-.6px;margin:0 0 5px}.intro p{margin:0;color:var(--muted)}.layout{display:grid;grid-template-columns:minmax(300px,390px) 1fr;gap:18px;align-items:start}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 5px 18px #1b37610a}.panel h2{font-size:15px;margin:0}.form{padding:20px}.field{margin-top:15px}.field label{display:block;font-weight:650;font-size:12px;margin-bottom:6px}.field input{width:100%;border:1px solid #d5deeb;border-radius:8px;height:38px;padding:0 10px;background:#fff;color:var(--ink);font:inherit;outline:none}.field input:focus{border-color:#8298ff;box-shadow:0 0 0 3px #335dff18}.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}.hint{font-size:11px;color:var(--muted);margin-top:4px}.actions{display:flex;gap:9px;margin-top:19px}.button{border:0;border-radius:8px;padding:10px 13px;font:inherit;font-weight:700;cursor:pointer}.primary{background:var(--blue);color:#fff;flex:1}.secondary{background:#eef2f8;color:#364258}.button:disabled{opacity:.45;cursor:not-allowed}.safety{margin-top:16px;padding:10px 11px;background:#fff8e8;border:1px solid #f5e4b8;border-radius:8px;color:#79602a;font-size:11px}
.right{display:grid;gap:18px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.stat{padding:16px}.stat small{display:block;color:var(--muted);font-weight:600}.stat strong{display:block;font-size:25px;margin-top:3px;letter-spacing:-.5px}.run-panel{padding:17px}.run-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.status{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;background:#aab4c4}.dot.running{background:#12a875;box-shadow:0 0 0 4px #12a8751f;animation:pulse 1.5s infinite}.dot.error{background:var(--red)}@keyframes pulse{50%{box-shadow:0 0 0 7px #12a87508}}.progress{height:5px;border-radius:9px;background:#edf1f7;margin:14px 0;overflow:hidden}.progress i{height:100%;display:block;background:linear-gradient(90deg,#335dff,#55a6ff);width:0;transition:width .3s}.output{font-size:11px;color:var(--muted);word-break:break-all}.log{height:330px;overflow:auto;margin-top:12px;padding:11px;background:#101827;border-radius:9px;color:#ced7e6;font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.line{padding:2px 0;border-bottom:1px solid #ffffff0a}.line.error{color:#ff9797}.line.complete{color:#8ee1bb}.empty{color:#75839a}.footer{margin-top:18px;text-align:center;font-size:11px;color:#8792a4}
@media(max-width:850px){.layout{grid-template-columns:1fr}.right{grid-row:1}.log{height:260px}}@media(max-width:480px){header{padding:0 16px}main{padding:0 13px;margin-top:18px}.stats{gap:7px}.stat{padding:12px}.stat strong{font-size:21px}.grid{grid-template-columns:1fr 1fr}}
</style></head><body>
<header><div class="brand"><span>◈</span> App Graph</div><div class="badge">LOCAL EXPLORER</div></header>
<main><div class="intro"><h1>Android 页面图谱探索</h1><p>用 BFS 自动遍历页面，按截图相似度合并状态，并实时构建页面关系图。</p></div>
<div class="layout"><section class="panel form"><h2>探索配置</h2><form id="form">
<div class="field"><label for="package">应用包名 *</label><input id="package" name="package" placeholder="com.example.app" required><div class="hint">APK 已安装时也需要填写包名</div></div>
<div class="field"><label for="apk">APK 路径（可选）</label><input id="apk" name="apk" placeholder="/path/to/app.apk"></div>
<div class="field"><label for="device">ADB 设备</label><select id="device" name="device"><option value="">正在读取设备…</option></select><div class="hint" id="deviceHint">只显示已授权设备</div></div>
<div class="field"><label for="output">输出目录</label><input id="output" name="output" value="./app-graph-output"></div>
<div class="grid"><div class="field"><label for="max_depth">最大深度</label><input id="max_depth" name="max_depth" type="number" min="0" value="5"></div><div class="field"><label for="max_states">最大状态数</label><input id="max_states" name="max_states" type="number" min="1" value="100"></div><div class="field"><label for="max_actions_per_state">每页最多动作</label><input id="max_actions_per_state" name="max_actions_per_state" type="number" min="0" value="80"></div><div class="field"><label for="settle_seconds">动作等待秒数</label><input id="settle_seconds" name="settle_seconds" type="number" min="0" step="0.1" value="0.6"></div><div class="field"><label for="phash_distance">pHash 距离</label><input id="phash_distance" name="phash_distance" type="number" min="0" max="64" value="8"></div><div class="field"><label for="ssim_threshold">SSIM 阈值</label><input id="ssim_threshold" name="ssim_threshold" type="number" min="0" max="1" step="0.01" value="0.90"></div></div>
<div class="field"><label><input type="checkbox" name="navigation_only" value="true"> 仅探索导航入口（跳过歌曲/内容与小图标，覆盖率较低）</label></div><div class="field"><label><input type="checkbox" name="seed_activities" value="true"> 枚举 APK 内可进入的 Activity 并逐个探索（提升覆盖率）</label></div><div class="safety">建议使用专用模拟器与测试账号。工具会自动点击、滚动和返回；危险操作标签过滤只是尽力而为。</div><div class="actions"><button id="start" class="button primary" type="submit">开始探索</button><button id="stop" class="button secondary" type="button" disabled>停止</button></div></form></section>
<div class="right"><div class="stats"><div class="panel stat"><small>States 页面状态</small><strong id="states">0</strong></div><div class="panel stat"><small>Transitions 页面跳转</small><strong id="edges">0</strong></div><div class="panel stat"><small>Progress 当前状态</small><strong id="stateText" style="font-size:14px;margin-top:11px">Ready</strong></div></div>
<section class="panel run-panel"><div class="run-head"><h2>实时探索日志</h2><div class="status"><i id="dot" class="dot"></i><span id="statusText">空闲</span></div></div><div class="progress"><i id="bar"></i></div><div id="output" class="output">尚未选择输出目录</div><div id="log" class="log"><div class="empty">等待开始探索…</div></div></section>
<section class="panel graph-panel"><div class="run-head"><h2>页面关系图</h2><span class="status">按 BFS 深度排列</span></div><div id="graph" class="graph-canvas"><div class="graph-empty">探索后将在这里显示页面截图与跳转关系</div></div></section></div></div>
<div class="footer">截图、XML、SQLite 数据库与 graph.html 均保存在本机输出目录。</div></main>
<script>
const form=document.querySelector('#form'),logBox=document.querySelector('#log');let lastGraph='';let apiToken='';
function addLine(item){if(logBox.querySelector('.empty'))logBox.innerHTML='';const row=document.createElement('div');row.className='line '+(item.kind||'');const time=new Date(item.time*1000).toLocaleTimeString();row.textContent=`${time}  ${item.message}`;logBox.appendChild(row);logBox.scrollTop=logBox.scrollHeight}
function drawEdges(data){const host=document.querySelector('#graphEdges');host.innerHTML='';for(const edge of data.edges){const row=document.createElement('div');row.className='graph-edge-row';const label=edge.action.text||edge.action.content_desc||edge.action.kind||'action';row.textContent=`State ${edge.from} — ${label} → State ${edge.to}`;host.appendChild(row)}}
function drawGraph(data){drawEdges(data);const key=JSON.stringify(data);if(key===lastGraph)return;lastGraph=key;const host=document.querySelector('#graph');host.innerHTML='';if(!data.states.length){host.innerHTML='<div class="graph-empty">探索后将在这里显示页面截图与跳转关系</div>';return}const depths=[...new Set(data.states.map(s=>s.depth))].sort((a,b)=>a-b),groups=depths.map(d=>data.states.filter(s=>s.depth===d)),gap=226,nodeW=152,nodeH=194,maxRows=Math.max(...groups.map(g=>g.length));host.style.width=(depths.length*gap+20)+'px';host.style.height=(maxRows*nodeH+45)+'px';const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('graph-svg');svg.setAttribute('width',host.style.width);svg.setAttribute('height',host.style.height);host.appendChild(svg);const cols=document.createElement('div');cols.className='graph-columns';host.appendChild(cols);const pos={};groups.forEach((group,index)=>{const col=document.createElement('div');col.className='graph-column';col.style.width=nodeW+'px';const label=document.createElement('div');label.className='graph-depth';label.textContent='Depth '+depths[index];col.appendChild(label);group.forEach((state,row)=>{pos[state.id]={x:12+index*gap,y:12+row*nodeH};const card=document.createElement('article');card.className='graph-node';const img=document.createElement('img');img.loading='lazy';img.src='/api/asset?path='+encodeURIComponent(state.screenshot_path);img.alt='State '+state.id;card.appendChild(img);const meta=document.createElement('div');meta.innerHTML='<b>State '+state.id+'</b><small>Visits '+state.visit_count+'</small>';card.appendChild(meta);col.appendChild(card)});cols.appendChild(col)});for(const edge of data.edges){const a=pos[edge.from],b=pos[edge.to];if(!a||!b)continue;const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',`M${a.x+nodeW},${a.y+100} C${a.x+nodeW+35},${a.y+100} ${b.x-35},${b.y+100} ${b.x},${b.y+100}`);path.setAttribute('fill','none');path.setAttribute('stroke','#9aa9bd');path.setAttribute('stroke-width','1.5');path.setAttribute('marker-end','url(#arrow)');svg.appendChild(path)}const defs=document.createElementNS('http://www.w3.org/2000/svg','defs'),marker=document.createElementNS('http://www.w3.org/2000/svg','marker');marker.setAttribute('id','arrow');marker.setAttribute('markerWidth','7');marker.setAttribute('markerHeight','7');marker.setAttribute('refX','6');marker.setAttribute('refY','3.5');marker.setAttribute('orient','auto');const tip=document.createElementNS('http://www.w3.org/2000/svg','path');tip.setAttribute('d','M0,0 L7,3.5 L0,7 z');tip.setAttribute('fill','#9aa9bd');marker.appendChild(tip);defs.appendChild(marker);svg.insertBefore(defs,svg.firstChild)}
async function refresh(){try{const r=await fetch('/api/status');const s=await r.json();document.querySelector('#states').textContent=s.states;document.querySelector('#edges').textContent=s.edges;document.querySelector('#stateText').textContent=s.message||'Ready';document.querySelector('#output').textContent=s.output||'尚未选择输出目录';document.querySelector('#statusText').textContent=s.running?'探索中':(s.error?'失败':'空闲');document.querySelector('#dot').className='dot '+(s.running?'running':s.error?'error':'');document.querySelector('#start').disabled=s.running;document.querySelector('#stop').disabled=!s.running;document.querySelector('#bar').style.width=s.running?Math.min(95,12+s.states*2)+'%':(s.error?'100%':'0%');const e=await fetch('/api/events');for(const item of await e.json())addLine(item);if(s.output){const g=await fetch('/api/graph');drawGraph(await g.json())}}catch(e){}}
async function loadDevices(){const select=document.querySelector('#device'),hint=document.querySelector('#deviceHint');try{const response=await fetch('/api/devices');if(!response.ok)throw new Error('ADB 不可用');const devices=await response.json();select.innerHTML='<option value="">自动选择唯一设备</option>';for(const device of devices){const option=document.createElement('option');option.value=device.serial;option.textContent=[device.serial,device.model||device.product||'Android'].join(' · ');select.appendChild(option)}hint.textContent=devices.length?`发现 ${devices.length} 台已授权设备`:'未发现设备；请启动模拟器或连接并授权设备'}catch(error){select.innerHTML='<option value="">自动选择（需要唯一设备）</option>';hint.textContent='无法读取 ADB 设备；检查 Android SDK 与 ADB 连接'}}
form.addEventListener('submit',async ev=>{ev.preventDefault();const body=Object.fromEntries(new FormData(form));const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json','X-App-Graph-Token':apiToken},body:JSON.stringify(body)});const result=await r.json();addLine({time:Date.now()/1000,message:result.message,kind:result.ok?'complete':'error'});refresh()});document.querySelector('#stop').addEventListener('click',async()=>{await fetch('/api/stop',{method:'POST',headers:{'X-App-Graph-Token':apiToken}});refresh()});fetch('/api/token').then(r=>r.json()).then(v=>{apiToken=v.token;refresh()});loadDevices();setInterval(refresh,1000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    service: ExplorerService

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _is_local_origin(self) -> bool:
        host_header = self.headers.get("Host", "").strip()
        try:
            host = urlparse(f"//{host_header}").hostname or ""
            is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
        if not is_loopback:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            origin_host = (parsed.hostname or "").lower()
            if origin_host not in {"127.0.0.1", "localhost", "::1"}:
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._is_local_origin():
            self._json({"error": "local requests only"}, 403)
            return
        if path == "/":
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/token":
            if not self._is_local_origin():
                self._json({"error": "local requests only"}, 403)
                return
            self._json({"token": self.service.control_token()})
        elif path == "/api/status":
            self._json(self.service.snapshot())
        elif path == "/api/events":
            self._json(self.service.drain_events())
        elif path == "/api/graph":
            self._json(self.service.graph_data())
        elif path == "/api/devices":
            try:
                self._json(AndroidDevice().list_devices())
            except Exception as exc:
                self._json({"error": str(exc)}, 503)
        elif path == "/api/asset":
            query = parse_qs(urlparse(self.path).query)
            asset = self.service.screenshot(query.get("path", [""])[0])
            if asset is None:
                self._json({"error": "not found"}, 404)
            else:
                body = asset.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._is_local_origin():
            self._json({"error": "local requests only"}, 403)
            return
        if path == "/api/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 64_000:
                    self._json({"ok": False, "message": "Request body too large."}, 413)
                    return
                values = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(values, dict):
                    raise ValueError("Expected a JSON object")
                if self.service.api_token is None:
                    self._json({"ok": False, "message": "Load the local control page first."}, 403)
                    return
                ok, message = self.service.start(
                    {str(k): str(v) for k, v in values.items()},
                    self.headers.get("X-App-Graph-Token"),
                )
                self._json({"ok": ok, "message": message}, 200 if ok else 400)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"ok": False, "message": f"Invalid request: {exc}"}, 400)
        elif path == "/api/stop":
            if self.service.api_token is None:
                self._json({"ok": False, "message": "Load the local control page first."}, 403)
                return
            ok = self.service.stop(self.headers.get("X-App-Graph-Token"))
            self._json({"ok": ok}, 200 if ok else 403)
        else:
            self._json({"error": "not found"}, 404)


def serve(port: int = 0) -> None:
    service = ExplorerService()
    handler = type("ExplorerHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"App Graph Explorer: http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
