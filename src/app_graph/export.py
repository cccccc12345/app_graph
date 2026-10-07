"""Export the persisted graph as portable JSON, Graphviz DOT, and HTML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database import GraphDatabase

_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Android App Graph</title><style>
:root{--bg:#f8fafc;--ink:#172033;--muted:#64748b;--line:#94a3b8;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:5;padding:12px 20px;background:#fff;border-bottom:1px solid #e2e8f0;display:flex;gap:16px;align-items:center}
h1{font-size:18px;margin:0}#stats{color:var(--muted)}
.tools{margin-left:auto;display:flex;gap:6px;align-items:center;color:var(--muted);font-size:12px}
.tools button{width:30px;height:30px;border:1px solid #d5deeb;background:#fff;border-radius:8px;font:16px/1 inherit;cursor:pointer;color:var(--ink)}
.tools button.wide{width:auto;padding:0 10px;font-size:12px}
.tools button:hover{background:#eef2f8}
main{padding:20px;overflow:auto}
#viewport{position:relative;height:74vh;min-height:460px;overflow:hidden;background:#fff;border:1px solid #e2e8f0;border-radius:12px;cursor:grab;touch-action:none}
#viewport.dragging{cursor:grabbing}
#map{position:absolute;left:0;top:0;transform-origin:0 0;margin:0}
#edges{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.edge{stroke:var(--line);stroke-width:1.5;fill:none;marker-end:url(#arrow);stroke-opacity:.55;pointer-events:stroke;cursor:pointer}
.edge:hover{stroke:#2563eb;stroke-opacity:1;stroke-width:2.5}
.edge-label{font-size:11px;fill:#475569;paint-order:stroke;stroke:var(--bg);stroke-width:4px;stroke-linejoin:round;pointer-events:none}
.node{position:absolute;width:190px;height:300px;display:flex;flex-direction:column;background:var(--card);border:1px solid #cbd5e1;border-top:4px solid var(--line);border-radius:10px;overflow:hidden;box-shadow:0 2px 8px #0f172a12;cursor:pointer}
.node:hover{border-color:#2563eb;box-shadow:0 6px 18px #2563eb2e}
.node img{display:block;width:100%;height:250px;flex:0 0 250px;object-fit:contain;background:#0f172a;pointer-events:none}
.meta{padding:7px 10px;flex:1;min-height:0}
.meta strong{display:block;font-size:13px}
.meta small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#transitions{margin-top:35px;max-width:1000px}
#transitions h2{font-size:16px}.transition{padding:8px 10px;border-bottom:1px solid #e2e8f0}
code{color:#1d4ed8}
#modal{position:fixed;inset:0;z-index:30;background:#0f172ab8;display:flex;align-items:center;justify-content:center;padding:24px}
#modal[hidden]{display:none}
#modal .panel{position:relative;background:#fff;border-radius:14px;max-width:960px;width:100%;max-height:90vh;overflow:auto;padding:20px}
.close{position:absolute;top:8px;right:12px;border:0;background:transparent;font-size:24px;line-height:1;cursor:pointer;color:var(--muted)}
.detail-row{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;margin:10px 0}
.detail-row img{width:220px;border-radius:8px;border:1px solid var(--line);display:block}
.detail-row figure{margin:0}
figcaption{font-size:12px;color:var(--muted);text-align:center;margin-top:4px}
.detail-info p{margin:4px 0;font-size:13px;word-break:break-all}
.detail-edges{display:flex;flex-direction:column;gap:4px;max-height:220px;overflow:auto;padding-right:4px}
.edge-link{text-align:left;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;padding:5px 8px;font:12px/1.4 inherit;cursor:pointer;color:var(--ink)}
.edge-link:hover{background:#e0e7ff}
h4{margin:14px 0 6px;font-size:14px}
</style></head><body>
<header><h1>Android App Graph</h1><span id="stats"></span>
<div class="tools"><button id="zoomOut" title="缩小">−</button><button id="zoomIn" title="放大">+</button><button id="fit" class="wide" title="适应窗口">适应</button><span id="zoomLevel">100%</span></div></header>
<main><div id="viewport"><div id="map"></div></div><section id="transitions"><h2>Transitions</h2><div id="list"></div></section></main>
<div id="modal" hidden><div class="panel"><button class="close" id="modalClose">×</button><div id="modalBody"></div></div></div>
<script>
const graph=__PAYLOAD__;
const map=document.querySelector('#map');
const states=graph.states,edges=graph.edges;
document.querySelector('#stats').textContent=`${states.length} states · ${edges.length} transitions`;
const nodeW=190,nodeH=300;
let seed=7;const rnd=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296};
const nodes=states.map((s,i)=>{const angle=i*2.399963,r=70*Math.sqrt(i+1);return{id:s.id,state:s,x:Math.cos(angle)*r+(rnd()-.5)*30,y:Math.sin(angle)*r+(rnd()-.5)*30,vx:0,vy:0}});
const index=new Map(nodes.map(n=>[n.id,n]));
const links=edges.map(e=>({a:index.get(e.from),b:index.get(e.to),edge:e})).filter(l=>l.a&&l.b);
const iterations=Math.min(600,120+nodes.length*6);
for(let it=0;it<iterations;it++){
  const alpha=1-it/iterations;
  for(let i=0;i<nodes.length;i++){const a=nodes[i];for(let j=i+1;j<nodes.length;j++){const b=nodes[j];let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy;if(d2<1){dx=rnd()-.5;dy=rnd()-.5;d2=1}if(d2>800*800)continue;const d=Math.sqrt(d2),f=1600000/d2;const fx=dx/d*f,fy=dy/d*f;a.vx+=fx;a.vy+=fy;b.vx-=fx;b.vy-=fy}}
  for(const l of links){const dx=l.b.x-l.a.x,dy=l.b.y-l.a.y,d=Math.hypot(dx,dy)||1,f=(d-340)*0.02;const fx=dx/d*f,fy=dy/d*f;l.a.vx+=fx;l.a.vy+=fy;l.b.vx-=fx;l.b.vy-=fy}
  for(const n of nodes){n.vx-=n.x*0.004;n.vy-=n.y*0.004;n.vx*=0.82;n.vy*=0.82;n.x+=n.vx*alpha;n.y+=n.vy*alpha}
}
for(let pass=0;pass<100;pass++){let moved=false;for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j];const ox=nodeW+26-Math.abs(a.x-b.x),oy=nodeH+26-Math.abs(a.y-b.y);if(ox>0&&oy>0){moved=true;if(ox<oy){const p=ox/2*(a.x<b.x?-1:1);a.x+=p;b.x-=p}else{const p=oy/2*(a.y<b.y?-1:1);a.y+=p;b.y-=p}}}if(!moved)break}
let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
for(const n of nodes){minX=Math.min(minX,n.x-nodeW/2);minY=Math.min(minY,n.y-nodeH/2);maxX=Math.max(maxX,n.x+nodeW/2);maxY=Math.max(maxY,n.y+nodeH/2)}
const pad=34;
if(nodes.length){for(const n of nodes){n.x+=pad-minX;n.y+=pad-minY}}
map.style.width=((nodes.length?maxX-minX:0)+pad*2)+'px';map.style.height=((nodes.length?maxY-minY:0)+pad*2)+'px';
const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
svg.setAttribute('id','edges');svg.setAttribute('width',map.style.width);svg.setAttribute('height',map.style.height);
svg.innerHTML='<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#94a3b8"/></marker></defs>';
map.appendChild(svg);
const palette=['#2563eb','#0891b2','#16a34a','#d97706','#dc2626','#7c3aed','#db2777','#65a30d'];
function depthColor(depth){return palette[((depth%palette.length)+palette.length)%palette.length]}
function clip(node,dx,dy){const sx=dx===0?Infinity:(nodeW/2+8)/Math.abs(dx);const sy=dy===0?Infinity:(nodeH/2+8)/Math.abs(dy);const s=Math.min(sx,sy);return{x:node.x+dx*s,y:node.y+dy*s}}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const statesById=new Map(states.map(s=>[s.id,s]));
for(const n of nodes){const card=document.createElement('article');card.className='node';card.style.left=(n.x-nodeW/2)+'px';card.style.top=(n.y-nodeH/2)+'px';card.style.borderTopColor=depthColor(n.state.depth);card.title='点击查看 State '+n.id+' 详情';const img=document.createElement('img');img.loading='lazy';img.src=n.state.screenshot_path;img.alt='State '+n.id+' screenshot';card.appendChild(img);const meta=document.createElement('div');meta.className='meta';meta.innerHTML='<strong>State '+n.id+'</strong><small>Depth '+n.state.depth+' · Visits '+n.state.visit_count+'</small><small title="'+escapeHtml(n.state.activity||'')+'">'+escapeHtml(n.state.activity||'')+'</small>';card.appendChild(meta);card.addEventListener('click',()=>{if(!moved)openModal(body=>buildStateDetails(body,n.id))});map.appendChild(card)}
function addLabel(x,y,text){const label=document.createElementNS('http://www.w3.org/2000/svg','text');label.setAttribute('class','edge-label');label.setAttribute('x',x);label.setAttribute('y',y);label.setAttribute('text-anchor','middle');label.textContent=text;svg.appendChild(label)}
function addTip(path,text){const tip=document.createElementNS('http://www.w3.org/2000/svg','title');tip.textContent=text;path.appendChild(tip)}
const showEdgeLabels=links.length<=100;
for(const l of links){const action=l.edge.action;const label=action.text||action.content_desc||action.kind||'action';const description='State '+l.edge.from+' — '+label+' → State '+l.edge.to;let path;if(l.a===l.b){const cx=l.a.x,top=l.a.y-nodeH/2;path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class','edge');path.setAttribute('d',`M${cx-55},${top+26} C${cx-75},${top-68} ${cx+75},${top-68} ${cx+55},${top+26}`);if(showEdgeLabels)addLabel(cx,top-38,label)}else{const dx=l.b.x-l.a.x,dy=l.b.y-l.a.y;const p1=clip(l.a,dx,dy),p2=clip(l.b,-dx,-dy);const nx=-dy,ny=dx,nl=Math.hypot(nx,ny)||1;const bend=Math.min(70,Math.hypot(dx,dy)*0.12);const cx=(p1.x+p2.x)/2+nx/nl*bend,cy=(p1.y+p2.y)/2+ny/nl*bend;path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class','edge');path.setAttribute('d',`M${p1.x},${p1.y} Q${cx},${cy} ${p2.x},${p2.y}`);if(showEdgeLabels)addLabel(cx,cy-6,label)}addTip(path,description);path.addEventListener('click',event=>{event.stopPropagation();if(!moved)openModal(body=>buildEdgeDetails(body,l.edge))});svg.appendChild(path)}

const viewport=document.querySelector('#viewport'),zoomLevel=document.querySelector('#zoomLevel');
let scale=1,tx=0,ty=0,moved=false;
function applyTransform(){map.style.transform=`translate(${tx}px,${ty}px) scale(${scale})`;zoomLevel.textContent=Math.round(scale*100)+'%'}
function fit(){const vw=viewport.clientWidth,vh=viewport.clientHeight;const w=parseFloat(map.style.width)||1,h=parseFloat(map.style.height)||1;scale=Math.min(vw/w,vh/h,1);tx=(vw-w*scale)/2;ty=(vh-h*scale)/2;applyTransform()}
function zoomBy(factor){const mx=viewport.clientWidth/2,my=viewport.clientHeight/2;const next=Math.min(4,Math.max(0.08,scale*factor));const k=next/scale;tx=mx-(mx-tx)*k;ty=my-(my-ty)*k;scale=next;applyTransform()}
viewport.addEventListener('wheel',event=>{event.preventDefault();const rect=viewport.getBoundingClientRect();const mx=event.clientX-rect.left,my=event.clientY-rect.top;const factor=event.deltaY<0?1.15:1/1.15;const next=Math.min(4,Math.max(0.08,scale*factor));const k=next/scale;tx=mx-(mx-tx)*k;ty=my-(my-ty)*k;scale=next;applyTransform()},{passive:false});
let dragging=false,lastX=0,lastY=0;
viewport.addEventListener('pointerdown',event=>{if(event.button!==0)return;dragging=true;moved=false;lastX=event.clientX;lastY=event.clientY;viewport.classList.add('dragging');viewport.setPointerCapture(event.pointerId)});
viewport.addEventListener('pointermove',event=>{if(!dragging)return;const dx=event.clientX-lastX,dy=event.clientY-lastY;if(Math.abs(dx)+Math.abs(dy)>3)moved=true;tx+=dx;ty+=dy;lastX=event.clientX;lastY=event.clientY;applyTransform()});
viewport.addEventListener('pointerup',()=>{dragging=false;viewport.classList.remove('dragging')});
viewport.addEventListener('pointercancel',()=>{dragging=false;viewport.classList.remove('dragging')});
document.querySelector('#zoomIn').addEventListener('click',()=>zoomBy(1.25));
document.querySelector('#zoomOut').addEventListener('click',()=>zoomBy(1/1.25));
document.querySelector('#fit').addEventListener('click',fit);
window.addEventListener('resize',fit);
fit();

const modal=document.querySelector('#modal'),modalBody=document.querySelector('#modalBody');
function openModal(build){modalBody.innerHTML='';build(modalBody);modal.hidden=false}
function closeModal(){modal.hidden=true}
document.querySelector('#modalClose').addEventListener('click',closeModal);
modal.addEventListener('click',event=>{if(event.target===modal)closeModal()});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeModal()});
function addInfo(info,key,value){const p=document.createElement('p');const strong=document.createElement('b');strong.textContent=key+': ';p.appendChild(strong);p.appendChild(document.createTextNode(String(value==null?'':value)));info.appendChild(p)}
function stateThumb(id,width){const state=statesById.get(id);const figure=document.createElement('figure');if(state){const img=document.createElement('img');img.src=state.screenshot_path;img.alt='State '+id;if(width)img.style.width=width+'px';figure.appendChild(img)}const cap=document.createElement('figcaption');cap.textContent='State '+id;figure.appendChild(cap);return figure}
function buildStateDetails(body,id){const state=statesById.get(id);if(!state){addInfo(body,'Error','State '+id+' not found');return}const title=document.createElement('h3');title.textContent='State '+state.id;body.appendChild(title);const row=document.createElement('div');row.className='detail-row';row.appendChild(stateThumb(id,240));const info=document.createElement('div');info.className='detail-info';addInfo(info,'Depth',state.depth);addInfo(info,'Visits',state.visit_count);addInfo(info,'Activity',state.activity||'');addInfo(info,'Screenshot',state.screenshot_path);addInfo(info,'Hierarchy',state.xml_path);row.appendChild(info);body.appendChild(row);if(state.variants&&state.variants.length){const heading=document.createElement('h4');heading.textContent='Variants ('+state.variants.length+')';body.appendChild(heading);const variants=document.createElement('div');variants.className='detail-row';for(const variant of state.variants.slice(0,12)){const figure=document.createElement('figure');const img=document.createElement('img');img.src=variant.screenshot_path;img.alt='variant';img.style.width='140px';figure.appendChild(img);const cap=document.createElement('figcaption');cap.textContent=variant.created_at;figure.appendChild(cap);variants.appendChild(figure)}body.appendChild(variants)}const outgoing=edges.filter(e=>e.from===id),incoming=edges.filter(e=>e.to===id);const section=(name,list)=>{const heading=document.createElement('h4');heading.textContent=name+' ('+list.length+')';body.appendChild(heading);const box=document.createElement('div');box.className='detail-edges';for(const edge of list.slice(0,120)){const button=document.createElement('button');button.className='edge-link';const action=edge.action;const label=action.text||action.content_desc||action.kind||'action';button.textContent='#'+edge.from+' — '+label+' → #'+edge.to;button.addEventListener('click',()=>openModal(next=>buildEdgeDetails(next,edge)));box.appendChild(button)}body.appendChild(box);if(list.length>120){const more=document.createElement('p');more.textContent='…共 '+list.length+' 条';body.appendChild(more)}};section('Outgoing',outgoing);section('Incoming',incoming)}
function buildEdgeDetails(body,edge){const action=edge.action;const title=document.createElement('h3');title.textContent='Action '+edge.from+' → '+edge.to;body.appendChild(title);const row=document.createElement('div');row.className='detail-row';row.appendChild(stateThumb(edge.from,180));row.appendChild(stateThumb(edge.to,180));body.appendChild(row);const info=document.createElement('div');info.className='detail-info';for(const key of ['kind','text','content_desc','resource_id','x','y','x2','y2','duration_ms'])addInfo(info,key,action[key]);if(action.bounds)addInfo(info,'bounds',JSON.stringify(action.bounds));body.appendChild(info);const back=document.createElement('button');back.className='edge-link';back.textContent='查看起点 State '+edge.from+' 详情';back.addEventListener('click',()=>openModal(next=>buildStateDetails(next,edge.from)));body.appendChild(back);const to=document.createElement('button');to.className='edge-link';to.textContent='查看终点 State '+edge.to+' 详情';to.style.marginLeft='6px';to.addEventListener('click',()=>openModal(next=>buildStateDetails(next,edge.to)));body.appendChild(to)}

const list=document.querySelector('#list');for(const e of edges){const row=document.createElement('div');row.className='transition';const ac=e.action;row.innerHTML='<code>State '+e.from+'</code> — '+escapeHtml(ac.text||ac.content_desc||ac.kind)+' → <code>State '+e.to+'</code>';list.appendChild(row)}
</script></body></html>
"""


def export_graph(db: GraphDatabase, output_dir: str | Path) -> dict[str, Path]:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    data = db.graph_data()
    json_path = output / "graph.json"
    dot_path = output / "graph.dot"
    html_path = output / "graph.html"

    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    dot_path.write_text(_to_dot(data), encoding="utf-8")
    html_path.write_text(_to_html(data), encoding="utf-8")
    return {"json": json_path, "dot": dot_path, "html": html_path}


def _to_dot(data: dict[str, Any]) -> str:
    lines = ["digraph AppGraph {", "  rankdir=LR;", '  node [shape=box, style="rounded,filled", fillcolor="#eff6ff"];']
    for state in data["states"]:
        state_id = state["id"]
        lines.append(f'  s{state_id} [label="State {state_id}\\nDepth {state["depth"]}"];')
    for edge in data["edges"]:
        action = edge["action"]
        label = action.get("text") or action.get("content_desc") or action.get("kind", "action")
        label = label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines.append(f'  s{edge["from"]} -> s{edge["to"]} [label="{label}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _to_html(data: dict[str, Any]) -> str:
    # All assets are local relative paths, so graph.html remains usable offline.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__PAYLOAD__", payload)
