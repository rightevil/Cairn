let workspaceGraph = null;
let workspaceGraphObserver = null;

function cairnApp() {
  return {
    ...developmentFeatures(), ...designFeatures(),
    view: 'list', sideTab: 'design', projects: [], project: null, selectedProjectId: '',
    loadingProject: false, loadingList: true, connectionError: '', polling: false,
    projectSearch: '', panelOpen: false, boardTab: 'graph', taskFilter: 'all', graphHistory: false, graphFocus: '', graphRunCursor: -1, selection: null, graphSignature: '',
    graphCards: [], graphTransform: '', graphZoom: 100,
    showNewProject: false, creatingProject: false, folderOpen: false, folderLoading: false, controlOpen: false,
    folderState: { directory: null, name: '', parent: null, breadcrumbs: [], entries: [] },
    folderError: '', folderSearch: '', folderRequest: 0,
    newFolderVisible: false, newFolderName: '', newFolderError: '', folderCreating: false,
    newProject: { name: '', directory: '' }, createError: '',
    deleteTarget: null, deleteConfirmation: '', deletingProject: false, deleteError: '',
    toast: '', toastType: 'info', toastTimer: null,
    fileSuggestions: [], fileIndex: 0, fileTimer: null,

    async init() {
      const returnFocus = new Map();
      for (const field of ['showNewProject', 'developmentArtifact.open', 'folderOpen', 'deleteTarget', 'controlOpen']) this.$watch(field, open => {
        if (open) {
          returnFocus.set(field, document.activeElement);
          this.$nextTick(() => {
            const modal = [...document.querySelectorAll('[role="dialog"]')].reverse().find(e => e.getBoundingClientRect().height && !e.inert);
            (modal?.querySelector('textarea:not(:disabled),input:not(:disabled)') || modal?.querySelector('button:not(:disabled)'))?.focus();
          });
        } else if (returnFocus.get(field)?.isConnected) returnFocus.get(field).focus();
      });
      await this.loadProjects();
      await this.route();
      window.addEventListener('pagehide', () => { this.designStream?.abort(); if (typeof disposeDesignTerminals === 'function') disposeDesignTerminals(); });
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
      const controls = [...event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]')]
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

    requestDeleteProject(project) {
      this.deleteTarget = { id: project.id, title: project.title };
      this.deleteConfirmation = ''; this.deleteError = '';
    },

    async deleteProject() {
      const target = this.deleteTarget;
      if (!target || this.deletingProject || this.deleteConfirmation !== target.title) return;
      this.deletingProject = true; this.deleteError = '';
      try {
        await this.api('DELETE', `/projects/${encodeURIComponent(target.id)}`);
        if (this.selectedProjectId === target.id) { this.backToList(); location.hash = ''; }
        this.deleteTarget = null;
        await this.loadProjects(); this.showToast('项目记录已删除，磁盘文件已保留');
      } catch (error) { this.deleteError = error.message; }
      finally { this.deletingProject = false; }
    },

    async route() {
      const params = new URLSearchParams(location.hash.slice(1));
      const id = params.get('project');
      if (!id) { this.backToList(); return; }
      if (this.selectedProjectId !== id) await this.openProject(id, params.get('stage'));
      else if (params.get('stage')) this.setStage(params.get('stage'), false);
    },

    navigateProject(id) { location.hash = new URLSearchParams({ project: id }).toString(); },
    navigateList() { location.hash = ''; },

    async openProject(id, stage = null) {
      this.controlOpen = false;
      this.designStream?.abort(); this.destroyGraph();
      this.selectedProjectId = id; this.project = null; this.selection = null;
      this.graphFocus = ''; this.taskFilter = 'all'; this.graphHistory = false; this.graphRunCursor = -1;
      this.designProject = ''; this.designCursor = 0;
      this.developmentControl = { kind: 'suggestion', content: '', scope: '', replaces: '' };
      this.designDraft = designFeatures().designDraft; this.designEntries = []; this.designError = '';
      this.fileSuggestions = [];
      this.agentSessionKey = ''; this.agentRunTexts = {};
      this.view = 'graph'; this.loadingProject = true; this.panelOpen = false;
      this.sideTab = stage === 'development' ? 'development' : stage === 'design' ? 'design'
        : this.projects.find(p => p.id === id)?.development?.phase === 'design' ? 'design' : 'development';
      try { await this.loadProject(id); if (this.selectedProjectId === id) await this.connectDesign(); }
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
      if (this.sideTab === 'development' && this.project?.project?.development) this.loadCollaboration(this.selectedProjectId);
      if (this.sideTab === 'design') this.connectDesign();
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
        failed: '失败', cancelled: '已取消', capacity: '等待额度', infrastructure: '推理请求失败，保留进度', blocked: '等待条件', recovered: '已回收',
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

    selectItem(kind, id) {
      this.selection = { kind, id }; this.panelOpen = true;
      this.syncGraphSelection();
      this.taskDetailTab = 'activity'; this.activeTaskRun = ''; this.taskRunText = ''; this.taskRunError = '';
      this.taskActivity = {intent:id, runs:[], next:null};
      if (kind === 'intent' && this.project?.project?.development) this.loadTaskActivity(id);
    },
    selectedIntent() { return this.selection?.kind === 'intent' ? this.project?.intents.find(i => i.id === this.selection.id) : null; },
    selectedFact() { return this.selection?.kind === 'fact' ? this.project?.facts.find(f => f.id === this.selection.id) : null; },
    selectedRuns() {
      if (this.taskActivity.intent === this.selectedIntent()?.id && this.taskActivity.runs.length) return this.taskActivity.runs;
      const id = this.selectedIntent()?.id;
      return this.developmentState.runs.filter(r => (this.runTask(r)?.id || r.intent_id) === id);
    },
    async resumeDevelopment() {
      try {
        await this.api('PUT', `/projects/${this.selectedProjectId}/status`, { status: 'active' });
        await this.loadProject(this.selectedProjectId); await this.loadProjects(); this.showToast('项目已继续');
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    async submitBlackboard() {
      if (this.project?.project.development) {
        await this.submitDevelopmentControl();
        if (!this.developmentControl.content.trim()) this.controlOpen = false;
        return;
      }
      if (!this.developmentControl.content.trim()) return;
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.developmentControl.content, creator: 'operator' });
        this.developmentControl.content = ''; await this.loadProject(this.selectedProjectId);
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    graphElements() {
      const facts = (this.project?.facts || []).filter(f => this.graphHistory || f.id === 'origin').map(f => ({ data: { id: 'f:' + f.id, ref: f.id, kind: 'fact', key: f.id, title: f.id === 'origin' ? '代码基线' : f.id === 'goal' ? '项目目标' : f.description,
        stateLabel: '⬡ 事实', tone: 'fact', roleLabel: '事实来源', activity: f.description, steps: [] } }));
      let intents = [...this.visibleWork(), ...(this.taskFilter === 'all' ? (this.project?.intents || []).filter(i => !i.work) : [])];
      if (this.graphFocus) {
        const focus = intents.find(i => i.id === this.graphFocus);
        const refs = new Set([this.graphFocus, ...(focus?.work?.dependencies || []), ...intents.filter(i => i.work?.dependencies?.includes(this.graphFocus)).map(i => i.id)]);
        intents = intents.filter(i => refs.has(i.id));
      }
      const nodes = [...facts, ...intents.map(i => {
        const state = i.work?.state || (i.to ? 'verified' : i.worker ? 'running' : 'ready');
        const runs = this.runningDevelopmentRuns().filter(r => r.intent_id === i.id || this.runTask(r)?.id === i.id);
        const waiting = this.taskIsWaiting(i);
        const needsChanges = ['request_changes','needs_design','reject','defer'].includes(i.work?.result?.output?.verdict);
        const tone = needsChanges ? 'waiting' : state === 'verified' ? 'verified' : waiting ? 'waiting'
          : ['implemented','reviewing','approved','merged'].includes(state) || state === 'running' && ['reviewer','architect'].includes(i.work?.role) ? 'review' : state === 'running' ? 'running' : 'ready';
        return { data: { id: 'i:' + i.id, ref: i.id, kind: 'intent', role: i.work?.role, state,
          key: i.work?.key || i.id, title: this.developmentText(i.description), stateLabel: this.graphStateLabel(i), tone,
          roleLabel: i.work ? this.developmentRole(i.work.role) : '探索任务', live: runs.length > 0,
          activity: runs.length ? runs.map(r => r.worker + ' · ' + this.developmentRunStatus(r) + (r.checkpoint ? ' · ' + r.checkpoint : '')).join('；')
            : this.taskWait(i) || (waiting ? i.work?.checkpoint || '等待条件满足，点击查看详情' : i.work ? this.developmentPhase(i) : '等待执行'),
          steps: this.taskPipeline(i) } };
      })];
      const known = new Set(nodes.map(n => n.data.id)), edges = [];
      const add = (source, target, type) => { if (known.has(source) && known.has(target)) edges.push({ data: { id: `${type}:${source}:${target}`, source, target, type } }); };
      for (const i of intents) {
        for (const from of i.from || []) add('f:' + from, 'i:' + i.id, 'fact');
        if (i.to) add('i:' + i.id, 'f:' + i.to, 'fact');
        for (const dependency of i.work?.dependencies || []) add('i:' + dependency, 'i:' + i.id, 'dependency');
        if (i.work?.subject) add('i:' + i.work.subject, 'i:' + i.id, 'review');
      }
      return [...nodes, ...edges];
    },
    destroyGraph() {
      workspaceGraphObserver?.disconnect(); workspaceGraphObserver = null;
      workspaceGraph?.destroy(); workspaceGraph = null; this.graphSignature = '';
      this.graphCards = []; this.graphTransform = ''; this.graphZoom = 100;
    },
    syncGraphViewport() {
      if (!workspaceGraph) return;
      const pan = workspaceGraph.pan(), zoom = workspaceGraph.zoom();
      this.graphTransform = `translate(${pan.x}px,${pan.y}px) scale(${zoom})`;
      this.graphZoom = Math.round(zoom * 100);
    },
    syncGraphCards() {
      if (!workspaceGraph) return;
      this.graphCards = workspaceGraph.nodes().map(node => ({ ...node.data(), x: node.position('x'), y: node.position('y') }));
      this.syncGraphViewport(); this.syncGraphSelection();
    },
    syncGraphSelection() {
      if (!workspaceGraph) return;
      const id = this.selection ? (this.selection.kind === 'fact' ? 'f:' : 'i:') + this.selection.id : '';
      workspaceGraph.elements().removeClass('related');
      if (this.panelOpen && id) workspaceGraph.getElementById(id).closedNeighborhood().addClass('related');
    },
    zoomGraph(factor, event = null) {
      if (!workspaceGraph) return;
      const box = this.$refs.blackboard.getBoundingClientRect();
      workspaceGraph.zoom({ level: Math.min(2.5, Math.max(.1, workspaceGraph.zoom() * factor)),
        renderedPosition: event ? { x: event.clientX - box.left, y: event.clientY - box.top } : { x: box.width / 2, y: box.height / 2 } });
    },
    revealGraphCard(card) {
      if (!workspaceGraph) return;
      const node = workspaceGraph.getElementById(card.id), p = node.renderedPosition(), zoom = workspaceGraph.zoom();
      if (p.x < 132 * zoom || p.x > workspaceGraph.width() - 132 * zoom || p.y < 78 * zoom || p.y > workspaceGraph.height() - 78 * zoom) workspaceGraph.center(node);
    },
    updateGraph() {
      if (this.view !== 'graph' || this.sideTab !== 'development' || this.boardTab !== 'graph') return;
      this.$nextTick(() => {
        const container = this.$refs.blackboard;
        if (!container || !container.clientWidth || !this.project) return;
        const elements = this.graphElements(), signature = JSON.stringify(elements);
        if (workspaceGraph && signature === this.graphSignature) { workspaceGraph.resize(); this.syncGraphSelection(); return; }
        this.graphSignature = signature;
        if (workspaceGraph) {
          const zoom = workspaceGraph.zoom(), pan = workspaceGraph.pan();
          const topology = items => items.map(e => e.data.id).sort().join('|');
          const previous = workspaceGraph.json().elements;
          const changed = topology([...(previous.nodes || []), ...(previous.edges || [])]) !== topology(elements);
          // Keep model positions when polling replaces node data; only topology changes need a layout.
          const positions = new Map(workspaceGraph.nodes().map(n => [n.id(), n.position()]));
          workspaceGraph.json({ elements: elements.map(e => positions.has(e.data.id) ? { ...e, position: positions.get(e.data.id) } : e) });
          if (changed) workspaceGraph.layout({ name: 'dagre', rankDir: 'LR', fit: false, nodeSep: 42, rankSep: 80 }).run();
          workspaceGraph.zoom(zoom); workspaceGraph.pan(pan);
          this.syncGraphCards();
          return;
        }
        const token = name => getComputedStyle(document.documentElement).getPropertyValue('--graph-' + name).trim();
        workspaceGraph = cytoscape({ container, elements, minZoom: 0.1, maxZoom: 2.5, autoungrabify: true,
          layout: { name: 'dagre', rankDir: 'LR', fit: false, padding: 40, nodeSep: 42, rankSep: 80 },
          style: [
            { selector:'node', style:{width:264,height:156,shape:'rectangle','background-opacity':0,'border-width':0,label:''} },
            { selector:'edge',style:{width:2,'line-color':token('muted'),'target-arrow-color':token('muted'),'target-arrow-shape':'triangle','curve-style':'bezier'} },
            { selector:'edge[type="fact"]',style:{'line-style':'dashed',opacity:.35} },
            { selector:'edge[type="review"]',style:{'line-style':'dotted','line-color':token('cool'),'target-arrow-color':token('cool')} },
            { selector:'edge.related',style:{width:3,'line-color':token('cool'),'target-arrow-color':token('cool'),opacity:1} },
          ] });
        workspaceGraph.on('pan zoom', () => this.syncGraphViewport());
        workspaceGraph.on('layoutstop', () => this.syncGraphCards());
        workspaceGraph.on('tap', 'node', event => { this.selectItem(event.target.data('kind'), event.target.data('ref')); });
        workspaceGraph.ready(() => requestAnimationFrame(() => {
          if (!workspaceGraph) return;
          workspaceGraph.resize(); workspaceGraph.zoom(.95);
          const running = workspaceGraph.nodes().filter(n => n.data('state')==='running');
          const active = running.length ? running : workspaceGraph.nodes().filter(n => n.data('state')==='reviewing');
          workspaceGraph.center(active.length ? active.first() : workspaceGraph.nodes().first());
          this.syncGraphCards();
        }));
        workspaceGraphObserver = new ResizeObserver(() => {
          if (!workspaceGraph) return;
          workspaceGraph.resize();
          if (this.panelOpen && this.selection) {
            const id = (this.selection.kind === 'fact' ? 'f:' : 'i:') + this.selection.id;
            const node = workspaceGraph.getElementById(id);
            if (node.length) workspaceGraph.center(node);
          }
        });
        workspaceGraphObserver.observe(container);
      });
    },
    graphStateLabel(intent) {
      const state = intent.work?.state || (intent.to ? 'verified' : intent.worker ? 'running' : 'ready');
      if (state === 'verified' && intent.work && intent.work.role !== 'implementer') return '◇ ' + this.developmentPhase(intent);
      if (state === 'ready' && intent.work?.dependencies?.some(id => this.project.intents.find(i => i.id === id)?.work?.state !== 'verified')) return '□ 依赖等待';
      if (state === 'ready' && this.taskIsWaiting(intent)) return this.developmentState.scheduling?.[intent.id]?.status === 'retry' ? '! 等待重试' : '! 等待资源';
      if (state === 'running' && ['reviewer','architect'].includes(intent.work?.role)) return '◇ 审查／决策中';
      if (state === 'reviewing' && intent.work?.role === 'implementer' && (!intent.work.head_commit || intent.work.result?.pending_design)) return '! 等待设计决策';
      return ({ready:'□ 待执行',running:'▶ 运行中',reviewing:'◇ 审查中',implemented:'◇ 待审查',approved:'◇ 待集成',merged:'◇ 待验证',verified:'✓ 已验证',waiting:'! 等待处理',failed:'! 失败',blocked:'! 阻塞',rejected:'! 需修订'})[state] || state;
    },
    focusRunning() {
      this.graphFocus = ''; this.taskFilter = 'all'; this.updateGraph();
      this.$nextTick(() => {
        if (!workspaceGraph) return;
        let nodes = workspaceGraph.nodes().filter(n => n.data('state') === 'running' || n.data('live'));
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
    focusRelated() {
      if (this.selection?.kind !== 'intent') return;
      this.taskFilter = 'all'; this.graphFocus = this.selection.id; this.fitVisibleGraph();
    },
    fitVisibleGraph() { this.updateGraph(); this.$nextTick(() => { if (this.boardTab === 'graph') workspaceGraph?.fit(undefined,60); }); },
    fitGraph() { this.graphFocus = ''; this.taskFilter = 'all'; this.fitVisibleGraph(); },
  };
}
