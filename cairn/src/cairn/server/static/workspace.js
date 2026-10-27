let workspaceGraph = null;
let workspaceGraphObserver = null;

function cairnApp() {
  return {
    ...developmentFeatures(), ...designFeatures(),
    view: 'list', sideTab: 'design', projects: [], project: null, selectedProjectId: '',
    loadingProject: false, loadingList: true, connectionError: '', polling: false,
    projectSearch: '', panelOpen: true, boardTab: 'tasks', taskFilter: 'all', graphHistory: false, graphFocus: '', graphRunCursor: -1, selection: null, graphSignature: '',
    showNewProject: false, creatingProject: false, folderOpen: false, folderLoading: false,
    folderState: { directory: null, name: '', parent: null, breadcrumbs: [], entries: [] },
    folderError: '', folderSearch: '', folderRequest: 0,
    newFolderVisible: false, newFolderName: '', newFolderError: '', folderCreating: false,
    newProject: { name: '', directory: '' }, createError: '',
    toast: '', toastType: 'info', toastTimer: null,
    fileSuggestions: [], fileIndex: 0, fileTimer: null,

    async init() {
      const returnFocus = new Map();
      for (const field of ['showNewProject', 'developmentArtifact.open', 'folderOpen']) this.$watch(field, open => {
        if (open) {
          returnFocus.set(field, document.activeElement);
          this.$nextTick(() => {
            const modal = [...document.querySelectorAll('[role="dialog"]')].reverse().find(e => e.getBoundingClientRect().height && !e.inert);
            modal?.querySelector('button:not(:disabled),input:not(:disabled)')?.focus();
          });
        } else if (returnFocus.get(field)?.isConnected) returnFocus.get(field).focus();
      });
      this.$watch('boardTab', () => this.rememberBoard());
      this.$watch('taskFilter', () => this.rememberBoard());
      await this.loadProjects();
      await this.route();
      setInterval(() => { if (this.selectedAgentSession()) this.loadAgentConversation(); }, 1000);
      window.addEventListener('hashchange', () => this.route());
      setInterval(async () => {
        if (this.polling) return;
        this.polling = true;
        try {
          await this.loadProjects();
          if (this.selectedProjectId) await this.loadProject(this.selectedProjectId);
        } catch (e) { this.connectionError = e.message; }
        finally { this.polling = false; }
      }, 5000);
    },

    trapDialog(event) {
      const controls = [...event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled), a[href]')]
        .filter(e => e.getBoundingClientRect().height);
      const target = event.shiftKey ? controls.at(-1) : controls[0];
      if (document.activeElement === (event.shiftKey ? controls[0] : controls.at(-1))) { event.preventDefault(); target?.focus(); }
    },

    async api(method, path, body) {
      const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(this.requestError(data.detail || `HTTP ${response.status}`));
      }
      return response.status === 204 ? null : response.json();
    },

    requestError(detail) {
      if (Array.isArray(detail)) return detail.map(e => e.msg || String(e)).join('；');
      return {
        'Operator token required': '请使用运行 Cairn 的本机地址打开工作台（localhost 或 127.0.0.1）。',
        'Dispatcher token required': '本机会话已失效，请刷新工作台。',
        'Project not found': '项目不存在，请返回项目列表。',
        'Independent review must approve this exact candidate': '此版本尚未通过独立评审。',
        'Design needs documents, a code baseline, a session and answers to all open questions': '请先完成方案文档、代码基线与待回答问题。',
        'Commit or set aside repository changes before publishing': '项目代码仍有未提交变更，请处理后再确认方案。',
      }[detail] || String(detail);
    },

    showToast(message, type = 'info') {
      this.toast = message; this.toastType = type;
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toast = ''; }, 5000);
    },

    async loadProjects() {
      try {
        this.projects = await this.api('GET', '/workspace/projects');
        this.connectionError = '';
      } catch (e) { this.connectionError = e.message; }
      finally { this.loadingList = false; }
    },

    filteredProjects() {
      return this.projects.filter(p => p.title.toLowerCase().includes(this.projectSearch.toLowerCase()));
    },

    async route() {
      const params = new URLSearchParams(location.hash.slice(1));
      const id = params.get('project');
      if (!id) { this.backToList(); return; }
      if (this.selectedProjectId !== id) await this.openProject(id, params.get('stage'));
      else if (params.get('stage')) this.setStage(params.get('stage'), false);
      this.boardTab = ['graph','tasks','facts'].includes(params.get('board')) ? params.get('board') : 'tasks';
      this.taskFilter = ['all','active','waiting','verified'].includes(params.get('filter')) ? params.get('filter') : 'all';
      this.updateGraph();
    },

    rememberBoard() {
      if (!this.selectedProjectId || this.view !== 'graph') return;
      const params = new URLSearchParams(location.hash.slice(1));
      params.set('board', this.boardTab); params.set('filter', this.taskFilter);
      history.replaceState(null, '', '#' + params); this.updateGraph();
    },

    navigateProject(id) { location.hash = new URLSearchParams({ project: id }).toString(); },
    navigateList() { location.hash = ''; },

    async openProject(id, stage = null) {
      this.designStream?.abort(); this.destroyGraph();
      this.selectedProjectId = id; this.project = null; this.selection = null;
      this.designProject = ''; this.designCursor = 0;
      this.developmentControl = { kind: 'suggestion', content: '', scope: '', replaces: '' };
      this.designDraft = designFeatures().designDraft; this.designEntries = []; this.designError = '';
      this.fileSuggestions = [];
      this.agentSessionKey = ''; this.agentRunTexts = {};
      this.view = 'graph'; this.loadingProject = true; this.panelOpen = true;
      this.sideTab = stage === 'development' ? 'development' : stage === 'design' ? 'design'
        : this.projects.find(p => p.id === id)?.development?.phase === 'design' ? 'design' : 'development';
      try { await this.loadProject(id); }
      catch (e) { if (this.selectedProjectId === id) this.showToast(e.message, 'error'); }
      finally { if (this.selectedProjectId === id) this.loadingProject = false; }
    },

    async loadProject(id) {
      const result = await this.api('GET', `/projects/${encodeURIComponent(id)}`);
      if (this.selectedProjectId !== id) return;
      this.project = result;
      await this.loadDevelopment(id);
      if (this.selectedProjectId === id) this.updateGraph();
    },

    backToList() {
      this.designStream?.abort(); this.destroyGraph();
      this.selectedProjectId = ''; this.project = null; this.view = 'list'; this.loadingProject = false;
    },

    setStage(stage, navigate = true) {
      this.agentSessionKey = '';
      this.sideTab = stage === 'development' ? 'development' : 'design';
      this.fileSuggestions = [];
      if (navigate) location.hash = new URLSearchParams({ project: this.selectedProjectId, stage: this.sideTab }).toString();
      this.updateGraph();
    },

    phaseLabel(project) {
      if (project.status === 'completed') return '已完成';
      return project.development?.phase === 'design' ? '方案设计' : '开发';
    },
    stageLabel(stage) {
      return { brief: '目标与范围', requirements: '需求梳理', ux: '体验设计', architecture: '架构设计', plan: '实施计划' }[stage] || stage;
    },
    statusLabel(status) {
      return { active: '进行中', stopped: '已暂停', completed: '已完成', running: '运行中', success: '成功',
        failed: '失败', cancelled: '已取消', capacity: '等待额度', blocked: '等待条件', recovered: '已回收',
        superseded: '已替代', unknown: '待核对', done: '已完成', absent: '确认不存在', ready: '待执行',
        proposed: '已提议', implemented: '已实现', reviewing: '待审查', approved: '已批准', merged: '已合并',
        verified: '已验证', rejected: '未通过', waiting: '等待条件' }[status] || status;
    },
    progressPercent(progress) { return progress?.total ? Math.round(progress.verified / progress.total * 100) : 0; },
    implementationTasks() {
      return this.developmentWork().filter(i => i.work.role === 'implementer' && i.work.state !== 'superseded');
    },
    projectProgress() {
      const tasks = this.implementationTasks();
      return { total: tasks.length, verified: tasks.filter(i => i.work.state === 'verified').length };
    },
    pendingQuestions() {
      const questions = this.designDraft.questions || [];
      return questions.filter(q => q.answer === null && q.depends_on.every(id => questions.some(p => p.id === id && p.answer !== null)));
    },
    canDesign() {
      return this.project?.project.development?.phase === 'design' && this.project?.project.status === 'active'
        && !this.designDraft.candidate.digest;
    },
    reviewLabel(value) {
      return { pending: '独立评审中', approve: '评审通过，可以确认', request_changes: '需要修改', reject: '评审未通过', needs_design: '需要继续设计' }[value] || value;
    },

    async pickProjectFolder() {
      this.folderOpen = true; this.createError = '';
      await this.browseFolders(this.newProject.directory || null);
    },
    async browseFolders(directory) {
      if (this.folderCreating) return;
      this.newFolderVisible = false;
      const request = ++this.folderRequest;
      this.folderLoading = true; this.folderError = ''; this.folderSearch = '';
      try {
        const state = await this.api('GET', '/workspace/folders' + (directory === null ? '' : '?directory=' + encodeURIComponent(directory)));
        if (request === this.folderRequest) this.folderState = state;
      } catch (e) {
        if (request === this.folderRequest) this.folderError = e.message === 'Not Found'
          ? '后端仍是旧版本，请重新启动 Cairn，再刷新页面。' : e.message;
      }
      finally { if (request === this.folderRequest) this.folderLoading = false; }
    },
    filteredFolders() {
      return this.folderState.entries.filter(entry => entry.name.toLowerCase().includes(this.folderSearch.toLowerCase()));
    },
    cancelFolder() {
      if (this.folderCreating) return;
      this.newFolderVisible = false;
      this.folderRequest++; this.folderLoading = false; this.folderOpen = false;
    },
    startNewFolder() {
      this.newFolderVisible = true; this.newFolderName = ''; this.newFolderError = '';
      this.$nextTick(() => this.$refs.newFolderName.focus());
    },
    async createFolder() {
      if (this.folderCreating || this.folderLoading || !this.folderState.directory || !this.newFolderName.trim()) return;
      this.folderCreating = true; this.newFolderError = '';
      try {
        this.folderState = await this.api('POST', '/workspace/folders', {
          directory: this.folderState.directory, name: this.newFolderName.trim(),
        });
        this.newFolderVisible = false; this.folderSearch = ''; this.folderError = '';
        this.$nextTick(() => this.$refs.confirmFolder.focus());
      } catch (e) { this.newFolderError = e.message; }
      finally { this.folderCreating = false; }
    },
    confirmFolder() {
      if (this.folderLoading || this.folderCreating || this.folderError || !this.folderState.directory) return;
      this.newProject = { directory: this.folderState.directory, name: this.folderState.name };
      this.cancelFolder();
    },
    async createProject() {
      if (this.creatingProject || !this.newProject.directory) return;
      this.creatingProject = true; this.createError = '';
      try {
        const result = await this.api('POST', '/workspace/projects', this.newProject);
        this.showNewProject = false; this.newProject = { name: '', directory: '' };
        await this.loadProjects(); this.navigateProject(result.project.id);
        this.showToast('项目已创建，可以开始讨论方案');
      } catch (e) { this.createError = e.message; }
      finally { this.creatingProject = false; }
    },

    onDesignInput() {
      this.designCommandIndex = 0; this.designCommandOpen = true; this.designMessageId = '';
      clearTimeout(this.fileTimer);
      const query = this.designInput.match(/(?:^|\s)@([^\s@]*)$/)?.[1];
      this.fileSuggestions = [];
      if (query === undefined) return;
      const projectId = this.selectedProjectId;
      this.fileTimer = setTimeout(async () => {
        try {
          const files = await this.api('GET', `/workspace/projects/${projectId}/files?query=${encodeURIComponent(query)}`);
          if (projectId === this.selectedProjectId && this.designInput.match(/(?:^|\s)@([^\s@]*)$/)?.[1] === query) {
            this.fileSuggestions = files; this.fileIndex = 0;
          }
        } catch (e) { this.designError = e.message; }
      }, 180);
    },
    chooseFile(file) {
      this.designInput = this.designInput.replace(/@[^\s@]*$/, '@' + (file.includes(' ') ? JSON.stringify(file) : file) + ' ');
      this.fileSuggestions = []; this.$refs.designComposer.focus();
    },
    composerKey(event) {
      if (event.isComposing) return;
      if (!this.fileSuggestions.length) { this.designKey(event); return; }
      if (event.key === 'Escape') { this.fileSuggestions = []; return; }
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); this.fileIndex = (this.fileIndex + (event.key === 'ArrowDown' ? 1 : -1) + this.fileSuggestions.length) % this.fileSuggestions.length;
      } else if (['Tab', 'Enter'].includes(event.key)) {
        event.preventDefault(); this.chooseFile(this.fileSuggestions[this.fileIndex]);
      }
    },

    selectItem(kind, id) { this.selection = { kind, id }; this.panelOpen = true; },
    selectedIntent() { return this.selection?.kind === 'intent' ? this.project?.intents.find(i => i.id === this.selection.id) : null; },
    selectedFact() { return this.selection?.kind === 'fact' ? this.project?.facts.find(f => f.id === this.selection.id) : null; },
    selectedRuns() { return this.developmentState.runs.filter(r => r.intent_id === this.selectedIntent()?.id); },
    async resumeDevelopment() {
      try {
        await this.api('PUT', `/projects/${this.selectedProjectId}/status`, { status: 'active' });
        await this.loadProject(this.selectedProjectId); await this.loadProjects(); this.showToast('项目已继续');
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    async submitBlackboard() {
      if (this.project?.project.development) return this.submitDevelopmentControl();
      if (!this.developmentControl.content.trim()) return;
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.developmentControl.content, creator: 'operator' });
        this.developmentControl.content = ''; await this.loadProject(this.selectedProjectId);
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    graphElements() {
      const facts = (this.project?.facts || []).filter(f => this.graphHistory || f.id === 'origin').map(f => ({ data: { id: 'f:' + f.id, ref: f.id, kind: 'fact', label: f.id === 'origin' ? '代码基线' : f.id === 'goal' ? '项目目标' : f.description.slice(0, 30) } }));
      let intents = [...this.visibleWork(), ...(this.taskFilter === 'all' ? (this.project?.intents || []).filter(i => !i.work) : [])];
      if (this.graphFocus) {
        const focus = intents.find(i => i.id === this.graphFocus);
        const refs = new Set([this.graphFocus, ...(focus?.work?.dependencies || []), ...intents.filter(i => i.work?.dependencies.includes(this.graphFocus)).map(i => i.id)]);
        intents = intents.filter(i => refs.has(i.id));
      }
      const nodes = [...facts, ...intents.map(i => ({ data: { id: 'i:' + i.id, ref: i.id, kind: 'intent',
        label: this.graphStateLabel(i) + '\n' + (i.work?.key || i.id) + '\n' + this.developmentText(i.description).slice(0, 34), state: i.work?.state || (i.to ? 'verified' : i.worker ? 'running' : 'ready') } }))];
      const known = new Set(nodes.map(n => n.data.id)), edges = [];
      const add = (source, target, type) => { if (known.has(source) && known.has(target)) edges.push({ data: { id: `${type}:${source}:${target}`, source, target, type } }); };
      for (const i of intents) {
        for (const from of i.from || []) add('f:' + from, 'i:' + i.id, 'fact');
        if (i.to) add('i:' + i.id, 'f:' + i.to, 'fact');
        for (const dependency of i.work?.dependencies || []) add('i:' + dependency, 'i:' + i.id, 'dependency');
      }
      return [...nodes, ...edges];
    },
    destroyGraph() {
      workspaceGraphObserver?.disconnect(); workspaceGraphObserver = null;
      workspaceGraph?.destroy(); workspaceGraph = null; this.graphSignature = '';
    },
    updateGraph() {
      if (this.view !== 'graph' || this.sideTab !== 'development' || this.boardTab !== 'graph') return;
      this.$nextTick(() => {
        const container = this.$refs.blackboard;
        if (!container || !container.clientWidth || !this.project) return;
        const elements = this.graphElements(), signature = JSON.stringify(elements);
        if (workspaceGraph && signature === this.graphSignature) { workspaceGraph.resize(); return; }
        this.graphSignature = signature;
        if (workspaceGraph) {
          const zoom = workspaceGraph.zoom(), pan = workspaceGraph.pan();
          const topology = items => items.map(e => e.data.id).sort().join('|');
          const previous = workspaceGraph.json().elements;
          const changed = topology([...(previous.nodes || []), ...(previous.edges || [])]) !== topology(elements);
          workspaceGraph.json({ elements });
          if (changed) workspaceGraph.layout({ name: 'dagre', rankDir: 'LR', fit: false, nodeSep: 42, rankSep: 80 }).run();
          workspaceGraph.zoom(zoom); workspaceGraph.pan(pan);
          return;
        }
        const token = name => getComputedStyle(document.documentElement).getPropertyValue('--graph-' + name).trim();
        workspaceGraph = cytoscape({ container, elements, minZoom: 0.1, maxZoom: 2.5,
          layout: { name: 'dagre', rankDir: 'LR', padding: 40, nodeSep: 42, rankSep: 80 },
          style: [
            { selector:'node', style:{label:'data(label)','font-family':getComputedStyle(document.documentElement).getPropertyValue('--font-body').trim(),'font-size':13,color:token('ink'),'text-wrap':'wrap','text-max-width':172,'text-valign':'center','background-color':token('paper'),shape:'roundrectangle',width:200,height:96,'border-width':2,'border-color':token('muted')} },
            { selector:'node[kind="fact"]',style:{shape:'hexagon','background-color':token('paper'),'border-color':token('cool'),color:token('cool'),'border-style':'dashed',opacity:.65} },
            { selector:'node[state="running"]',style:{'border-color':token('accent'),'border-width':4,color:token('accent')} },
            { selector:'node[state="verified"]',style:{'border-color':token('ok'),color:token('ok'),opacity:.75} },
            { selector:'node[state="reviewing"], node[state="approved"], node[state="merged"], node[state="implemented"]',style:{'border-color':token('cool'),color:token('cool'),shape:'cutrectangle'} },
            { selector:'node[state="waiting"], node[state="failed"], node[state="blocked"]',style:{'border-color':token('bad'),color:token('bad'),'border-style':'double'} },
            { selector:'edge',style:{width:2,'line-color':token('muted'),'target-arrow-color':token('muted'),'target-arrow-shape':'triangle','curve-style':'bezier'} },
            { selector:'edge[type="fact"]',style:{'line-style':'dashed',opacity:.35} },
            { selector:':selected',style:{'border-color':token('ink'),'border-width':4} },
          ] });
        workspaceGraph.on('tap', 'node', event => { this.selectItem(event.target.data('kind'), event.target.data('ref')); if (event.target.data('kind') === 'intent') { this.graphFocus = event.target.data('ref'); this.updateGraph(); } });
        workspaceGraphObserver = new ResizeObserver(() => workspaceGraph?.resize());
        workspaceGraphObserver.observe(container);
      });
    },
    graphStateLabel(intent) {
      const state = intent.work?.state || (intent.to ? 'verified' : intent.worker ? 'running' : 'ready');
      if (state === 'ready' && intent.work?.dependencies.some(id => this.project.intents.find(i => i.id === id)?.work?.state !== 'verified')) return '□ 依赖等待';
      return ({ready:'□ 待执行',running:'▶ 运行中',reviewing:'◇ 审查中',implemented:'◇ 待审查',approved:'◇ 待集成',merged:'◇ 验证中',verified:'✓ 已验证',waiting:'! 等待处理',failed:'! 失败',blocked:'! 阻塞'})[state] || state;
    },
    focusRunning() {
      this.graphFocus = ''; this.taskFilter = 'all'; this.updateGraph();
      this.$nextTick(() => {
        if (!workspaceGraph) return;
        let nodes = workspaceGraph.nodes().filter(n => n.data('state') === 'running');
        if (!nodes.length) nodes = workspaceGraph.nodes().filter(n => ['reviewing','implemented','approved','merged'].includes(n.data('state')));
        if (!nodes.length) { this.showToast('当前没有执行或审查中的任务'); return; }
        this.graphRunCursor = (this.graphRunCursor + 1) % nodes.length;
        const target = nodes.filter((n,index) => index === this.graphRunCursor);
        workspaceGraph.fit(target,20);
        if (workspaceGraph.zoom() > 1.25) workspaceGraph.zoom(1.25);
        workspaceGraph.center(target);
        if (nodes.length > 1) this.showToast(`已定位 ${target[0].data('ref')}（${this.graphRunCursor + 1}/${nodes.length}），再次点击查看下一项`);
      });
    },
    fitGraph() { this.graphFocus = ''; this.updateGraph(); this.$nextTick(() => workspaceGraph?.fit(undefined,40)); },
  };
}
