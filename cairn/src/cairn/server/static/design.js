function designFeatures() {
  return {
    designDraft: { version: 0, stage: 'brief', documents: {}, decisions: [], questions: [], work: [], commands: [], candidate: {} },
    designEntries: [], designCursor: 0, designProject: '', designInput: '', designAnswers: {},
    designStream: null, designStreamProject: '', designSending: false, designMessageId: '', designCommandIndex: 0, designCommandOpen: true,
    designError: '', designDocument: '', designFollowPending: false, designMessageSignature: '',
    designReadiness: { ready: false, issues: ['正在检查提交条件'], version: -1 },
    designDocuments: {}, designDocumentErrors: [], designArtifactDirectory: '',
    designReview: null,

    designReviewStatus() {
      const review = this.designReview;
      if (!review) return '';
      if (!review.current) {
        if (review.revision?.state === 'ready') return '修订已排队，等待设计 Agent 领取';
        if (review.revision?.state === 'running') return '设计 Agent 正在根据评审意见修订';
        if (review.revision?.state === 'implemented') return '修订输出已返回，等待校验';
        if (review.revision?.state === 'waiting') return '修订暂停，请查看设计 Agent 的等待原因';
        if (review.revision?.state === 'verified') return '修订已完成，请核对新草案后重新送审';
        return '上一轮评审 · ' + (review.verdict && review.verdict !== 'pending' ? this.reviewLabel(review.verdict) : '未完成') + '（已归档）';
      }
      if (review.verdict !== 'pending') return this.reviewLabel(review.verdict);
      const run = review.run;
      if (run?.state === 'running') {
        if (Date.now() - Date.parse(run.heartbeat_at) > 60000) return '心跳待确认，请查看运行输出';
        return run.checkpoint || (run.pid ? '评审 Agent 正在运行' : '正在启动评审 Agent');
      }
      if (review.work_state === 'waiting') return '评审等待重试／外部条件';
      if (run?.state === 'success' && !run.applied) return '评审输出已返回，等待校验并应用';
      if (run && run.state !== 'success') return '上次运行未完成：' + this.developmentRunStatus(run);
      return '已排队，等待独立评审 Agent 领取';
    },

    canRecoverDesignRevision() {
      return !this.designDraft.candidate.digest && !!this.designReview?.summary && !this.designBusy()
        && !this.designReview.revision && !this.designDraft.session;
    },

    openDesignReviewReport() {
      const review = this.designReview;
      if (!review?.summary) return;
      this.developmentArtifact = { open: true, name: '独立评审报告 · v' + review.version, markdown: true,
        text: `# 独立评审报告\n\n冻结版本：v${review.version} · ${this.stageLabel(review.stage)}\n\n评审任务：${review.intent_id}\n\n结论：${this.reviewLabel(review.verdict)}\n\n${review.summary}` };
    },

    openDesignDocument(name) {
      const text = this.designDocuments[name];
      if (!text) { this.designError = '此文档尚无可读取的正文，请查看方案面板中的文件错误。'; return; }
      this.developmentArtifact = { open: true, name, text, markdown: true };
    },

    canSubmitDesign() {
      return !this.designSending && !this.designBusy() && this.designReadiness.ready
        && this.designReadiness.version === this.designDraft.version;
    },

    visibleDesignEntries() {
      return this.designEntries.filter(entry => String(entry.text || '').replace(/[\s\u200B-\u200D\uFEFF]/g, '').length);
    },

    nextDesignStage() {
      const stages = ['brief', 'requirements', 'ux', 'architecture', 'plan'];
      return stages[stages.indexOf(this.designDraft.stage) + 1] || 'plan';
    },

    designBusy() {
      return (this.project?.intents || []).some(i => i.work?.purpose === 'design' && ['ready', 'running', 'implemented'].includes(i.work.state));
    },

    designReceive(item) {
      if (item.id <= this.designCursor) return;
      const history = this.$refs?.designHistory;
      const follow = history && history.scrollHeight - history.scrollTop - history.clientHeight < 100;
      this.designCursor = item.id;
      const data = item.data;
      if (item.kind === 'user') this.designEntries.push({ id: item.id, role: '你', text: data.content });
      if (item.kind === 'text') {
        const previous = this.designEntries.at(-1);
        const parts = String(data.block || '').split(':');
        if (parts.length === 2 && previous?.role === 'Claude' && previous.block?.split(':').length === 3
            && previous.block.split(':')[0] === parts[0] && previous.text === data.text) return;
        const existing = this.designEntries.find(e => e.block === data.block);
        if (existing) existing.text = data.mode === 'snapshot' ? data.text : existing.text + data.text;
        else this.designEntries.push({ id: item.id, block: data.block, role: 'Claude', text: data.text });
      }
      if (item.kind === 'activity') this.designEntries.push({ id: item.id, role: '工作流', text: [data.tool, data.skill].filter(Boolean).join(' · ') });
      if (follow && !this.designFollowPending) {
        this.designFollowPending = true;
        this.$nextTick(() => { history.scrollTop = history.scrollHeight; this.designFollowPending = false; });
      }
    },

    async loadDesign(id) {
      if (this.selectedProjectId !== id) return;
      if (this.designProject !== id) {
        this.designStream?.abort(); this.designStream = null; this.designStreamProject = '';
        this.designProject = id; this.designEntries = []; this.designCursor = 0; this.designInput = '';
        this.designAnswers = {}; this.designMessageId = ''; this.designDocument = '';
        this.designReadiness = { ready: false, issues: ['正在检查提交条件'], version: -1 };
        this.designDocuments = {}; this.designDocumentErrors = [];
        this.designArtifactDirectory = '';
        this.designReview = null;
      }
      try {
        const state = await this.api('GET', `/projects/${id}/development/design?after=${this.designCursor}`);
        if (this.selectedProjectId !== id) return;
        this.designDraft = state.draft;
        this.designDocuments = state.document_views || {};
        this.designDocumentErrors = state.document_errors || [];
        this.designArtifactDirectory = state.artifact_directory || '';
        this.designReview = state.review || null;
        this.designReadiness = state.readiness || { ready: false, issues: ['请重启 Cairn 以加载提交校验'], version: -1 };
        for (const item of state.messages) this.designReceive(item);
        this.designError = '';
        if (!this.designStream || this.designStreamProject !== id) this.startDesignStream(id);
      } catch (e) { if (this.selectedProjectId === id) this.designError = e.message; }
    },

    async startDesignStream(id) {
      this.designStream?.abort();
      const controller = new AbortController();
      this.designStream = controller; this.designStreamProject = id;
      try {
        const response = await fetch(`/projects/${id}/development/design/events?after=${this.designCursor}`, {
          signal: controller.signal
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        while (this.selectedProjectId === id && this.view === 'graph') {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let end;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
            const data = frame.split('\n').find(line => line.startsWith('data: '));
            if (data && this.selectedProjectId === id) this.designReceive(JSON.parse(data.slice(6)));
          }
        }
      } catch (e) { if (e.name !== 'AbortError' && this.selectedProjectId === id) this.designError = '连接中断，正在通过持久记录恢复'; }
      finally { controller.abort(); if (this.designStream === controller) { this.designStream = null; this.designStreamProject = ''; } }
    },

    designCommands() {
      if (!this.designCommandOpen || !this.designInput.startsWith('/') || /\s/.test(this.designInput)) return [];
      const prefix = this.designInput.slice(1).toLowerCase();
      return (this.designDraft.commands || []).map(c => typeof c === 'string' ? { name: c } : c)
        .filter(c => c.name && !['theme', 'terminal-setup', 'login', 'logout'].includes(c.name) && c.name.toLowerCase().startsWith(prefix)).slice(0, 10);
    },

    designMarkdown(text) {
      return DOMPurify.sanitize(marked.parse(String(text || ''), { gfm: true, breaks: true }), {
        USE_PROFILES: { html: true }, ALLOW_DATA_ATTR: false,
        FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'img'], FORBID_ATTR: ['style'],
      });
    },

    designKey(event) {
      if (event.isComposing) return;
      const commands = this.designCommands();
      if (event.key === 'Escape') { this.designCommandOpen = false; return; }
      if (commands.length && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); this.designCommandIndex = (this.designCommandIndex + (event.key === 'ArrowDown' ? 1 : -1) + commands.length) % commands.length;
      } else if (commands.length && ['Enter', 'Tab'].includes(event.key)) {
        event.preventDefault(); this.designInput = '/' + commands[this.designCommandIndex % commands.length].name + ' ';
      } else if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.sendDesign(); }
    },

    async sendDesign(question = null) {
      const answer = question ? (this.designAnswers[question.id] || '').trim() : '';
      const text = question ? answer : this.designInput.trim();
      if (!text || this.designSending || this.designBusy()) return;
      this.designSending = true;
      const projectId = this.selectedProjectId;
      const signature = JSON.stringify([text, question?.id || '']);
      if (signature !== this.designMessageSignature) { this.designMessageId = ''; this.designMessageSignature = signature; }
      this.designMessageId ||= crypto.randomUUID();
      try {
        await this.api('POST', `/projects/${projectId}/development/design/messages`, {
          id: this.designMessageId, content: question ? `${question.id}：${answer}` : text, answers: question ? { [question.id]: answer } : {}
        });
        if (this.selectedProjectId !== projectId) return;
        this.designMessageId = '';
        if (!question) this.designInput = '';
        if (question) delete this.designAnswers[question.id];
        await this.loadProject(this.selectedProjectId);
        if (question && this.selectedProjectId === projectId) this.$nextTick(() => this.$refs.questionAnswer?.focus());
      } catch (e) { if (this.selectedProjectId === projectId) this.designError = e.message; }
      finally { this.designSending = false; }
    },

    async designAction(action) {
      if (this.designSending || action === 'candidate' && !this.canSubmitDesign()) return;
      this.designSending = true;
      const projectId = this.selectedProjectId;
      try {
        await this.api('POST', `/projects/${projectId}/development/design/${action}`, {
          version: this.designDraft.version, digest: this.designDraft.candidate.digest || (action === 'reopen' ? this.designReview?.digest : '') || ''
        });
        if (this.selectedProjectId !== projectId) return;
        await this.loadProject(this.selectedProjectId);
        if (action === 'publish') { this.setStage('development'); this.showToast('已确认此版本，原生任务已交给 Cairn 执行'); }
        if (action === 'reopen') this.showToast('修订已排队，设计 Agent 将接收评审意见并保存新草案');
      } catch (e) { if (this.selectedProjectId === projectId) this.designError = e.message; }
      finally { this.designSending = false; }
    },

    async interruptDesign() {
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/development/design/interrupt`, {});
        await this.loadProject(this.selectedProjectId);
      } catch (e) { this.designError = e.message; }
    },

    openAgentWindow(run) {
      window.open(`/static/agent.html?project=${encodeURIComponent(this.selectedProjectId)}&run=${encodeURIComponent(run.id)}`, '_blank');
    },
  };
}
