const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise lifecycle and transport without launching Claude or replacing its PTY.
const sockets = [], terminals = [], frames = new Map(); let frameId = 0;
class Terminal {
  constructor(options) { this.options=options; this.rows=24; this.cols=80; this.text=''; this.scroll=0; terminals.push(this); }
  loadAddon() {} open() {} onData(callback) { this.input=callback; }
  write(text, callback) { this.text+=text; callback(); }
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
  location:{protocol:'http:',host:'localhost'}, document:{documentElement:{},createElement:()=>({remove() {},append() {},addEventListener() {}})},
  getComputedStyle:()=>({getPropertyValue:()=> '#ffffff'}),
  requestAnimationFrame:callback=>{const id=++frameId; frames.set(id,callback); return id;},
  cancelAnimationFrame:id=>frames.delete(id)});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/cairn/server/static/design-terminal.js'),'utf8'),context);
const host=()=>({clientWidth:800,clientHeight:600,replaceChildren(child) {this.child=child;}});
const flush=()=>{while(frames.size){const batch=[...frames.values()]; frames.clear(); batch.forEach(callback=>callback());}};
const mainHost=host(), reviewHost=host(), states=[];
const mount=(target,project,id)=>context.mountDesignTerminal(target,project,{id},(run,state)=>states.push([run,state]));
mount(mainHost,'project','main'); flush();
sockets[0].receive({type:'ready',readonly:false});
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
