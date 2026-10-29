/* Live PTY and persistent conversation history share the terminal surface. */
let cairnTerminal = null;
const cairnTerminals = new Map();
function disposeDesignTerminals() { for (const entry of [...cairnTerminals.values()]) entry.dispose(); }
function mountDesignTerminal(host, project, run, onState) {
  run ||= {id:'pending',state:'pending',placeholder:true};
  const key = project + ':' + run.id;
  if (cairnTerminal?.host === host && cairnTerminal?.key === key) { cairnTerminal.update(run); return; }
  cairnTerminal?.detach();
  let entry = cairnTerminals.get(key);
  if (entry) { entry.update(run); entry.attach(host,onState); return; }
  const viewport = document.createElement('div'); viewport.className = 'terminal-viewport'; host.replaceChildren(viewport);
  const controls = document.createElement('div'); controls.className = 'terminal-view-controls';
  const liveButton = document.createElement('button'); liveButton.textContent = '终端画面'; liveButton.className = 'quiet';
  const historyButton = document.createElement('button'); historyButton.textContent = '对话历史'; historyButton.className = 'quiet';
  const more = document.createElement('button'); more.textContent = '加载更早历史'; more.className = 'quiet'; more.hidden = true;
  const status = document.createElement('span'); status.className = 'muted';
  controls.append(liveButton,historyButton,more,status);
  const live = document.createElement('div'); live.className = 'terminal-live';
  const history = document.createElement('div'); history.className = 'terminal-history'; history.hidden = true;
  viewport.append(controls,live,history);
  const palette = getComputedStyle(document.documentElement), color = name => palette.getPropertyValue(name).trim();
  const options = {cursorBlink:true,convertEol:false,scrollback:10000,
    fontFamily:'Consolas, "Cascadia Mono", monospace',fontSize:13,
    theme:{background:color('--graph-paper'),foreground:color('--graph-ink'),cursor:color('--graph-cool'),selectionBackground:color('--terminal-selection')}};
  const terminal = new Terminal(options), fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(live);
  let archive = null, archiveFit = null, rows = [], before = null, loading = false, historyLoaded = false;
  let readonly = true, state = 'connecting', frame = null, queue = [], writing = false, closed = false, finished = false;
  const base = `/projects/${encodeURIComponent(project)}/development/runs/${encodeURIComponent(run.id)}/terminal`;
  const socket = run.placeholder || run.historyOnly ? null : new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${base}`);
  const metrics = {bytes:0,batches:0,maxQueued:0,paintDelay:[]};
  const send = value => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const canInput = () => !readonly && !finished && history.hidden && ['idle','working'].includes(state);
  function resize() {
    if (!entry?.host?.clientWidth || !entry.host.clientHeight) return;
    if (!history.hidden) { archiveFit?.fit(); return; }
    const oldRows = terminal.rows, oldCols = terminal.cols; fit.fit();
    if (oldRows !== terminal.rows || oldCols !== terminal.cols) send({type:'resize',rows:terminal.rows,cols:terminal.cols});
  }
  const safe = text => (typeof agentRedact === 'function' ? agentRedact(text) : String(text)).replace(/\r\n?/g,'\n').replace(/[\x00-\x08\x0B-\x1F\x7F-\x9F]/g,'');
  function showHistory() {
    live.hidden = true; history.hidden = false;
    terminal.options.disableStdin = true;
    if (!archive) { archive = new Terminal({...options,cursorBlink:false,disableStdin:true,scrollback:100000}); archiveFit = new FitAddon.FitAddon(); archive.loadAddon(archiveFit); archive.open(history); }
    requestAnimationFrame(()=>{if(!closed){resize();archive.focus();}});
    loadHistory();
  }
  async function loadHistory(earlier = false) {
    if (loading || closed) return;
    loading = true; status.textContent = '正在读取历史…'; more.disabled = true;
    try {
      const response = run.placeholder ? null : await fetch(base + '/history' + (earlier && before != null ? '?before=' + before : ''));
      if (response && !response.ok) throw new Error('历史暂不可读，请重新点击“对话历史”重试');
      const data = response ? await response.json() : {entries:[],before:null,total:0};
      if (closed) return;
      const position = archive?.buffer?.active?.viewportY || 0, oldBase = archive?.buffer?.active?.baseY || 0;
      const follow = !historyLoaded || position >= oldBase;
      const known = new Set(rows.map(item=>item.id)), overlap = data.entries.some(item=>known.has(item.id));
      const items = earlier ? [...data.entries,...rows] : [...rows,...data.entries];
      rows = [...new Map(items.map(item=>[item.id,item])).values()].sort((a,b)=>(a.index||0)-(b.index||0));
      before = earlier || !historyLoaded || !overlap ? data.before : before;
      const text = rows.map(item=>'\x1b[1;34m'+safe(item.role)+'\x1b[0m\r\n'+safe(item.text).replace(/\n/g,'\r\n')+'\r\n\r\n').join('') || '尚无已保存的对话。\r\n';
      archive.reset(); await new Promise(resolve=>archive.write(text,()=>{
        if (closed) { resolve(); return; }
        if (earlier) archive.scrollToLine(position + Math.max(0,archive.buffer.active.baseY-oldBase));
        else if (follow) archive.scrollToBottom();
        else archive.scrollToLine(position);
        resolve();
      }));
      if (closed) return;
      historyLoaded = true; more.hidden = before == null;
      status.textContent = `历史只读 · 已加载 ${rows.length} / ${data.total} 条记录`;
    } catch(e) { status.textContent = e.message; }
    finally { loading = false; more.disabled = false; }
  }
  liveButton.onclick = () => { history.hidden = true; live.hidden = false; status.textContent = ''; terminal.options.disableStdin=!canInput();requestAnimationFrame(()=>{if(!closed){resize();terminal.focus();}}); };
  historyButton.onclick = showHistory; more.onclick = () => loadHistory(true);
  live.addEventListener('wheel', event => {
    if (event.deltaY >= 0) return;
    event.preventDefault(); event.stopImmediatePropagation(); showHistory();
  }, {capture:true,passive:false});
  history.addEventListener('wheel', event => {
    if (event.deltaY < 0 && archive?.buffer.active.viewportY === 0 && before != null) loadHistory(true);
  }, {passive:true});
  const drain = () => {
    frame = null;
    if (closed || writing || !queue.length) return;
    writing = true; const batch = queue; queue = [];
    terminal.write(batch.map(item=>item.data).join(''),()=>{
      writing = false; metrics.batches++;
      const last = batch.at(-1);
      if (last.seq != null && !closed) send({type:'ack',seq:last.seq});
      if (last.sent_at) { metrics.paintDelay.push(Date.now()-last.sent_at); if (metrics.paintDelay.length>200) metrics.paintDelay.shift(); }
      if (queue.length && !closed) frame=requestAnimationFrame(drain);
    });
  };
  const observer = new ResizeObserver(resize);
  terminal.onData(data => {
    if (canInput() && !/^\x1b(?:\[[?\d;>]*[Rc]|\](?:10|11);)/.test(data)) send({type:'input',data});
  });
  if (socket) {
    socket.onmessage = event => {
      const value=JSON.parse(event.data);
      if (value.type==='output') { queue.push(value); metrics.bytes+=value.data.length; metrics.maxQueued=Math.max(metrics.maxQueued,queue.length); if(!frame&&!writing) frame=requestAnimationFrame(drain); }
      if (value.type==='ready') { readonly=value.readonly; terminal.options.disableStdin=!canInput(); resize(); send({type:'resize',rows:terminal.rows,cols:terminal.cols}); }
      if (value.type==='state') {
        state=value.state; entry.onState(run.id,state);
        terminal.options.disableStdin=!canInput();
        if (state==='closed') { finished=true;readonly=true; terminal.options.disableStdin=true; showHistory(); }
      }
    };
    socket.onclose=()=>{terminal.options.disableStdin=true;state='disconnected';entry.onState(run.id,state);};
    socket.onerror=()=>{state='disconnected';entry.onState(run.id,state);};
  } else { terminal.options.disableStdin=true; terminal.write('Cairn · 方案设计\r\n\r\n正在准备终端连接，请在连接就绪后直接在终端输入。\r\n'); }
  entry={key,host:null,run:run.id,onState,metrics,
    update(next) {
      if ((next.state !== 'running' || next.historyOnly) && !next.placeholder && !finished) { finished=true;readonly=true;terminal.options.disableStdin=true;showHistory(); }
    },
    attach(nextHost,callback) {this.host=nextHost;this.onState=callback;nextHost.replaceChildren(viewport);observer.observe(nextHost);cairnTerminal=this;requestAnimationFrame(resize);callback(run.id,state);},
    detach() {observer.disconnect();viewport.remove();this.host=null;if(cairnTerminal===this)cairnTerminal=null;},
    dispose() {closed=true;this.detach();if(frame)cancelAnimationFrame(frame);if(socket){socket.onclose=null;socket.onerror=null;socket.close();}terminal.dispose();archive?.dispose();cairnTerminals.delete(key);}
  };
  cairnTerminals.set(key,entry);entry.attach(host,onState);entry.update(run);
  while(cairnTerminals.size>4){const unused=[...cairnTerminals.values()].find(item=>item!==entry&&!item.host);if(!unused)break;unused.dispose();}
}
