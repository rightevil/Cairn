function developmentFeatures() {
  return {
    developmentState: { runs: [], events: [], operations: [] },
    developmentError: '',
    developmentProjectId: '',
    developmentNow: Date.now(),
    developmentControl: { kind: 'suggestion', content: '', scope: '', replaces: '' },
    controlSending: false,
    developmentArtifact: { open: false, name: '', text: '' },

    async loadDevelopment(id) {
      this.developmentNow = Date.now();
      if (this.developmentProjectId !== id) {
        this.developmentState = { runs: [], events: [], operations: [] };
        this.developmentProjectId = id;
      }
      if (!this.project?.project?.development) return;
      try {
        const result = await this.api('GET', `/projects/${id}/development`);
        if (this.selectedProjectId !== id) return;
        const selected = this.selectedAgentSession();
        this.developmentState = result;
        if (selected && !this.selectedAgentSession()) {
          this.agentSessionKey = (result.sessions || []).find(s => s.runs.some(r => selected.runs.some(old => old.id === r.id)))?.key || '';
        }
        if (this.selectedProjectId === id && this.loadDesign) await this.loadDesign(id);
        if (this.selectedProjectId === id) await this.loadAgentConversation();
        if (this.selectedProjectId === id) this.developmentError = '';
      } catch (error) { if (this.selectedProjectId === id) this.developmentError = error.message; }
    },

    developmentWork() {
      return (this.project?.intents || []).filter(intent => intent.work && intent.work.purpose !== 'design' && intent.work.state !== 'superseded').slice().reverse();
    },

    visibleWork() {
      return this.developmentWork().filter(i => this.taskFilter === 'active' ? ['running','implemented','reviewing','approved','merged'].includes(i.work.state)
        : this.taskFilter === 'waiting' ? i.work.state === 'waiting' || i.work.state === 'ready' && (i.work.dependencies || []).some(id => this.project.intents.find(d => d.id === id)?.work?.state !== 'verified')
        : this.taskFilter === 'verified' ? i.work.state === 'verified' : true);
    },

    runningDevelopmentRuns() {
      return this.developmentState.runs.filter(run => run.state === 'running');
    },

    developmentTime(timestamp) {
      if (!timestamp) return '暂无记录';
      return new Date(timestamp).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    },

    developmentRunStatus(run) {
      if (run.state !== 'running') return this.statusLabel(run.state);
      if (run.cancel_requested) return '正在停止，等待进程收尾';
      const heartbeat = Date.parse(run.heartbeat_at);
      if (!Number.isFinite(heartbeat) || this.developmentNow - heartbeat > 60000) return '心跳待确认，等待执行器核对';
      if (!run.pid && run.role !== 'integration') return '准备工作区／启动进程';
      const intent = this.project?.intents?.find(item => item.id === run.intent_id);
      if (intent?.work?.purpose === 'design') return '方案设计访谈中';
      if (run.role === 'planner') return intent?.work?.result?.review_feedback ? '修订方案中' : '规划中';
      return { architect: '审查／设计中', reviewer: '独立审查中', implementer: '实现中', integration: '合并与集成验证中' }[run.role] || '执行中';
    },

    developmentText(text) {
      const fixed = {
        'Plan the current milestone within the project design': '按项目设计规划当前里程碑',
        'Design conversation': '方案设计对话',
        'Replan affected work using current user directives': '根据当前用户指令重新规划受影响的工作',
        'Evaluate the pending suggestion against the updated project direction': '按更新后的项目方向独立评估待处理建议',
        'Plan the next milestone within the approved project design': '在已确认的项目设计内规划下一里程碑',
        'Choose the next useful milestone within the approved design': '在已确认设计内选择下一项有价值的里程碑',
        'Independently review the proposed plan and project fit': '独立审查计划及其与项目目标的一致性',
        'Verify the milestone against raw integration evidence': '根据原始集成证据验收里程碑',
        'Independently inspect the diff, acceptance and check evidence': '独立检查代码变更、验收条件和测试证据',
        'Review progress and remaining work against the agreed project design': '按已确认的项目设计审查进展与剩余工作',
        'Decide a new approach using repeated failure evidence': '根据重复失败的证据决定新的实施方法',
        'Resolve failed integration checks without undoing completed external actions': '裁决集成检查失败，保留已完成的外部操作',
        'Cancel running work and stop new dispatch': '取消当前执行并停止派发新任务',
        'New user directive requires further work': '收到新用户指令，项目需要继续推进',
        'Included in an independently approved plan': '已纳入通过独立审查的计划',
        'Controls changed before result application': '应用结果前指令已变更，旧结果不再生效',
        'Delivery mode: local': '交付方式已设为本地审查合并',
        'Delivery mode: github': '交付方式已设为 GitHub PR',
        suggestion: '建议', directive: '用户指令',
      };
      if (fixed[text]) return fixed[text];
      const prefixes = {
        'Independently evaluate suggestion ': '独立评估建议 ',
        'Incorporate evaluated suggestion ': '将已评估的建议纳入计划 ',
        'Resolve design dispute: ': '裁决设计争议：',
        'Resolve repeated review disagreement using evidence: ': '根据证据裁决反复出现的审查分歧：',
        'Decide implementation design gap: ': '裁决实现中的设计缺口：',
        'Direction review: ': '方向审查：', 'Review: ': '审查意见：',
        'revision ': '指令版本 ', 'planner: ': '规划执行者：', 'architect: ': '设计执行者：',
        'implementer: ': '实现执行者：', 'reviewer: ': '审查执行者：',
      };
      for (const [prefix, label] of Object.entries(prefixes)) {
        if (typeof text === 'string' && text.startsWith(prefix)) return label + text.slice(prefix.length);
      }
      return this.statusLabel(text);
    },

    developmentRole(role, run = null) {
      const work = this.project?.intents?.find(i => i.id === run?.intent_id)?.work;
      const tier = run?.result?.snapshot?.tier || (role === 'implementer' ? work?.model_tier : null);
      const label = { planner: '规划', implementer: '实现', reviewer: '独立审查', architect: '设计／方向', integration: '合并与集成验证' }[role] || role;
      return label + (tier ? ' · ' + (tier === 'strong' ? '强模型' : '普通模型') : '');
    },

    taskWait(intent) {
      return this.developmentState.scheduling?.[intent.id]?.detail || '';
    },

    planParallelWidth() {
      const tasks = this.designDraft.work || [], levels = new Map(), visiting = new Set();
      const level = task => {
        if (levels.has(task.key)) return levels.get(task.key);
        if (visiting.has(task.key)) throw new Error('cycle');
        visiting.add(task.key);
        const deps = task.dependencies || [];
        const value = deps.length ? 1 + Math.max(...deps.map(key => {
          const dep = tasks.find(t => t.key === key);
          if (!dep) throw new Error('missing');
          return level(dep);
        })) : 0;
        visiting.delete(task.key); levels.set(task.key, value); return value;
      };
      try {
        const counts = {};
        for (const task of tasks) { const n = level(task); counts[n] = (counts[n] || 0) + 1; }
        return Math.max(0, ...Object.values(counts));
      } catch { return 0; }
    },

    developmentPhase(intent) {
      const work = intent.work;
      if (work.state === 'superseded') return '已替代';
      if (work.role === 'implementer' && work.state === 'reviewing' && (!work.head_commit || work.result?.pending_design)) return '等待设计决策';
      if (work.role === 'implementer') return { ready: '待实现', running: '实现中', waiting: '等待条件', implemented: '已实现', reviewing: '已实现 · 待审查', approved: '已实现 · 待合并', merged: '已合并 · 待验证', verified: '已验证', rejected: '需修订' }[work.state] || work.state;
      if (work.role === 'planner') {
        if (work.state === 'running') return work.result?.review_feedback ? '修订方案中' : '规划中';
        if (work.state === 'ready') return work.result?.review_feedback ? '待修订 · 等待领取' : '待规划 · 等待领取';
        return work.state === 'verified' ? '已决定' : work.state === 'reviewing' ? '提议 · 待审查' : work.state === 'waiting' ? '等待条件' : '提议中';
      }
      if (work.result?.output?.verdict) return { approve: '审查通过', request_changes: '需修订', needs_design: '需设计决策', reject: '已拒绝', defer: '暂缓', experiment: '采用实验' }[work.result.output.verdict];
      return work.state === 'verified' ? '已决定' : work.state === 'waiting' ? '等待条件' : '审查／决策中';
    },

    controlState(value) {
      return { received: '已收到 · 待评估', accepted: '已采纳 · 待检查点', experiment: '采用实验', deferred: '暂缓', rejected: '未采纳', applied: '已生效', completed: '已完成验证', replaced: '已替代', revoked: '已撤销' }[value] || value;
    },

    activeDirectives() {
      return (this.project?.hints || []).filter(h => h.control?.kind === 'directive' && !['revoked', 'replaced'].includes(h.control.state));
    },

    async submitDevelopmentControl() {
      if (!this.developmentControl.content.trim() || this.controlSending) return;
      const projectId = this.selectedProjectId, kind = this.developmentControl.kind;
      this.controlSending = true;
      try {
        await this.api('POST', `/projects/${projectId}/development/controls`, {
          kind: this.developmentControl.kind, content: this.developmentControl.content.trim(),
          scope: this.developmentControl.scope ? [this.developmentControl.scope] : [], replaces: this.developmentControl.replaces || null,
        });
        if (this.selectedProjectId !== projectId) return;
        this.developmentControl.content = '';
        this.developmentControl.replaces = '';
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(kind === 'directive' ? '指令已采纳，等待安全检查点生效' : '建议已提交独立评估');
      } catch (error) { this.showToast(error.message, 'error'); }
      finally { this.controlSending = false; }
    },

    async revokeDevelopmentControl(id) {
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/development/controls/${id}/revoke`, {});
        await this.loadProject(this.selectedProjectId);
        this.showToast('已记录撤销；受影响工作将在检查点调整');
      } catch (error) { this.showToast(error.message, 'error'); }
    },

    async stopDevelopment() {
      try {
        const result = await this.api('POST', `/projects/${this.selectedProjectId}/development/stop`, {});
        await this.loadProject(this.selectedProjectId);
        this.showToast(result.state === 'stopped' ? '已暂停，工作与证据已保留' : '正在停止；运行记录会继续显示实际收尾状态');
      } catch (error) { this.showToast(error.message, 'error'); }
    },

    async viewDevelopmentEvidence(run, index) {
      return this.openDevelopmentPreview(`/projects/${this.selectedProjectId}/development/runs/${run.id}/evidence/${index}`, run.evidence[index].path.split(/[\\/]/).pop());
    },

    async viewDevelopmentStream(run, channel) {
      return this.openDevelopmentPreview(`/projects/${this.selectedProjectId}/development/runs/${run.id}/stream/${channel}`, `${channel} · 实时输出（非验收证据）`);
    },

    async openDevelopmentPreview(url, name) {
      try {
        const result = await fetch(url);
        if (!result.ok) throw new Error(this.requestError((await result.json()).detail || `HTTP ${result.status}`));
        this.developmentArtifact = { open: true, name, text: await result.text() };
      } catch (error) { this.showToast(error.message, 'error'); }
    },

    developmentLink(url) {
      return /^https:\/\//.test(url || '') ? url : null;
    },
  };
}
