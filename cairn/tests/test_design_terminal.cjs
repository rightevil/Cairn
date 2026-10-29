const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise lifecycle and transport without launching Claude or replacing its PTY.
const sockets = [], terminals = [], frames = new Map(); let frameId = 0;
class Terminal {
  constructor(options) { this.options=options; this.rows=24; this.cols=80; this.text=''; this.scroll=0;this.buffer={active:{viewportY:0,baseY:0}}; terminals.push(this); }
  loadAddon() {} open() {} focus() {} onData(callback) { this.input=callback; }
  write(text, callback) { this.text+=text; this.buffer.active.baseY=Math.max(0,this.text.split('\n').length-this.rows);callback?.(); }
  reset() {this.text='';this.buffer.active.baseY=this.buffer.active.viewportY=0;}
  scrollToLine(line) {this.buffer.active.viewportY=Math.max(0,Math.min(line,this.buffer.active.baseY));}
  scrollToBottom() {this.buffer.active.viewportY=this.buffer.active.baseY;}
  dispose() { this.disposed=true; }
}
class Socket {
  static OPEN=1;
  constructor(url) { this.url=url; this.readyState=1; this.sent=[]; sockets.push(this); }
  send(value) { this.sent.push(JSON.parse(value)); }
  receive(value) { this.onmessage({data:JSON.stringify(value)}); }
  close() { this.closed=true; }
}
const context = vm.createContext({Terminal, WebSocket:Socket, FitAddon:{FitAddon:class {fit() {}}},
  ResizeObserver:class {observe() {} disconnect() {}},
  location:{protocol:'http:',host:'localhost'}, document:{documentElement:{},createElement:()=>({children:[],remove() {},append(...items) {this.children.push(...items);},addEventListener() {}})},
  getComputedStyle:()=>({getPropertyValue:()=> '#ffffff'}),
  requestAnimationFrame:callback=>{const id=++frameId; frames.set(id,callback); return id;},
  cancelAnimationFrame:id=>frames.delete(id)});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/cairn/server/static/design-terminal.js'),'utf8'),context);
const host=()=>({clientWidth:800,clientHeight:600,replaceChildren(child) {this.child=child;}});
const flush=()=>{while(frames.size){const batch=[...frames.values()]; frames.clear(); batch.forEach(callback=>callback());}};
const mainHost=host(), reviewHost=host(), states=[];
const mount=(target,project,id)=>context.mountDesignTerminal(target,project,{id,state:'running'},(run,state)=>states.push([run,state]));
mount(mainHost,'project','main'); flush();
sockets[0].receive({type:'ready',readonly:false});
terminals[0].input('not ready');assert(!sockets[0].sent.some(value=>value.type==='input'));
sockets[0].receive({type:'state',state:'idle'});
sockets[0].receive({type:'output',data:'first',seq:5});
sockets[0].receive({type:'output',data:' second',seq:12});
assert(!sockets[0].sent.some(value=>value.type==='ack'),'Acknowledge after xterm has consumed the output');flush();
assert(sockets[0].sent.some(value=>value.type==='ack' && value.seq===12));
assert.equal(terminals[0].text,'first second');
terminals[0].scroll=17;
terminals[0].input('hello');
assert(sockets[0].sent.some(value=>value.type==='input' && value.data==='hello'));
mount(reviewHost,'project','review'); flush();
sockets[1].receive({type:'ready',readonly:true});
terminals[1].input('must not reach reviewer');
assert(!sockets[1].sent.some(value=>value.type==='input'));
sockets[0].receive({type:'output',data:' background'}); flush();
mount(mainHost,'project','main'); flush();
assert.equal(terminals.length,2,'Switching sessions reuses terminals and sockets');
assert.equal(terminals[0].text,'first second background');
assert.equal(terminals[0].scroll,17,'Switching does not reset scrollback');
mount(mainHost,'project','main'); assert.equal(terminals.length,2);
terminals[0].input('\x1b[?1;2c');
assert(!sockets[0].sent.some(value=>value.data==='\x1b[?1;2c'),'Do not feed device replies to Claude input');
for(let i=0;i<5;i++) mount(host(),'another-project','run-'+i);
assert(vm.runInContext('cairnTerminals.size',context)<=4,'Inactive terminal cache is bounded');
assert(sockets[0].closed,'Eviction closes only the browser socket');
context.disposeDesignTerminals();
assert.equal(vm.runInContext('cairnTerminals.size',context),0);
assert(sockets.every(socket=>socket.closed));
assert(terminals.every(terminal=>terminal.disposed));
console.log('Design terminal checks passed: cache, scrollback, batched writes, read-only input and bounded cleanup');

(async()=>{
  const fetched=[];
  context.fetch=async url=>{fetched.push(url);return {ok:true,json:async()=>url.includes('before=')
    ? {entries:[{id:'first',index:0,role:'你',text:'First message'}],before:null,total:2}
    : {entries:[{id:'last',index:1,role:'Agent',text:'Last saved reply'}],before:1,total:2}};};
  const ended=host();context.mountDesignTerminal(ended,'project',{id:'ended',state:'success'},()=>{});flush();
  await new Promise(setImmediate);
  const archive=terminals.at(-1),controls=ended.child.children[0];
  assert(archive.text.includes('Last saved reply') && archive.options.disableStdin);
  await controls.children[2].onclick();
  assert(archive.text.indexOf('First message')<archive.text.indexOf('Last saved reply'));
  assert.equal(fetched.length,2);
  const cached=vm.runInContext('cairnTerminal',context);cached.detach();
  context.mountDesignTerminal(ended,'project',{id:'ended',state:'success'},()=>{});flush();
  assert.equal(fetched.length,2,'Switching back must not reset/reload the saved history viewport');
  sockets.at(-1).receive({type:'ready',readonly:false});sockets.at(-1).receive({type:'state',state:'idle'});
  controls.children[0].onclick();flush();
  terminals.at(-2).input('cannot send after end');
  assert(!sockets.at(-1).sent.some(value=>value.type==='input'),'An ended main session stays read-only even with stale live state');
  const count=sockets.length;context.mountDesignTerminal(host(),'empty-project',null,()=>{});flush();
  assert.equal(sockets.length,count,'A waiting terminal must not attach an invented run');
  assert(terminals.at(-1).text.includes('准备终端连接') && terminals.at(-1).options.disableStdin);
  context.disposeDesignTerminals();
  console.log('Persistent history checks passed: ended replay, earlier messages, cached viewport, ended input fencing and terminal-only waiting view');
})().catch(error=>{console.error(error);process.exitCode=1;});
