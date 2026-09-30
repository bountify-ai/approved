'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../service/src/approved/tryit/page.py'),'utf8');
const match=source.match(/PAGE_SCRIPT = r"""([\s\S]*?)"""/);
assert.ok(match,'page script found');
const script=match[1].replace(/\}\)\(\);\s*$/, 'globalThis.__pageTest={plainTelegram,traceLink,renderRun,begin};})();');
const nodes=new Map();
const document={
  querySelector(){return null;},
  getElementById(id){if(!nodes.has(id))nodes.set(id,{children:[],addEventListener(){},replaceChildren(){this.children=[];},appendChild(n){this.children.push(n);},append(...items){this.children.push(...items);},setAttribute(){},textContent:'',hidden:false});return nodes.get(id);},
  createElement(tag){return {tagName:tag,children:[],textContent:'',className:'',appendChild(n){this.children.push(n);},append(...items){this.children.push(...items);}};},
};
const context={document,location:{href:'https://gateway.example/'},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},URL,setInterval(){}};
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
