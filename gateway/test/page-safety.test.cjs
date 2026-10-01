'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../service/src/approved/tryit/page.py'),'utf8');
const match=source.match(/PAGE_SCRIPT = r"""([\s\S]*?)"""/);
assert.ok(match,'page script found');
const script=match[1].replace(/\}\)\(\);\s*$/, 'globalThis.__pageTest={plainTelegram,traceLink,status,renderRun,renderChat,renderTelemetry,clearSession,openPolicy,setTokenForTest(value){token=value;},begin,poll,run};})();');
const nodes=new Map();
const document={
  querySelector(){return null;},
  getElementById(id){if(!nodes.has(id))nodes.set(id,{children:[],style:{},open:false,addEventListener(){},replaceChildren(...items){this.children=items;},appendChild(n){this.children.push(n);},append(...items){this.children.push(...items);},querySelector(sel){return this.children.find(n=>sel==='details.history'?n.className==='history':n.className==='current-payload');},setAttribute(){},getBoundingClientRect(){return {top:340};},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(options){this.lastScrollOptions=options;},focus(options){this.lastFocusOptions=options;},textContent:'',hidden:false});return nodes.get(id);},
  createElement(tag){return {tagName:tag,children:[],textContent:'',className:'',addEventListener(){},appendChild(n){this.children.push(n);},append(...items){this.children.push(...items);}};},
};
const parentMessages=[];
const context={document,window:{innerHeight:844,parent:{postMessage(data,origin){parentMessages.push({data,origin});}}},requestAnimationFrame(cb){cb();},location:{href:'https://gateway.example/'},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},URL,setInterval(){}};
vm.runInNewContext(script,context);
test('chat formatting becomes inert readable text',()=>{
  assert.equal(context.__pageTest.plainTelegram('<b>Approval</b> &lt;required&gt; <script>alert(1)</script>'),'Approval <required> alert(1)');
});
test('trace links accept only a Weave call on the fixed host',()=>{
  const {traceLink}=context.__pageTest;
  assert.equal(traceLink('javascript:alert(1)'),null);
  assert.equal(traceLink('https://wandb.ai.evil.example/a/b/r/call/x'),null);
  const link=traceLink('https://wandb.ai/bountify/judgy/r/call/01a0f12b-bee2-7124-8bfc-a90f3e670ffa');
  assert.equal(link.href,'https://wandb.ai/bountify/judgy/r/call/01a0f12b-bee2-7124-8bfc-a90f3e670ffa');
  assert.equal(link.rel,'noopener noreferrer');
});
test('page initializes when embedded storage is blocked',()=>{
  const blocked={getItem(){throw Error('storage blocked');},setItem(){throw Error('storage blocked');},removeItem(){throw Error('storage blocked');}};
  const elements=new Map();
  const doc={querySelector(){return null;},getElementById(id){if(!elements.has(id))elements.set(id,{addEventListener(){},replaceChildren(){},appendChild(){},setAttribute(){},textContent:'',hidden:false});return elements.get(id);},createElement(tag){return {tagName:tag,textContent:'',append(){}};}};
  assert.doesNotThrow(()=>vm.runInNewContext(script,{document:doc,location:{href:'https://gateway.example/'},sessionStorage:blocked,URL,setInterval(){}}));
});
test('checked-in gateway shell matches runtime page source',()=>{
  const {execFileSync}=require('node:child_process');
  const root=path.join(__dirname,'../..');
  assert.match(execFileSync('python3',['gateway/scripts/build-page.py','--check'],{cwd:root,encoding:'utf8'}),/matches page.py/);
});
test('current approval context stays visible and payload/history details stay open',()=>{
  const chat=context.__pageTest.renderChat;
  const messages=[
    {bot:'gate',text:'old outcome',buttons:[]},
    {bot:'judge',text:'Judge (advisory AI): vcs.push.main',buttons:[]},
    {bot:'gate',text:'<b>APPROVAL REQUIRED</b>\nClass: vcs.push.main\nCommand: git push origin main\nContext: demo branch',buttons:[]},
    {bot:'gate',text:'<b>PAYLOAD — the canonical rendering this approval display_hash names; raw bytes at the store path inside</b>\nAPPROVAL REQUIRED appears in data',buttons:[]},
    {bot:'gate',text:'<b>WHAT THIS DOES</b>\nMoves main',buttons:[{text:'Approve',data:'g:1:ab'}]},
  ];
  chat({messages});
  const box=nodes.get('chat');
  assert.equal(box.children[0].className,'history');
  assert.match(box.children[2].children[1].textContent,/Class: vcs.push.main/);
  assert.match(box.children[2].children[1].textContent,/Context: demo branch/);
  assert.equal(box.children[3].className,'current-payload');
  assert.match(box.children[3].children[0].textContent,/Canonical request payload/);
  assert.match(box.children[3].children[1].children[1].textContent,/APPROVAL REQUIRED appears in data/);
  box.children[0].open=true;
  box.children[3].open=true;
  chat({messages:[...messages,{bot:'judge',text:'Advisory update',buttons:[]}]});
  assert.equal(box.children[0].open,true);
  assert.equal(box.querySelector('details.current-payload').open,true);
});
test('session telemetry uses measured status and clears after expiry',()=>{
  context.__pageTest.renderTelemetry({
    telemetry:{version:1,session:{ref:'abc123',elapsed_s:42},runtime:{processes:{
      approval_gate:{running:true,healthy:true},approver_chat:{running:true,healthy:false},ai_judge:{running:false,healthy:false},
    }},events:[{kind:'gate_healthy',at:1}]},
    reviewer:{label:'Live reviewer: W&B Inference (model)'},record:{log_verify:{status:'ok',records:3}},
  });
  assert.match(nodes.get('ops-session').textContent,/abc123/);
  assert.match(nodes.get('ops-processes').children[1].children[1].textContent,/unavailable/);
  assert.match(nodes.get('ops-processes').children[0].children[1].className,/status$/);
  assert.match(nodes.get('ops-processes').children[2].children[1].className,/wait/);
  assert.match(context.__pageTest.status('stopped').className,/wait/);
  assert.match(nodes.get('ops-proof').textContent,/3 records/);
  context.__pageTest.clearSession();
  assert.match(nodes.get('ops-session').textContent,/Start a session/);
  assert.equal(nodes.get('ops-processes').children.length,1);
});
test('Maritime health link appears only for configured provider',()=>{
  const base={version:1,session:{ref:'abc123',elapsed_s:1},runtime:{processes:{}},events:[]};
  context.__pageTest.renderTelemetry({telemetry:base});
  assert.equal(nodes.get('ops-host').textContent,'Hosted demo runtime');
  context.__pageTest.renderTelemetry({telemetry:{...base,runtime:{...base.runtime,provider:'Maritime deployment'}}});
  const link=nodes.get('ops-host').children[0];
  assert.equal(link.href,'https://api.maritime.sh/a/65c73318-c0cb-44ee-82ed-717d63b87019/health');
  assert.equal(link.target,'_blank');
  assert.equal(link.rel,'noopener noreferrer');
  context.__pageTest.clearSession();
  assert.equal(nodes.get('ops-host').textContent,'Hosted demo runtime');
});
test('policy modal renders plain current bytes and ignores a stale session response',async()=>{
  const page=context.__pageTest;
  page.setTokenForTest('a'.repeat(32));
  context.Response=Response;
  context.fetch=async()=>new Response(JSON.stringify({path:'/data/tryit/sessions/one/demo/APPROVAL.md',sha256:'a'.repeat(64),text:'<b>read.*</b>'}),{headers:{'content-type':'application/json'}});
  await page.openPolicy();
  assert.equal(nodes.get('policy-dialog').open,true);
  assert.equal(nodes.get('policy-dialog').style.top,'340px');
  assert.equal(nodes.get('policy-dialog').style.maxHeight,'492px');
  assert.equal(nodes.get('policy-dialog').style.height,'492px');
  assert.equal(nodes.get('policy-dialog').scrollTop,0);
  assert.equal(nodes.get('policy-close').lastFocusOptions.preventScroll,true);
  assert.equal(parentMessages.length,2);
  assert.equal(parentMessages[1].data.type,'approved-demo-policy-reveal-v1');
  assert.equal(parentMessages[1].data.y,340);
  assert.equal(parentMessages[1].origin,'https://approval.md');
  assert.equal(nodes.get('policy-path').textContent,'/data/tryit/sessions/one/demo/APPROVAL.md');
  assert.equal(nodes.get('policy-text').textContent,'<b>read.*</b>');
  assert.equal(nodes.get('policy-sha').textContent,'SHA-256 '+'a'.repeat(64));
  let answer;
  context.fetch=()=>new Promise(resolve=>{answer=resolve;});
  const pending=page.openPolicy();
  page.clearSession();
  answer(new Response(JSON.stringify({path:'/data/old',sha256:'b'.repeat(64),text:'old private bytes'}),{headers:{'content-type':'application/json'}}));
  await pending;
  assert.equal(nodes.get('policy-dialog').open,false);
  assert.equal(nodes.get('policy-path').textContent,'');
  assert.equal(nodes.get('policy-text').textContent,'');
  assert.equal(parentMessages.length,3);
});

test('judge absence reason is visible as text while the run waits for a human',()=>{
  context.__pageTest.renderRun({state:'running',scenarios:[{status:'waiting',command:'git push origin main',judge:{state:'absent',reason:'live allowance used up'}}],lines:[]});
  const scenario=nodes.get('scenarios').children[0];
  const explanation=scenario.children.find(n=>n.tagName==='p');
  assert.match(explanation.textContent,/live allowance used up/);
  assert.match(explanation.textContent,/Your tap still decides/);
});

test('cold wake probes health twice and allocates a session once',async()=>{
  const seen=[];let healthCalls=0;const saved=[];
  const doc={querySelector(){return null;},getElementById(){return {addEventListener(){},replaceChildren(){},appendChild(){},setAttribute(){},textContent:'',hidden:false};},createElement(){return {textContent:'',appendChild(){},append(){}};}};
  const fetcher=async(url,options)=>{
    const route=new URL(url).pathname;seen.push(route);
    if(route==='/health'){healthCalls++;return new Response(JSON.stringify({status:healthCalls===1?'unhealthy':'ok'}),{status:healthCalls===1?503:200,headers:{'content-type':'application/json'}});}
    if(route==='/api/session')return new Response(JSON.stringify({session_token:'a'.repeat(43)}),{status:202,headers:{'content-type':'application/json'}});
    if(route==='/api/state')return new Response(JSON.stringify({session:{state:'starting'}}),{headers:{'content-type':'application/json'}});
    throw Error('unexpected route '+route);
  };
  const ctx={document:doc,location:{href:'https://gateway.example/'},URL,Response,fetch:fetcher,sessionStorage:{getItem(){return null;},setItem(_k,v){saved.push(v);},removeItem(){}},setInterval(){},setTimeout(fn){fn();return 0;}};
  vm.runInNewContext(script,ctx);
  await ctx.__pageTest.begin();
  assert.deepEqual(seen,['/health','/health','/api/session','/api/state']);
  assert.equal(saved.length,1);
});

function runHarness({ready=[true],runResponses=[{status:202,body:{ok:true}}],clockStep=0,stateFor=null,routes={}}={}) {
  const calls=[];const elements=new Map();let stateIndex=0,runIndex=0,tick=0;
  const node=()=>({children:[],textContent:'',hidden:false,disabled:false,listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},replaceChildren(){this.children=[];},appendChild(item){this.children.push(item);},append(...items){this.children.push(...items);},setAttribute(){}});
  const doc={querySelector(){return null;},getElementById(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);},createElement(){return node();}};
  const fetcher=async(url,options)=>{
    const route=new URL(url).pathname;calls.push([route,options.method]);
    const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}});
    if(route==='/health')return json({status:'ok'});
    if(route==='/api/session')return json({session_token:'a'.repeat(43)},202);
    if(routes[route])return json(routes[route]());
    if(route==='/api/state'&&stateFor){const state=stateFor(stateIndex++);if(state.throw)throw Error('network down');return json(state);}
    if(route==='/api/state'){const isReady=ready[Math.min(stateIndex++,ready.length-1)];return json({session:{state:'ready'},health:{ok:isReady,parts:{judge:true}},run:null,record:{}});}
    if(route==='/api/run'){const result=runResponses[Math.min(runIndex++,runResponses.length-1)];if(result.throw)throw Error('connection dropped after submit');return json(result.body,result.status);}
    if(route==='/approver/api/chat')return json({messages:[]});
    throw Error('unexpected route '+route);
  };
  const ctx={document:doc,location:{href:'https://gateway.example/'},URL,Response,Date:clockStep?{now(){tick+=clockStep;return tick;}}:Date,fetch:fetcher,sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},setInterval(){},setTimeout(fn){fn();return 0;}};
  vm.runInNewContext(script,ctx);
  return {ctx,calls,elements};
}
test('one click waits for runtime health and sends exactly one accepted run',async()=>{
  const h=runHarness({ready:[false,false,true]});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,1);
  assert.equal(h.calls.filter(([route])=>route==='/api/session').length,1);
  assert.ok(h.calls.findIndex(([route])=>route==='/api/run')>h.calls.findIndex(([route])=>route==='/api/state'));
  assert.match(h.elements.get('notice').textContent,/Agent running/);
});
test('confirmed pre-execution 503 retries after a fresh state read',async()=>{
  const h=runHarness({runResponses:[{status:503,body:{ok:false,code:'starting',message:'The demo is starting.'}},{status:202,body:{ok:true}}]});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,2);
  assert.ok(h.calls.filter(([route])=>route==='/api/state').length>=3);
  assert.match(h.elements.get('notice').textContent,/Agent running/);
});
test('ambiguous run response is never retried automatically',async()=>{
  const h=runHarness({runResponses:[{status:502,body:{ok:false,message:'The demo did not answer.'}}]});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,1);
  assert.equal(h.elements.get('retry-run').hidden,true);
  assert.match(h.elements.get('notice').textContent,/could not be confirmed/);
});
test('budget refusal is not retried automatically',async()=>{
  const h=runHarness({runResponses:[{status:503,body:{ok:false,message:'The live AI demo allowance is used up.'}}]});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,1);
  assert.match(h.elements.get('notice').textContent,/allowance is used up/);
});

test('readiness deadline before any POST leaves manual retry available',async()=>{
  const h=runHarness({ready:[false],clockStep:30000});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,0);
  assert.equal(h.elements.get('retry-run').hidden,false);
  assert.equal(h.elements.get('session-state').textContent,'Preparing approval gate');
  assert.match(h.elements.get('notice').textContent,/90 seconds/);
});
test('deadline after only confirmed pre-execution refusals leaves manual retry available',async()=>{
  const h=runHarness({ready:[true],clockStep:30000,runResponses:[{status:503,body:{ok:false,code:'starting',message:'starting'}}]});await h.ctx.__pageTest.begin();
  assert.ok(h.calls.filter(([route])=>route==='/api/run').length>0);
  assert.equal(h.elements.get('retry-run').hidden,false);
  assert.match(h.elements.get('notice').textContent,/90 seconds/);
});

test('network loss after run submit never triggers another POST',async()=>{
  const h=runHarness({runResponses:[{throw:true}]});await h.ctx.__pageTest.begin();
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,1);
  assert.equal(h.elements.get('retry-run').hidden,true);
  assert.match(h.elements.get('notice').textContent,/could not be confirmed/);
});

const RUNNING={state:'running',scenarios:[],lines:[]};
const readyState=run=>({session:{state:'ready'},health:{ok:true,parts:{judge:true}},run,record:{}});
test('a transient state failure notice clears once a later poll confirms the run',async()=>{
  const h=runHarness({stateFor:i=>i===0?{throw:true}:readyState(RUNNING)});
  h.ctx.__pageTest.setTokenForTest('a'.repeat(43));
  await h.ctx.__pageTest.poll();
  assert.equal(h.elements.get('notice').textContent,'The demo connection failed. Retry in a moment.');
  assert.equal(h.elements.get('notice').className,'bad');
  await h.ctx.__pageTest.poll();
  assert.equal(h.elements.get('notice').textContent,'Agent running. Watch the chat and decide each held action.');
  assert.equal(h.elements.get('notice').className,'');
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,0);
});
test('an unconfirmed run notice clears once a later poll shows the run',async()=>{
  let confirmed=false;
  const h=runHarness({runResponses:[{throw:true}],stateFor:()=>readyState(confirmed?RUNNING:null)});
  await h.ctx.__pageTest.begin();
  assert.match(h.elements.get('notice').textContent,/could not be confirmed/);
  assert.equal(h.elements.get('notice').className,'bad');
  confirmed=true;
  await h.ctx.__pageTest.poll();
  assert.equal(h.elements.get('notice').textContent,'Agent running. Watch the chat and decide each held action.');
  assert.equal(h.elements.get('notice').className,'');
  assert.equal(h.calls.filter(([route])=>route==='/api/run').length,1);
});
test('a failed tap notice survives the next poll of a running run',async()=>{
  const h=runHarness({stateFor:()=>readyState(RUNNING),routes:{
    '/approver/api/chat':()=>({messages:[{bot:'gate',message_id:7,text:'<b>WHAT THIS DOES</b>\nMoves main',buttons:[{text:'Approve',data:'g:1:ab'}]}]}),
    '/approver/api/tap':()=>{throw Error('network down');},
  }});
  h.ctx.__pageTest.setTokenForTest('a'.repeat(43));
  await h.ctx.__pageTest.poll();
  const find=n=>n.listeners&&n.listeners.click&&n.textContent==='Approve'?n:(n.children||[]).map(find).find(Boolean);
  const approve=find(h.elements.get('chat'));
  assert.ok(approve,'approve button rendered');
  await approve.listeners.click();
  assert.equal(h.elements.get('notice').textContent,'The demo connection failed. Retry in a moment.');
  await h.ctx.__pageTest.poll();
  assert.equal(h.elements.get('notice').textContent,'The demo connection failed. Retry in a moment.');
  assert.equal(h.elements.get('notice').className,'bad');
});
