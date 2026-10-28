// Display only: raw evidence is never changed.
function agentRedact(value) {
  const secret = /(?:api[-_]?key|token|secret|password|authorization|credential)/i;
  const redact = text => String(text ?? '')
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g, '[已隐藏私钥]')
    .replace(/((?:[\w-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)[\w-]*)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/gi, '$1"[已隐藏]"')
    .replace(/\bBearer\s+[^\s"',}]+/gi, 'Bearer [已隐藏]')
    .replace(/((?:[\w-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)[\w-]*)["']?\s*[:=]\s*["']?)([^\s"',;}]+)/gi, '$1[已隐藏]')
    .replace(/\bsk-[A-Za-z0-9_-]{12,}/g, '[已隐藏]')
    .replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/g, '$1[已隐藏]@');
  const clean = item => typeof item === 'string' ? redact(item) : Array.isArray(item) ? item.map(clean)
    : item && typeof item === 'object' ? Object.fromEntries(Object.entries(item).map(([key,val]) => [key, secret.test(key) ? '[已隐藏]' : clean(val)])) : item;
  if (typeof value !== 'string') return JSON.stringify(clean(value), null, 2) ?? '';
  // Decode raw JSONL before masking, so escaped quotes cannot expose credential tails.
  try { return JSON.stringify(clean(JSON.parse(value)), null, 2); } catch {}
  return redact(value.split('\n').map(line => {
    try { return JSON.stringify(clean(JSON.parse(line))); } catch { return line; }
  }).join('\n'));
}
function agentOutput(text) {
  const entries=[],calls=new Map(),blocks=new Map();
  let active=null,message='',serial=0;
  const normal=value=>{try{return JSON.parse(value).summary||value;}catch{return value;}};
  const add=value=>{
    if(!value?.trim())return;
    value=normal(value);
    if(active&&(value.startsWith(active.text)||active.text.startsWith(value)))active.text=value;
    else if(entries.at(-1)?.text!==value){active={role:'Agent',text:value,id:'text:'+serial++};entries.push(active);}
  };
  const tool=(block,parent,timestamp)=>{
    const id=block.id||'unidentified:'+serial++;
    if(!calls.has(id)){
      const entry={id:'tool:'+id,callId:id,parentId:parent||null,role:'工具',kind:'tool',name:block.name||'工具',text:block.name||'工具',input:'',output:'',state:'running',startedAt:timestamp||null};
      calls.set(id,entry);entries.push(entry);
    }
    const entry=calls.get(id);
    if(block.input&&Object.keys(block.input).length)entry.input=agentRedact(block.input);
    active=null;return entry;
  };
  for(const line of text.split('\n')){
    let item;try{item=JSON.parse(line);}catch{continue;}
    const event=item.event||(item.type==='content_block_delta'?item:null);
    if(event){
      if(event.type==='message_start'){active=null;message=event.message?.id||'message:'+serial++;blocks.clear();}
      const index=event.index??0;
      if(event.type==='content_block_start'){
        const block=event.content_block;
        if(block?.type==='tool_use')blocks.set(index,{entry:tool(block,item.parent_tool_use_id,item.timestamp||event.timestamp),json:''});
        else if(block?.text)add(block.text);
      }
      if(event.delta?.type==='input_json_delta'){
        const target=blocks.get(index);
        if(target){target.json+=event.delta.partial_json||'';try{target.entry.input=agentRedact(JSON.parse(target.json));}catch{target.entry.input='参数接收中…';}}
      }
      if(event.delta?.type==='text_delta'){
        if(!active){active={role:'Agent',text:'',id:message+':'+index};entries.push(active);}
        active.text+=event.delta.text;
      }
      continue;
    }
    const content=item.message?.content||item.content;
    if(Array.isArray(content)){
      for(const block of content){
        if(block.type==='tool_result'||block.tool_use_id){
          const entry=calls.get(block.tool_use_id);
          if(entry){
            const result=typeof block.content==='string'?block.content:(block.content||[]).map(b=>b.text||(b.type==='image'?'[图片输出]':'')).join('\n');
            entry.output=agentRedact(result);entry.state=block.is_error?'error':'success';
            entry.finishedAt=item.timestamp||null;
            const meta=item.tool_use_result||{};
            entry.exitCode=meta.exitCode??meta.exit_code??null;
            entry.durationMs=meta.durationMs??meta.duration_ms??null;
            if(entry.exitCode!==null&&entry.exitCode!==0)entry.state='error';
            entry.stderr=typeof meta.stderr==='string'?agentRedact(meta.stderr):'';
          }
          active=null;
        }else if(block.name)tool(block,item.parent_tool_use_id,item.timestamp);
        else if(typeof block.text==='string')add(block.text);
      }
    }else if(item.type==='result'||['success','error_during_execution','error_max_turns'].includes(item.subtype))add(item.result||'');
  }
  return entries.filter(e=>e.text.trim()).map(e=>({...e,text:agentRedact(normal(e.text)),...(e.kind==='tool'?{input:e.input.slice(0,24000),output:e.output.slice(0,48000),stderr:(e.stderr||'').slice(0,24000),truncated:e.input.length>24000||e.output.length>48000||(e.stderr||'').length>24000}:{})}));
}
function agentToolLabel(entry){return {running:'等待工具结果',success:'已完成',error:'执行失败'}[entry.state]||'状态未知';}
function agentEscape(text){return String(text||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function agentToolHTML(entry,open=false){
  let target='';
  try { const args=JSON.parse(entry.input); target=args.file_path||args.path||args.command||args.url||args.pattern||args.skill||''; } catch {}
  const timing=[entry.startedAt&&'开始 '+entry.startedAt,entry.finishedAt&&'结束 '+entry.finishedAt,entry.durationMs!=null&&'耗时 '+entry.durationMs+' ms',entry.exitCode!=null&&'退出码 '+entry.exitCode].filter(Boolean).join(' / ');
  return `<details class="tool-call state-${entry.state}" data-call="${agentEscape(entry.callId)}" ${open?'open':''}><summary><span class="tool-name">${agentEscape(entry.name)}</span><span class="tool-target">${agentEscape(String(target).slice(0,160))}</span><span class="tool-state">${agentToolLabel(entry)}</span></summary><div class="tool-details"><p class="muted">调用 ${agentEscape(entry.callId)}${entry.parentId?' / 父调用 '+agentEscape(entry.parentId):''}</p>${timing?'<p class="muted">'+agentEscape(timing)+'</p>':''}<h4>输入</h4><pre>${agentEscape(entry.input||'未提供输入')}</pre><h4>输出</h4><pre>${agentEscape(entry.output||'尚未收到工具结果；不据此判断是否仍在运行。')}</pre>${entry.stderr?'<h4>错误输出</h4><pre>'+agentEscape(entry.stderr)+'</pre>':''}${entry.truncated?'<p class="muted">显示已截断，可在运行窗口查看完整记录。</p>':''}</div></details>`;
}
