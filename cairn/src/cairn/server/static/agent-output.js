// Both CLI stream-json and SDK records become conversation entries, never raw status telemetry.
function agentOutput(text) {
  const entries = [];
  let active = null;
  const normal = value => {
    try { const data = JSON.parse(value); return data.summary || value; } catch { return value; }
  };
  const add = (role, value) => {
    if (!value?.trim()) return;
    if (role === 'Agent' && entries.at(-1)?.role === role && normal(entries.at(-1).text) === normal(value)) return;
    entries.push({role, text:value});
  };
  for (const line of text.split('\n')) {
    let item;
    try { item = JSON.parse(line); } catch { continue; }
    const event = item.event;
    if (event) {
      if (event.type === 'message_start') active = null;
      if (event.type === 'content_block_start' && event.content_block?.type === 'tool_use') {
        add('工具', event.content_block.name); active = null;
      }
      if (event.delta?.type === 'text_delta') {
        if (!active) { active = {role:'Agent',text:''}; entries.push(active); }
        active.text += event.delta.text;
      }
      continue;
    }
    const content = item.message?.content || item.content;
    if (Array.isArray(content)) {
      for (const block of content) {
        if (typeof block.text === 'string') {
          if (active && (block.text.startsWith(active.text) || active.text.startsWith(block.text))) active.text = block.text;
          else add('Agent', block.text);
        } else if (block.name && block.type !== 'tool_result') add('工具', block.name);
      }
    } else if (item.type === 'result' || ['success','error_during_execution','error_max_turns'].includes(item.subtype)) {
      add('Agent', item.result || '');
    }
  }
  return entries.filter(e => e.text.trim()).map((e,index) => ({...e,id:index,text:normal(e.text)}));
}
