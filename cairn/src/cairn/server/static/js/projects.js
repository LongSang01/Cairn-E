// Project list, detail, export, delete and status changes.

export const projectsMixins = {
  async loadProjects() {
    try {
      this.projects = await this.api('GET', '/projects');
    } catch (e) {
      console.error(e);
    }
  },

  rememberProjectListScroll() {
    const scroller = this.$refs.projectListScroll;
    if (!scroller) return;
    this.projectListScrollTop = scroller.scrollTop;
  },

  restoreProjectListScroll() {
    if (!this.shouldRestoreProjectListScroll) return;
    this.$nextTick(() => {
      const scroller = this.$refs.projectListScroll;
      if (!scroller) return;
      const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
      scroller.scrollTop = Math.min(this.projectListScrollTop, maxScrollTop);
      this.shouldRestoreProjectListScroll = false;
    });
  },

  projectLabel(projectId, projectTitle) {
    return projectTitle ? `${projectId} - ${projectTitle}` : projectId;
  },

  deleteConfirmLabel() {
    return this.projectLabel(this.deleteConfirm.id, this.deleteConfirm.title);
  },

  requestDeleteProject(projectId, projectTitle = '') {
    if (!projectId) return;
    this.deleteConfirm = { id: projectId, title: projectTitle };
    this.showDeleteModal = true;
  },

  closeDeleteModal(force = false) {
    if (this.isDeletingProject && !force) return;
    this.showDeleteModal = false;
    this.deleteConfirm = { id: '', title: '' };
  },

  async viewProjectYaml(projectId, projectTitle = '') {
    if (!projectId) return;
    this.exportProjectId = projectId;
    this.yamlPreviewTitle = this.projectLabel(projectId, projectTitle);
    this.exportTab = 'yaml';
    await this.loadExportTab('yaml');
    this.showYamlModal = true;
  },

  async viewFindingsReport() {
    if (!this.project) return;
    this.exportProjectId = this.project.project.id;
    this.yamlPreviewTitle = this.projectLabel(this.project.project.id, this.project.project.title);
    this.exportTab = 'findings';
    await this.loadExportTab('findings');
    this.showYamlModal = true;
  },

  async switchExportTab(tab) {
    if (tab === this.exportTab) return;
    this.exportTab = tab;
    await this.loadExportTab(tab);
  },

  async loadExportTab(tab) {
    if (!this.exportProjectId) return;
    try {
      const text = await this.fetchText(`/projects/${this.exportProjectId}/export?format=${tab}`);
      this.yamlPreviewText = text;
      this.yamlPreviewHtml =
        tab === 'yaml'
          ? this.highlightYaml(text)
          : tab === 'findings' || tab === 'report'
            ? this.highlightReport(text)
            : this.highlightTimeline(text);
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },

  deleteProject() {
    if (!this.selectedProjectId) return;
    this.requestDeleteProject(this.selectedProjectId, this.project?.project?.title || '');
  },

  deleteProjectById(projectId, projectTitle = '') {
    this.requestDeleteProject(projectId, projectTitle);
  },

  async confirmDeleteProject() {
    const projectId = this.deleteConfirm.id;
    if (!projectId || this.isDeletingProject) return;
    try {
      this.isDeletingProject = true;
      await this.api('DELETE', `/projects/${projectId}`);
      this.closeDeleteModal(true);
      if (this.selectedProjectId === projectId && this.view === 'graph') {
        this.backToList();
      } else {
        await this.loadProjects();
      }
      this.showToast(`已删除 ${projectId}`);
    } catch (e) {
      this.showToast(e.message, 'error');
    } finally {
      this.isDeletingProject = false;
      if (!this.showDeleteModal) this.deleteConfirm = { id: '', title: '' };
    }
  },

  async loadSettings() {
    try {
      const s = await this.api('GET', '/settings');
      this.settingsForm.step_timeout = s.step_timeout;
      this.settingsForm.decide_timeout = s.decide_timeout;
    } catch (e) {
      console.error(e);
    }
  },

  async loadProject(id) {
    try {
      const project = await this.api('GET', `/projects/${id}`);
      const switching = this.project && this.project.project.id !== project.project.id;
      // A poll refresh must not clobber the live transcript written by the streams,
      // so re-open the streams after swapping in the polled snapshot.
      if (switching) this.resetSessionState();
      else this.stopAllSessionStreams();
      this.project = project;
      await this.loadProjectExtras(id);
      if (!switching)
        this.openSessionIds.forEach((sessionId) => this.startSessionStream(sessionId));
      if (this.questionsProjectId !== project.project.id) {
        this.resetQuestionState();
        this.questionsProjectId = project.project.id;
        this.loadQuestions();
      }
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },

  async loadProjectExtras(id) {
    // Switching projects must not briefly show the previous project's numbers, but a
    // single failed poll should keep the last good values instead of blanking them.
    if (this._extrasProjectId !== id) {
      this._extrasProjectId = id;
      this.findingsSummary = null;
      this.usage = null;
      this.contextSummary = null;
    }
    const [findingsSummary, usage, contextSummary, approvals] = await Promise.all([
      this.api('GET', `/projects/${id}/findings/summary`).catch(() => null),
      this.api('GET', `/projects/${id}/usage`).catch(() => null),
      this.api('GET', `/projects/${id}/context`).catch(() => null),
      // Pending `ask` rules block the worker until someone decides, so the count
      // has to be visible on every poll, not only when the tab is open.
      this.api('GET', `/projects/${id}/tool-approvals`).catch(() => null),
    ]);
    if (this._extrasProjectId !== id) return;
    if (findingsSummary) this.findingsSummary = findingsSummary;
    if (usage) this.usage = usage;
    if (contextSummary) this.contextSummary = contextSummary;
    if (approvals) {
      this.toolApprovals = approvals.approvals || [];
      this.approvalsProjectId = id;
    }
  },

  // --- Command guard: human approval queue -------------------------------

  async loadToolApprovals() {
    const projectId = this.selectedProjectId;
    if (!projectId) return;
    try {
      const data = await this.api('GET', `/projects/${projectId}/tool-approvals`);
      if (this.selectedProjectId !== projectId) return;
      this.toolApprovals = data.approvals || [];
      this.approvalsProjectId = projectId;
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },

  async decideToolApproval(approvalId, approve) {
    const projectId = this.selectedProjectId;
    if (!projectId || !approvalId || this.decidingApprovalId) return;
    this.decidingApprovalId = approvalId;
    try {
      await this.api('POST', `/projects/${projectId}/tool-approvals/${approvalId}/decide`, {
        approve,
        actor: this.actorName(),
      });
      this.showToast(approve ? '已批准该命令' : '已拒绝该命令');
      await this.loadToolApprovals();
    } catch (e) {
      this.showToast(e.message, 'error');
    } finally {
      this.decidingApprovalId = '';
    }
  },

  // --- Token budget -------------------------------------------------------

  openBudgetDialog() {
    const budget = this.project?.project?.token_budget;
    // The dialog speaks millions (0.5 = 500k tokens); the API keeps raw tokens.
    this.budgetForm = { value: this.budgetMillions(budget) };
    this.showBudgetModal = true;
  },

  async saveBudget(clear = false) {
    const projectId = this.selectedProjectId;
    if (!projectId || this.savingBudget) return;
    const raw = clear ? '' : String(this.budgetForm.value ?? '').trim();
    let tokenBudget = null;
    if (raw) {
      tokenBudget = this.budgetMillionsToTokens(raw);
      if (tokenBudget === null) {
        this.showToast('预算必须是大于 0 的数（单位 M，1 M = 100 万 token）', 'error');
        return;
      }
    }
    this.savingBudget = true;
    try {
      const updated = await this.api('PUT', `/projects/${projectId}/budget`, {
        token_budget: tokenBudget,
      });
      if (this.project && this.project.project.id === projectId) {
        this.project.project.status = updated.status;
        this.project.project.token_budget = updated.token_budget;
        this.project.project.blocked_reason = updated.blocked_reason;
      }
      const summary = this.projects.find((item) => item.id === projectId);
      if (summary) summary.status = updated.status;
      this.showBudgetModal = false;
      this.showToast(tokenBudget ? '预算已更新' : '预算已清除');
      await this.loadProjects();
    } catch (e) {
      this.showToast(e.message, 'error');
    } finally {
      this.savingBudget = false;
    }
  },

  // --- Sessions: one model conversation per dispatcher run -------------------

  async toggleProjectStop() {
    if (!this.selectedProjectId || !this.project || this.project.project.status === 'completed')
      return;
    const nextStatus = this.project.project.status === 'active' ? 'stopped' : 'active';
    try {
      await this.setProjectStatus(this.selectedProjectId, nextStatus);
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },

  async toggleProjectStopById(projectId, currentStatus) {
    if (!projectId || currentStatus === 'completed') return;
    const nextStatus = currentStatus === 'active' ? 'stopped' : 'active';
    try {
      await this.setProjectStatus(projectId, nextStatus);
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },

  async setProjectStatus(projectId, status, options = {}) {
    const { reload = true, toast = true } = options;
    const updated = await this.api('PUT', `/projects/${projectId}/status`, { status });
    const projectSummary = this.projects.find((item) => item.id === projectId);
    if (projectSummary) {
      projectSummary.status = updated.status;
      projectSummary.decide = updated.decide;
    }
    if (this.selectedProjectId === projectId && this.project) {
      this.project.project.status = updated.status;
      this.project.project.decide = updated.decide;
      if (!this.projectIsActive()) {
        this.showStepModal = false;
        this.showConcludeModal = false;
        this.showCompleteModal = false;
      }
      if (!this.projectCanWriteHints()) {
        this.showHintModal = false;
        this.showConstraintModal = false;
      }
    }
    if (reload) await this.loadProjects();
    if (toast) this.showToast(status === 'stopped' ? '项目已暂停' : '项目已继续');
    return updated;
  },

  async stopAllActiveProjects() {
    const activeProjects = this.projects.filter((project) => project.status === 'active');
    if (activeProjects.length === 0 || this.isStoppingAllProjects) return;
    this.isStoppingAllProjects = true;
    try {
      const results = await Promise.allSettled(
        activeProjects.map((project) =>
          this.setProjectStatus(project.id, 'stopped', { reload: false, toast: false }),
        ),
      );
      await this.loadProjects();
      const successCount = results.filter((result) => result.status === 'fulfilled').length;
      const failureCount = results.length - successCount;
      if (failureCount === 0) {
        this.showToast(`已暂停 ${successCount} 个进行中的项目`);
      } else {
        this.showToast(`已暂停 ${successCount} 个项目，${failureCount} 个失败`, 'error');
      }
    } catch (e) {
      this.showToast(e.message, 'error');
    } finally {
      this.isStoppingAllProjects = false;
    }
  },

  openReopenProject(projectId, projectTitle = '') {
    if (!projectId) return;
    this.reopenForm = { projectId, projectTitle, description: '' };
    this.showReopenModal = true;
  },

  openRenameProject(projectId, projectTitle = '') {
    if (!projectId) return;
    this.renameForm = { projectId, originalTitle: projectTitle, title: projectTitle };
    this.showRenameModal = true;
    this.$nextTick(() => {
      this.$refs.renameTitleInput?.focus();
      this.$refs.renameTitleInput?.select?.();
    });
  },

  async renameProject() {
    const projectId = this.renameForm.projectId || this.selectedProjectId;
    if (!projectId) return;
    try {
      const updated = await this.api('PUT', `/projects/${projectId}/title`, {
        title: this.renameForm.title,
      });
      const projectSummary = this.projects.find((item) => item.id === projectId);
      if (projectSummary) {
        projectSummary.title = updated.title;
      }
      if (this.selectedProjectId === projectId && this.project) {
        this.project.project.title = updated.title;
      }
      this.showRenameModal = false;
      this.renameForm = { projectId: '', originalTitle: '', title: '' };
      await this.loadProjects();
      this.showToast('项目已重命名');
    } catch (e) {
      this.showToast(e.message, 'error');
    }
  },
};
