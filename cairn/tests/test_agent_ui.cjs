const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
const context = vm.createContext({
  URLSearchParams, location: { search: '?project=p&run=r' },
  document: { body: { scrollHeight: 100 }, getElementById(id) {
    if (!elements.has(id)) elements.set(id, { checked: false, value: 'stdout', textContent: '', querySelectorAll() { return []; }, addEventListener() {} });
    return elements.get(id);
  } },
  window: { scrollY: 0, innerHeight: 700, scrollTo() {} },
  sessionStorage: { getItem: () => 'test-token' }, setInterval() {},
  fetch: async () => ({ ok: true, json: async () => ({ worker: 'test', role: 'planner', state: 'running', streams: {} }) }),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/cairn/server/static/agent-output.js'), 'utf8'), context);
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
assert.equal(context.readable('truncated { line'), 'truncated { line');
elements.get('raw').checked = true;
assert.equal(context.readable(events), events);
console.log('Agent window checks passed: real-time deltas, completed-block deduplication, tools, malformed lines and raw mode');

const toolLog = [
  {event:{type:'message_start',message:{id:'m1'}}},
  {timestamp:'2026-01-01T00:00:00Z',event:{type:'content_block_start',index:0,content_block:{type:'tool_use',id:'call-1',name:'Bash',input:{}}}},
  {event:{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:'{"command":"node --test","env":{"API_KEY":"test-only-secret"}}'}}},
  {type:'assistant',message:{content:[{type:'tool_use',id:'call-1',name:'Bash',input:{command:'node --test',env:{API_KEY:'test-only-secret'}}}]}},
  {timestamp:'2026-01-01T00:00:01Z',type:'user',tool_use_result:{exitCode:7,durationMs:125,stderr:'TOKEN=test-only-tail'},message:{content:[{type:'tool_result',tool_use_id:'call-1',is_error:true,content:'{"token":"test-only-output","message":"failed"}'}]}},
].map(JSON.stringify).join('\n');
const toolEntries = context.agentOutput(toolLog).filter(entry=>entry.kind==='tool');
assert.equal(toolEntries.length,1,'Streaming and completed tool snapshots share the real call ID');
assert.equal(toolEntries[0].state,'error');
assert.equal(toolEntries[0].exitCode,7);
assert.equal(toolEntries[0].durationMs,125);
assert(!JSON.stringify(toolEntries).includes('test-only-'));
const toolMarkup=context.agentToolHTML(toolEntries[0],true);
assert(toolMarkup.includes('退出码 7')&&toolMarkup.includes('125 ms')&&toolMarkup.includes('2026-01-01'));
const malicious=context.agentToolHTML({...toolEntries[0],input:'{"command":"<img src=x onerror=alert(1)>"}'});
assert(!malicious.includes('<img')&&malicious.includes('&lt;img'));
const incomplete=context.agentOutput(JSON.stringify({content:[{id:'pending',name:'Read',input:{file_path:'src/app.py'}}]}))[0];
assert.equal(incomplete.state,'running');
assert(!context.agentToolHTML(incomplete).includes('耗时'));
