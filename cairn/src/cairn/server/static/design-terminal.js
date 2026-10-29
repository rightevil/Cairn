/* Native design TUI only; development sessions continue to use structured logs. */
let cairnTerminal = null;
function mountDesignTerminal(host, project, run, onState) {
  if (cairnTerminal?.host === host && cairnTerminal?.run === run.id) return;
  cairnTerminal?.dispose();
  host.replaceChildren();
  const terminal = new Terminal({cursorBlink:true,convertEol:false,scrollback:8000,
    fontFamily:'Consolas, "Cascadia Mono", monospace',fontSize:14,
    theme:{background:'#f7f5f0',foreground:'#29303a',cursor:'#575b9a',selectionBackground:'#c8c9ed'}});
  const fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(host);
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/projects/${encodeURIComponent(project)}/development/runs/${encodeURIComponent(run.id)}/terminal`);
  let readonly = true;
  const send = value => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const resize = () => { if (host.clientWidth && host.clientHeight) {fit.fit();send({type:'resize',rows:terminal.rows,cols:terminal.cols});} };
  const observer = new ResizeObserver(resize); observer.observe(host);
  terminal.onData(data => {
    // The dispatcher answers device queries even when no browser is attached.
    if (!readonly && !/^\x1b(?:\[[?\d;>]*[Rc]|\](?:10|11);)/.test(data)) send({type:'input',data});
  });
  socket.onmessage = event => {
    const value=JSON.parse(event.data);
    if(value.type==='output') terminal.write(value.data);
    if(value.type==='ready'){readonly=value.readonly;terminal.options.disableStdin=readonly;resize();}
    if(value.type==='state') onState(run.id,value.state);
  };
  socket.onclose = () => {terminal.options.disableStdin=true;onState(run.id,'disconnected');};
  socket.onerror = () => onState(run.id,'disconnected');
  cairnTerminal={host,run:run.id,dispose(){observer.disconnect();socket.onclose=null;socket.close();terminal.dispose();cairnTerminal=null;}};
}
