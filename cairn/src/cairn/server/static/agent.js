const params = new URLSearchParams(location.search);
const projectId = params.get('project'), runId = params.get('run');
const element = id => document.getElementById(id);
let lastText = '', busy = false;
function readable(text) {
  if (element('raw').checked) return agentRedact(text);
  return agentOutput(text).map(entry => entry.kind === 'tool' ? `[工具] ${entry.name}\n${entry.input}\n${entry.output}` : entry.text).join('\n') || agentRedact(text);
}
function renderOutput(text, channel) {
  const raw = element('raw').checked || channel === 'stderr';
  element('output').hidden = !raw;
  element('conversation').hidden = raw;
  if (raw) { element('output').textContent = agentRedact(text); return; }
  const entries = agentOutput(text);
  const opened = new Set([...element('conversation').querySelectorAll('details[open]')].map(el => el.dataset.call));
  element('conversation').innerHTML = entries.map(entry => entry.kind === 'tool' ? agentToolHTML(entry)
    : `<article class="agent-entry markdown">${DOMPurify.sanitize(marked.parse(entry.text))}</article>`).join('') || '<p class="muted">等待会话正文与工具活动…</p>';
  for (const el of element('conversation').querySelectorAll('details')) el.open = opened.has(el.dataset.call);
}
async function get(url) {
  const response = await fetch(url);
  if (!response.ok) {
    if (response.status === 401) throw new Error('请先在本机打开 Cairn 工作台，再查看 Agent 窗口。');
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
    element('title').textContent = run.worker + ' / ' + run.role;
    element('status').textContent = run.state;
    element('meta').textContent = `${run.intent_id} / PID ${run.pid || '准备中'} / 会话 ${run.session || '准备中'} / 心跳 ${run.heartbeat_at}`;
    const channel = element('channel').value;
    const text = run.streams?.[channel] ? await (await get(`${base}/stream/${channel}`)).text() : (run.detail || run.checkpoint || '等待启动输出…');
    const nearBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 80;
    lastText = text; renderOutput(text, channel); element('error').textContent = '';
    if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
  } catch (e) { element('error').textContent = e.message; }
  finally { busy = false; }
}
element('channel').addEventListener('change', refresh);
element('raw').addEventListener('change', () => renderOutput(lastText, element('channel').value));
refresh();
setInterval(refresh, 1000);
