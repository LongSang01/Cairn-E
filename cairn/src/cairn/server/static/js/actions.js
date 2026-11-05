// Form submissions that mutate the graph.

export const actionsMixins = {
    formatDurationMs(ms) {
      const totalSeconds = Math.max(0, Math.round(ms / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours > 0) return `${hours}h ${minutes}m`;
      if (minutes > 0) return `${minutes}m ${seconds}s`;
      return `${seconds}s`;
    },

    async createProject() {
      try {
        const actor = this.actorName();
        const hintContents = this.newProject.hints.filter(h => h.content?.trim());
        const body = {
          title: this.newProject.title,
          origin: this.newProject.origin,
          goal: this.newProject.goal
        };
        const hints = hintContents.map(h => ({ content: h.content.trim(), creator: actor }));
        if (hints.length > 0) body.hints = hints;
        const data = await this.api('POST', '/projects', body);
        this.showNewProject = false;
        this.newProject = { title:'', origin:'', goal:'', hints: [{ content:'' }] };
        await this.loadProjects();
        await this.openProject(data.project.id);
        this.showToast('项目已创建');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCreateStep() {
      if (!this.canActOnSelectedFacts()) return;
      this.stepForm = { description:'' };
      this.showStepModal = true;
    },

    async createStep(claim = false) {
      try {
        const actor = this.actorName();
        const rootGoal = (this.project?.goals || []).find(goal => !goal.parent);
        await this.api('POST', `/projects/${this.selectedProjectId}/steps`, {
          from: this.selectedFacts,
          description: this.stepForm.description,
          goal: rootGoal ? rootGoal.id : null,
          creator: actor,
          worker: claim ? actor : null,
        });
        this.showStepModal = false;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claim ? '步骤已声明并认领' : '步骤已声明');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async sendHeartbeatForSelectedStep() {
      const step = this.selectedActionableOpenStepRecord();
      if (!step) return;
      try {
        const actor = this.actorName();
        const claiming = !step.worker;
        await this.api('POST', `/projects/${this.selectedProjectId}/steps/${step.id}/heartbeat`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claiming ? '步骤已认领' : '心跳已发送');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async releaseSelectedStep() {
      const step = this.selectedReleasableOpenStepRecord();
      if (!step) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/steps/${step.id}/release`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('步骤已释放');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openConclude(step) { this.concludeForm = { description:'', stepId: step.id }; this.showConcludeModal = true; },

    async concludeStep() {
      try {
        const actor = this.actorName();
        const description = (this.concludeForm.description || '').trim();
        await this.api('POST', `/projects/${this.selectedProjectId}/steps/${this.concludeForm.stepId}/conclude`, {
          worker: actor,
          status: description ? 'done' : 'abandoned',
          facts: description ? [{ description }] : [],
        });
        this.showConcludeModal = false;
        this.selectedNode = null;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('步骤已结束');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCompleteProject() {
      if (!this.canActOnSelectedFacts()) return;
      this.completeForm = { description:'' };
      this.showCompleteModal = true;
    },

    openHintModal() {
      if (!this.projectCanWriteHints()) return;
      this.hintForm = { content:'' };
      this.showHintModal = true;
    },

    async completeProject() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/complete`, { from: this.selectedFacts, description: this.completeForm.description, worker: actor });
        this.showCompleteModal = false;
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('项目已完成');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async addHint() {
      if (!this.projectCanWriteHints()) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.hintForm.content, creator: actor });
        this.showHintModal = false;
        this.hintForm = { content:'' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'hints';
        this.showToast('提示已添加');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    saveLocalSettings() {
      try {
        this.localPrefs.actor_name = this.actorName();
        this.localPrefs.layout_mode = this.isValidLayoutMode(this.localPrefs.layout_mode) ? this.localPrefs.layout_mode : 'dagre_tb';
        this.layoutMode = this.localPrefs.layout_mode;
        this.saveLocalPrefs();
        if (this.cy) this.cy.layout(this.layoutOpts()).run();
        this.showLocalPrefs = false;
        this.showToast('本地偏好已保存');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async saveServerSettings() {
      try {
        await this.api('PUT', '/settings', this.settingsForm);
        this.showSettings = false;
        this.showToast('服务端设置已保存');
      } catch(e) { this.showToast(e.message, 'error'); }
    },
};
