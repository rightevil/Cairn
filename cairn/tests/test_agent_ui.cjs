const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
const context = vm.createContext({
  URLSearchParams, location: { search: '?project=p&run=r' },
  document: { body: { scrollHeight: 100 }, getElementById(id) {
    if (!elements.has(id)) elements.set(id, { checked: false, value: 'stdout', textContent: '', addEventListener() {} });
    return elements.get(id);
  } },
  window: { scrollY: 0, innerHeight: 700, scrollTo() {} },
  sessionStorage: { getItem: () => 'test-token' }, setInterval() {},
  fetch: async () => ({ ok: true, json: async () => ({ worker: 'test', role: 'planner', state: 'running', streams: {} }) }),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/cairn/server/static/agent.js'), 'utf8'), context);
const events = [
  { event: { type: 'message_start' } },
  { event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'Live ' } } },
  { event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'text' } } },
  { type: 'assistant', message: { content: [{ type: 'text', text: 'Live text' }, { name: 'Skill', input: { skill: 'bmad-prd' } }] } },
].map(JSON.stringify).join('\n');
const readable = context.readable(events);
assert(readable.includes('Live text'));
assert.equal(readable.split('Live text').length - 1, 1);
assert(readable.includes('[工具] Skill'));
assert.equal(context.readable('truncated { line'), 'truncated { line\n');
elements.get('raw').checked = true;
assert.equal(context.readable(events), events);
console.log('Agent window checks passed: real-time deltas, completed-block deduplication, tools, malformed lines and raw mode');
