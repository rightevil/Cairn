const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/cairn/server/static/agent-output.js'),'utf8'),context);
const lines=[
 {event:{type:'message_start',message:{id:'m1'}}},
 {event:{type:'content_block_start',index:0,content_block:{type:'tool_use',id:'read1',name:'Read',input:{}}}},
 {event:{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:'{"file_'}}},
 {event:{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:'path":"src/app.js"}'}}},
 {type:'assistant',message:{content:[{type:'tool_use',id:'read1',name:'Read',input:{file_path:'src/app.js'}}]}},
 {content:[{tool_use_id:'read1',content:[{type:'text',text:'API_KEY=private-value\n<h1>file body</h1>'}],is_error:false}]},
 {content:[{id:'bash2',name:'Bash',input:{command:'echo ok',env:{TOKEN:'nested-secret'}}}],parent_tool_use_id:'read1'},
 {type:'user',message:{content:[{type:'tool_result',tool_use_id:'bash2',content:'permission denied',is_error:true}]}},
 {event:{type:'message_start',message:{id:'m2'}}},
 {event:{type:'content_block_delta',delta:{type:'text_delta',text:'Done'}}},
 {type:'assistant',message:{content:[{type:'text',text:'Done'}]}},
 {type:'result',result:'Done'}
].map(JSON.stringify).join('\n');
const entries=context.agentOutput(lines),tools=entries.filter(e=>e.kind==='tool');
assert.equal(tools.length,2);assert.equal(tools[0].state,'success');assert.equal(tools[1].state,'error');
assert(tools[0].input.includes('src/app.js'));assert(tools[0].output.includes('file body'));
assert(!JSON.stringify(entries).includes('private-value'));assert(!JSON.stringify(entries).includes('nested-secret'));
assert.equal(tools[1].parentId,'read1');assert.equal(entries.filter(e=>e.text==='Done').length,1);
assert(context.agentToolHTML(tools[0]).includes('&lt;h1&gt;'));assert(!context.agentToolHTML(tools[0]).includes('<h1>'));
const partial=context.agentOutput(lines.split('\n').slice(0,3).join('\n'));
assert.equal(partial[0].input,'参数接收中…');assert.equal(partial[0].state,'running');
const streaming=context.agentOutput(lines.split('\n').slice(0,4).join('\n'));
assert(streaming[0].input.includes('src/app.js'));
assert(!context.agentRedact('Authorization: Bearer xyz-secret').includes('xyz-secret'));
assert(!context.agentRedact('https://name:pass@example.com').includes('name:pass'));
const bounded=context.agentOutput(JSON.stringify({content:[{id:'large',name:'Read',input:{path:'a'}}]})+'\n'+JSON.stringify({content:[{tool_use_id:'large',content:'x'.repeat(60000)}]}));
assert(bounded[0].truncated);assert.equal(bounded[0].output.length,48000);
console.log('Tool transcript checks passed: CLI/SDK, stream deltas, IDs, results/errors, deduplication, escaping, redaction and bounds');

assert(!context.agentRedact('password="alpha beta"').includes('beta'));
assert(!context.agentRedact("PASSWORD='line one\nline two'").includes('line two'));
assert(!context.agentRedact('-----BEGIN PRIVATE KEY-----\nprivate bytes\n-----END PRIVATE KEY-----').includes('private bytes'));

const encoded=JSON.stringify({content:'password="alpha beta"',nested:{message:"TOKEN='two words'"}});
assert(!context.agentRedact(encoded).includes('alpha'));
assert(!context.agentRedact(encoded).includes('two words'));
assert(!context.agentRedact('[Live output]\n'+encoded+'\n'+encoded).includes('beta'));
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/cairn/server/static/design.js'),'utf8'),context);
(async()=>{
 const app=context.designFeatures(),run={id:'run',state:'running',streams:{stdout:'log'}},session={key:'session',main:false,runs:[run]};
 app.developmentState={sessions:[session]};app.selectedProjectId='project';app.agentSessionKey='session';app.$refs={};
 let fetches=0;
 context.fetch=async()=>{fetches++;return {ok:true,text:async()=>run.state==='running'?'initial log':'completed log'};};
 await app.loadAgentConversation();run.state='success';await app.loadAgentConversation();await app.loadAgentConversation();
 assert.equal(fetches,2,'Fetch terminal bytes once after a live run ends');assert.equal(app.agentRunTexts.run,'completed log');
 session.main=true;run.design_conversation=true;app.agentSessionKey='';app.agentRunStates.run='running';
 app.designEntries=[{id:'main',role:'Claude',text:'Main answer'}];
 context.fetch=async()=>({ok:true,text:async()=>JSON.stringify({content:[{type:'text',text:'Main answer'},{id:'main-read',name:'Read',input:{file_path:'README.md'}}]})});
 await app.loadAgentConversation();
 assert.equal(app.visibleDesignEntries().length,2,'Main chat loads tool details without duplicating persisted text');
 assert.equal(app.visibleDesignEntries()[1].kind,'tool');

 console.log('Session cache checks passed: final tool results are fetched on run completion');
})().catch(error=>{console.error(error);process.exitCode=1;});
