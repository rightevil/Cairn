function desktopSettings() {
  return {
    form:null, path:'', error:'', message:'', busy:false, saved:false, section:'providers',
    sections:[{id:'providers',label:'供应商'},{id:'agents',label:'Agent 与角色'},{id:'scheduling',label:'并发与超时'},{id:'skills',label:'内置设计 Skill'}],
    roles:[{id:'planner',label:'规划'},{id:'architect',label:'设计决策'},{id:'implementer',label:'实施'},{id:'reviewer',label:'独立审查'}],
    runtimeFields:[{key:'max_workers',label:'总运行名额'},{key:'max_project_workers',label:'单项目运行名额'},{key:'max_running_projects',label:'同时推进的项目数'},
      {key:'max_implementers',label:'实施并发上限',optional:true},{key:'max_reviewers',label:'审查并发上限',optional:true},{key:'interval',label:'调度间隔（秒）'},{key:'healthcheck_timeout',label:'启动检查超时（秒）'}],
    init() {
      if (window.pywebview?.api) this.load();
      else window.addEventListener('pywebviewready', () => this.load(), {once:true});
    },
    accept(result) {
      if (!result.ok) { this.error = result.error; return; }
      this.form = result; this.path = result.path; this.error = result.warning || ''; this.message = ''; this.saved = false;
      this.form.tasks.development_timeout ??= 3600;
    },
    async load() {
      try { this.accept(await window.pywebview.api.get_settings()); }
      catch { this.error = '无法读取桌面配置，请重新打开设置。'; }
    },
    addProvider() {
      this.form.providers.push({id:crypto.randomUUID(),name:'',protocol:'anthropic',base_url:'',api_key:'',models_text:''});
      this.saved = false;
    },
    removeProvider(index) {
      const provider = this.form.providers[index];
      if (this.form.workers.some(w => w.provider === provider.id)) { this.error = '请先为使用此供应商的 Agent 选择其他供应商。'; return; }
      this.form.providers.splice(index,1); this.saved = false;
    },
    providerModels(id) { return (this.form.providers.find(p => p.id === id)?.models_text || '').split('\n').map(s=>s.trim()).filter(Boolean); },
    addWorker() {
      this.form.workers.push({name:'',type:'claudecode',tier:'standard',roles:['implementer'],command:'',model:'',provider:'',max_running:1,priority:0});
      this.saved = false;
    },
    async save() {
      if (this.busy || !this.form) return;
      this.busy = true; this.error = ''; this.message = '';
      try {
        const result = await window.pywebview.api.save_settings(JSON.parse(JSON.stringify(this.form)));
        if (!result.ok) { this.error = result.error; return; }
        this.form.revision = result.revision; this.saved = true; this.message = result.message;
        // Reload clears entered secrets and obtains their configured markers without displaying values.
        const refreshed = await window.pywebview.api.get_settings();
        if (refreshed.ok) { this.form = refreshed; this.saved = true; }
      } catch { this.error = '保存失败，请检查文件是否可写，或重新打开设置。'; }
      finally { this.busy = false; }
    },
  };
}
