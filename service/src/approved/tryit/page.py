"""The try-it page: one static document, its style and script inline and pinned by hash.

The script and style are constants, so their sha256 hashes go in the page's
Content-Security-Policy (no ``'unsafe-inline'``). Nothing dynamic is interpolated into the
HTML except the escaped base-path meta value; everything the page shows comes from
``api/state`` as JSON and is written with ``textContent``. Every URL is relative (or built from
the base path), so the page works at ``/`` locally, behind Maritime's ``/a/<agent-id>/`` prefix,
and inside an iframe on approval.md. It sets no cookie and needs none; it polls.
"""

from __future__ import annotations

import base64
import hashlib
import html

__all__ = ["PAGE_SCRIPT", "PAGE_STYLE", "csp_hash", "render_page"]

PAGE_STYLE = """
:root{--bg:#f4f5f7;--panel:#fff;--fg:#17202a;--muted:#5b6673;--line:#d9dde3;--accent:#2f6fdf;
--ok:#1e7d4f;--ok-bg:#e3f4ea;--no:#b3261e;--no-bg:#fbe7e5;--warn:#8a5a00;--warn-bg:#fdf1d6;
--info:#1f5fbf;--info-bg:#e5eefc;--chip:#eceff3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--panel:#171d24;--fg:#e6ebf1;--muted:#98a4b2;
--line:#2a333d;--accent:#6c9cf0;--ok:#6fd49c;--ok-bg:#15301f;--no:#f08c84;--no-bg:#3a1a18;
--warn:#f0c46a;--warn-bg:#35290f;--info:#8fb4f5;--info-bg:#17263d;--chip:#232b35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:16px 16px 4px;max-width:1200px;margin:0 auto}
h1{margin:0;font-size:20px}h2{margin:0 0 8px;font-size:15px}
.lede{margin:6px 0 0;color:var(--muted);max-width:70ch}
html.framed .lede{display:none}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.badge{border-radius:999px;padding:3px 10px;font-size:12.5px;background:var(--chip)}
.badge.mode{background:var(--info-bg);color:var(--info)}
.badge.fallback{background:var(--warn-bg);color:var(--warn)}
.badge.shared{background:var(--warn-bg);color:var(--warn)}
main{display:grid;gap:12px;padding:12px 16px 16px;max-width:1200px;margin:0 auto;
grid-template-columns:minmax(0,1fr);grid-template-areas:"controls" "chat" "run" "record"}
@media (min-width:820px){main{grid-template-columns:minmax(0,1fr) minmax(0,1fr);
grid-template-areas:"controls chat" "run chat" "record chat"}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
#controls{grid-area:controls}#run{grid-area:run}#record{grid-area:record}
#chatcard{grid-area:chat;display:flex;flex-direction:column;min-height:420px}
#chat{flex:1;width:100%;min-height:420px;border:0;border-radius:8px;background:var(--bg)}
@media (min-width:820px){#chat{min-height:640px}}
button{border:0;border-radius:8px;padding:9px 16px;font:inherit;color:#fff;background:var(--accent);cursor:pointer}
button.secondary{background:var(--chip);color:var(--fg)}
button:disabled{opacity:.55;cursor:default}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#notice{margin:8px 0 0;min-height:1.3em;color:var(--muted);font-size:13.5px}
#notice.bad{color:var(--no)}
.hint{color:var(--muted);font-size:13px;margin:8px 0 0}
ol.scen{list-style:none;margin:0;padding:0}
ol.scen li{border-top:1px solid var(--line);padding:8px 0}
ol.scen li:first-child{border-top:0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;word-break:break-all}
.chip{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600;background:var(--chip);margin-right:6px}
.chip.allowed,.chip.agree,.chip.granted,.chip.clean,.chip.verified{background:var(--ok-bg);color:var(--ok)}
.chip.rejected,.chip.disagree,.chip.blocked,.chip.bad{background:var(--no-bg);color:var(--no)}
.chip.expired,.chip.timed-out,.chip.escalated,.chip.absent{background:var(--warn-bg);color:var(--warn)}
.chip.waiting,.chip.running,.chip.reviewing{background:var(--info-bg);color:var(--info)}
.judge{margin:4px 0 0;font-size:13px;color:var(--muted)}
.judge a{color:var(--accent)}
pre#lines{margin:10px 0 0;padding:8px;max-height:180px;overflow:auto;background:var(--bg);border-radius:6px;
font:12px/1.4 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:0}
dt{color:var(--muted)}dd{margin:0}
.tablewrap{overflow-x:auto;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
td,th{overflow-wrap:anywhere}
th:first-child,td:first-child{width:3em}
th,td{text-align:left;padding:5px 4px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}
footer{max-width:1200px;margin:0 auto;padding:0 16px 20px;color:var(--muted);font-size:12.5px}
""".strip()

PAGE_SCRIPT = """
(function(){
"use strict";
var meta=document.querySelector('meta[name="base-path"]');
var base=meta&&meta.content?meta.content.replace(/\\/+$/,"")+"/":
  (location.pathname.slice(-1)==="/"?location.pathname:location.pathname+"/");
try{if(window.self!==window.top){document.documentElement.classList.add("framed");}}catch(e){document.documentElement.classList.add("framed");}
var TRACE=/^https:\\/\\/wandb\\.ai\\/[A-Za-z0-9._-]+\\/[A-Za-z0-9._-]+\\/r\\/call\\/[A-Za-z0-9-]+$/;
var $=function(id){return document.getElementById(id);};
$("chat").src=base+"approver/";
function el(tag,cls,text){var e=document.createElement(tag);if(cls){e.className=cls;}if(text!==undefined&&text!==null){e.textContent=String(text);}return e;}
function chip(text,cls){return el("span","chip "+(cls||text),text);}
function traceLink(url){if(!url||!TRACE.test(url)){return null;}var a=el("a",null,"Weave trace");a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;}
function notice(text,bad){var n=$("notice");n.textContent=text||"";n.className=bad?"bad":"";}
var LABEL={pending:"pending",running:"running",waiting:"waiting for your tap",allowed:"allowed",rejected:"rejected",
  expired:"expired",blocked:"blocked","timed-out":"no decision","not-run":"not run"};
function judgeLine(j){
  var p=el("p","judge");
  if(!j){return null;}
  if(j.state==="verdict"){p.appendChild(document.createTextNode("AI judge (advisory): "));p.appendChild(chip(j.verdict,j.verdict==="READY"?"allowed":"escalated"));
    if(j.note){p.appendChild(document.createTextNode(j.note+" "));}
    var a=traceLink(j.trace_url);if(a){p.appendChild(a);}else if(j.mode==="offline"){p.appendChild(document.createTextNode("(offline reviewer: no trace)"));}}
  else if(j.state==="absent"){p.appendChild(chip("judge absent","absent"));p.appendChild(document.createTextNode((j.reason?j.reason+"; ":"")+"no advisory for this one, and your tap still decides."));}
  else if(j.state==="reviewing"){p.appendChild(chip("reviewing","reviewing"));p.appendChild(document.createTextNode("the AI judge is reading the request"));}
  else{p.textContent="AI judge: not seen yet (it reads each request within a few seconds)";}
  return p;
}
function renderRun(run){
  var list=$("scenarios");list.textContent="";
  if(!run){list.appendChild(el("li",null,"No run yet. Press Run the agent."));$("lines").textContent="";$("runstate").textContent="";return;}
  $("runstate").textContent=run.state==="done"?(run.end_reason||"Run finished."):(run.state==="preparing"?"Preparing the judge\\u2026":"Running. Answer each prompt in the approver chat.");
  run.scenarios.forEach(function(s){
    var li=el("li");var head=el("div");head.appendChild(chip(LABEL[s.status]||s.status,s.status));
    head.appendChild(el("code",null,s.command));li.appendChild(head);
    li.appendChild(el("div","judge",s.why));
    if(s.detail&&s.status!=="allowed"){li.appendChild(el("div","judge",s.detail));}
    var j=judgeLine(s.judge);if(j){li.appendChild(j);}
    list.appendChild(li);
  });
  $("lines").textContent=(run.lines||[]).join("\\n");
}
function renderRecord(rec){
  var dl=$("chain");dl.textContent="";
  function row(k,v){dl.appendChild(el("dt",null,k));var dd=el("dd");if(v instanceof Node){dd.appendChild(v);}else{dd.textContent=v;}dl.appendChild(dd);}
  var jc=rec.judge_chain||"unknown";row("Judge's follow of the log",chip(jc,jc==="verified"?"verified":(jc==="chain-break"?"bad":"")));
  var lv=rec.log_verify||{};var d=el("span");d.appendChild(chip(lv.status||"unknown",lv.status==="clean"?"clean":(lv.status==="not-checked-yet"?"":"bad")));
  if(lv.records!==undefined&&lv.records!==null){d.appendChild(document.createTextNode(lv.records+" hash-chained records"));}
  row("approval log verify",d);
  var c=rec.counters||{};var cs=el("span");
  cs.appendChild(chip((c.agree||0)+" agree","agree"));cs.appendChild(chip((c.escalated||0)+" escalated","escalated"));cs.appendChild(chip((c.disagree||0)+" disagree","disagree"));
  row("Judge vs. human",cs);
  if(c.false_ready!==undefined){row("False READY",String(c.false_ready)+" (READY on a request the human rejected)");}
  var tb=$("recent");tb.textContent="";
  (rec.recent||[]).forEach(function(r){
    var tr=el("tr");tr.appendChild(el("td",null,r.seq));tr.appendChild(el("td",null,r.action_class||"not reviewed"));
    var h=el("td");h.appendChild(chip(r.human||r.event,r.human||"expired"));tr.appendChild(h);
    var v=el("td");v.textContent=r.verdict||("absent"+(r.advisory&&r.advisory.indexOf("absent:")===0?" ("+r.advisory.slice(7)+")":""));
    var a=traceLink(r.trace_url);if(a){v.appendChild(document.createTextNode(" "));v.appendChild(a);}tr.appendChild(v);
    var l=el("td");if(r.label){l.appendChild(chip(r.label,r.label));}else{l.textContent="\\u2014";}tr.appendChild(l);
    tb.appendChild(tr);
  });
  if(!(rec.recent||[]).length){var tr=el("tr");var td=el("td",null,"No decisions recorded yet.");td.colSpan=5;tr.appendChild(td);tb.appendChild(tr);}
}
var busy=false;
function render(s){
  var m=$("mode");m.textContent=s.reviewer.label;m.className="badge "+(s.reviewer.fallback?"fallback":"mode");
  $("shared").textContent=s.shared_note;
  $("window").textContent="Each prompt waits up to "+Math.round(s.window_s/60)+" min; an unanswered prompt expires (the gate records it) and ends the run.";
  var active=s.run&&s.run.state!=="done";
  $("run-btn").disabled=busy||active||!s.health.ok;
  $("reset-btn").disabled=busy||active;
  if(!s.health.ok){notice("Starting up: "+Object.keys(s.health.parts).filter(function(k){return !s.health.parts[k];}).join(", ")+" not ready yet.");}
  renderRun(s.run);renderRecord(s.record);
}
function poll(){fetch(base+"api/state",{cache:"no-store"}).then(function(r){return r.json();}).then(render).catch(function(){});}
function post(path){
  busy=true;
  return fetch(base+path,{method:"POST",headers:{"content-type":"application/json"},body:"{}"})
    .then(function(r){return r.json().then(function(b){notice(b.message||"",!r.ok);});})
    .catch(function(){notice("The demo did not answer; try again.",true);})
    .then(function(){busy=false;poll();});
}
$("run-btn").addEventListener("click",function(){post("api/run");});
$("reset-btn").addEventListener("click",function(){post("api/reset").then(function(){$("chat").src=base+"approver/";});});
poll();setInterval(poll,1500);
})();
""".strip()

_BODY = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="base-path" content="{base}">
<title>Approved: try it</title>
<style>{style}</style></head><body>
<header>
<h1>Approved: try it live</h1>
<p class="lede">A scripted AI agent asks to run four commands. The approval.md gate lets the read through and holds each push for a human; an AI judge posts an advisory beside each prompt. You are the human: tap Approve or Reject in the approver chat, and every decision lands in a hash-chained log.</p>
<div class="badges"><span class="badge mode" id="mode">reviewer</span><span class="badge shared" id="shared">one shared demo tenant; other visitors may be tapping too</span></div>
</header>
<main>
<section class="card" id="controls" aria-label="Controls">
<div class="row"><button id="run-btn" type="button">Run the agent</button><button id="reset-btn" type="button" class="secondary">Reset view</button></div>
<p id="notice" role="status"></p>
<p class="hint" id="window"></p>
<p class="hint">Prompts reach the chat on the gate's 30-second dispatch cycle, and the AI judge's advisory usually lands beside each one within seconds. The judge is advisory: it never approves or blocks anything, and you may tap before it speaks. Only your tap decides. Reset clears this view; it never touches the log.</p>
</section>
<section class="card" id="chatcard" aria-label="Approver chat"><iframe id="chat" title="Approver chat (fake Telegram)"></iframe></section>
<section class="card" id="run" aria-label="The agent"><h2>The agent</h2><p class="hint" id="runstate"></p><ol class="scen" id="scenarios"></ol><pre id="lines"></pre></section>
<section class="card" id="record" aria-label="The record"><h2>The record</h2><dl id="chain"></dl>
<div class="tablewrap"><table><thead><tr><th>seq</th><th>class</th><th>human</th><th>judge</th><th>label</th></tr></thead><tbody id="recent"></tbody></table></div></section>
</main>
<footer>Hermetic demo: a fake Telegram, throwaway credentials generated when the container starts, and a scripted agent that runs only its own four fixed commands against a demo store. Nothing you type reaches the agent.</footer>
<script>{script}</script>
</body></html>
"""


def csp_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def render_page(base_path: str) -> str:
    return _BODY.format(
        base=html.escape(base_path, quote=True), style=PAGE_STYLE, script=PAGE_SCRIPT
    )
