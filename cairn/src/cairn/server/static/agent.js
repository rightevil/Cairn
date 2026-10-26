const params = new URLSearchParams(location.search);
const projectId = params.get('project'), runId = params.get('run');
const element = id => document.getElementById(id);
let lastText = '', busy = false;
function readable(text) {
  if (element('raw').checked) return text;
  let output = '', streamed = false;
  const lineOut = value => { if (value) output += (output && !output.endsWith('\n') ? '\n' : '') + value + '\n'; };
  for (const line of text.split('\n')) {
    try {
      const item = JSON.parse(line), event = item.event;
      if (event) {
        if (event.type === 'message_start') streamed = false;
        if (event.delta?.type === 'text_delta') { output += event.delta.text; streamed = true; }
        continue;
      }
      const content = item.message?.content || item.content;
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block.text && !streamed) lineOut(block.text);
          else if (block.name) lineOut(`[工具] ${block.name} ${JSON.stringify(block.input || {})}`);
        }
      } else if (item.type === 'result' || item.subtype === 'success') lineOut(item.result || '');
      else if (item.type === 'system' || item.subtype === 'init') lineOut(`[会话] ${item.subtype || 'system'} ${item.session_id || item.data?.session_id || ''}`);
      else lineOut(line);
    } catch { lineOut(line); }
  }
  return output;
}
async function get(url) {
  const response = await fetch(url, { headers: { 'X-Cairn-Operator-Token': sessionStorage.getItem('cairn.operatorToken') || '' } });
  if (!response.ok) {
    if (response.status === 401) element('auth').hidden = false;
    const body = await response.json(); throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response;
}
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    if (!projectId || !runId) throw new Error('运行地址缺少项目或运行 ID');
    const base = `/projects/${encodeURIComponent(projectId)}/development/runs/${encodeURIComponent(runId)}`;
    const run = await (await get(base)).json();
    element('title').textContent = run.worker + ' · ' + run.role;
    element('status').textContent = run.state;
    element('meta').textContent = `${run.intent_id} · PID ${run.pid || '准备中'} · 会话 ${run.session || '准备中'} · 心跳 ${run.heartbeat_at}`;
    const channel = element('channel').value;
    const text = run.streams?.[channel] ? await (await get(`${base}/stream/${channel}`)).text() : (run.detail || run.checkpoint || '等待启动输出…');
    const nearBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 80;
    lastText = text; element('output').textContent = readable(text); element('error').textContent = '';
    if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
  } catch (e) { element('error').textContent = e.message; }
  finally { busy = false; }
}
element('auth').addEventListener('submit', event => { event.preventDefault(); sessionStorage.setItem('cairn.operatorToken', element('token').value.trim()); element('token').value = ''; element('auth').hidden = true; refresh(); });
element('channel').addEventListener('change', refresh);
element('raw').addEventListener('change', () => { element('output').textContent = readable(lastText); });
refresh();
setInterval(refresh, 1000);
