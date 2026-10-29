/* Native PTY sessions: preserve terminal buffers when switching views. */
let cairnTerminal = null;
const cairnTerminals = new Map();
function disposeDesignTerminals() {
  for (const entry of [...cairnTerminals.values()]) entry.dispose();
}
function mountDesignTerminal(host, project, run, onState) {
  const key = project + ':' + run.id;
  if (cairnTerminal?.host === host && cairnTerminal?.key === key) return;
  cairnTerminal?.detach();
  let entry = cairnTerminals.get(key);
  if (entry) { entry.attach(host, onState); return; }
  const viewport = document.createElement('div'); viewport.className = 'terminal-viewport';
  host.replaceChildren(viewport);
  const palette = getComputedStyle(document.documentElement), color = name => palette.getPropertyValue(name).trim();
  const terminal = new Terminal({cursorBlink:true,convertEol:false,scrollback:10000,
    fontFamily:'Consolas, "Cascadia Mono", monospace',fontSize:13,
    theme:{background:color('--graph-paper'),foreground:color('--graph-ink'),cursor:color('--graph-cool'),selectionBackground:color('--terminal-selection')}});
  const fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(viewport);
  const history = document.createElement('div'); history.className = 'terminal-history'; history.hidden = true;
  const returnLive = document.createElement('button'); returnLive.textContent = '回到实时终端'; returnLive.className = 'secondary';
  const snapshots = document.createElement('pre'); history.append(returnLive,snapshots); viewport.append(history);
  let screens = [], lastScreen = '';
  returnLive.onclick = () => { history.hidden = true; };
  viewport.addEventListener('wheel', event => {
    if (terminal.buffer?.active?.type !== 'alternate' || !screens.length || !history.hidden || event.deltaY >= 0) return;
    event.preventDefault(); event.stopImmediatePropagation();
    snapshots.textContent = screens.join('\n\n──────── 历史画面 ────────\n\n'); history.hidden = false;
    history.scrollTop = history.scrollHeight;
  }, {capture:true,passive:false});
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/projects/${encodeURIComponent(project)}/development/runs/${encodeURIComponent(run.id)}/terminal`);
  let readonly = true, state = 'connecting', frame = null, queue = [], writing = false, closed = false;
  const metrics = {bytes:0,batches:0,maxQueued:0,paintDelay:[]};
  const send = value => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const resize = () => {
    if (!entry?.host?.clientWidth || !entry.host.clientHeight) return;
    const rows = terminal.rows, cols = terminal.cols;
    fit.fit();
    if (rows !== terminal.rows || cols !== terminal.cols) send({type:'resize',rows:terminal.rows,cols:terminal.cols});
  };
  const drain = () => {
    frame = null;
    if (closed || writing || !queue.length) return;
    writing = true;
    const batch = queue; queue = [];
    terminal.write(batch.map(item => item.data).join(''), () => {
      writing = false; metrics.batches++;
      const last = batch.at(-1);
      if (last.seq != null && !closed) send({type:'ack',seq:last.seq});
      if (last.sent_at) { metrics.paintDelay.push(Date.now()-last.sent_at); if (metrics.paintDelay.length > 200) metrics.paintDelay.shift(); }
      if (terminal.buffer?.active?.type === 'alternate') {
        const buffer = terminal.buffer.active, rows = [];
        for (let i=0;i<terminal.rows;i++) rows.push(buffer.getLine(i)?.translateToString(true) || '');
        const screen = rows.join('\n').trimEnd();
        if (screen && screen !== lastScreen) { lastScreen = screen; screens.push(screen); if (screens.length > 80) screens.shift(); }
      }
      if (queue.length && !closed) frame = requestAnimationFrame(drain);
    });
  };
  const observer = new ResizeObserver(resize);
  terminal.onData(data => {
    if (!readonly && !/^\x1b(?:\[[?\d;>]*[Rc]|\](?:10|11);)/.test(data)) send({type:'input',data});
  });
  socket.onmessage = event => {
    const value = JSON.parse(event.data);
    if (value.type === 'output') { queue.push(value); metrics.bytes += value.data.length; metrics.maxQueued = Math.max(metrics.maxQueued,queue.length); if (!frame && !writing) frame = requestAnimationFrame(drain); }
    if (value.type === 'ready') { readonly = value.readonly; terminal.options.disableStdin = readonly; resize(); send({type:'resize',rows:terminal.rows,cols:terminal.cols}); }
    if (value.type === 'state') { state = value.state; entry.onState(run.id,state); }
  };
  socket.onclose = () => { terminal.options.disableStdin = true; state = 'disconnected'; entry.onState(run.id,state); };
  socket.onerror = () => { state = 'disconnected'; entry.onState(run.id,state); };
  entry = { key, host:null, run:run.id, onState, metrics,
    attach(nextHost, callback) {
      this.host = nextHost; this.onState = callback; nextHost.replaceChildren(viewport);
      observer.observe(nextHost); cairnTerminal = this;
      requestAnimationFrame(resize); callback(run.id,state);
    },
    detach() { observer.disconnect(); viewport.remove(); this.host = null; if (cairnTerminal === this) cairnTerminal = null; },
    dispose() { closed = true; this.detach(); if (frame) cancelAnimationFrame(frame); socket.onclose = null; socket.onerror = null; socket.close(); terminal.dispose(); cairnTerminals.delete(key); }
  };
  cairnTerminals.set(key,entry); entry.attach(host,onState);
  // Bound inactive terminal memory; log replay restores evicted sessions.
  while (cairnTerminals.size > 4) {
    const unused = [...cairnTerminals.values()].find(item => item !== entry && !item.host);
    if (!unused) break; unused.dispose();
  }
}
