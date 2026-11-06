// Lifecycle, routing, HTTP and local preferences.

export const coreMixins = {
    async init() {
      this._panelResizeMove = (e) => this.onPanelResize(e);
      this._panelResizeStop = () => this.stopPanelResize();
      window.addEventListener('pointermove', this._panelResizeMove);
      window.addEventListener('pointerup', this._panelResizeStop);
      this.loadLocalPrefs();
      await this.loadProjects();
      await this.loadSettings();
      this.startPolling();
      window.addEventListener('hashchange', () => this.handleRoute());
      this.handleRoute();
    },

    handleRoute() {
      const hash = location.hash || '#/';
      const m = hash.match(/^#\/projects\/(.+)$/);
      if (m) {
        const id = m[1];
        if (this.selectedProjectId !== id || this.view !== 'graph') {
          this.openProject(id);
        }
      } else {
        if (this.view !== 'list') this.backToList(true);
      }
    },

    async api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      if (r.status === 204) return null;
      const data = await r.json();
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        if (typeof data.detail === 'string') msg = data.detail;
        else if (Array.isArray(data.detail)) msg = data.detail.map(e => e.msg).join('; ');
        throw new Error(msg);
      }
      return data;
    },

    async fetchText(path) {
      const r = await fetch(path);
      const text = await r.text();
      if (!r.ok) {
        let detail = text;
        try {
          const data = JSON.parse(text);
          detail = data.detail || text;
        } catch {}
        throw new Error(detail || `HTTP ${r.status}`);
      }
      return text;
    },

    showToast(msg, type = 'info') {
      this.toast = { show: true, message: msg, type };
      setTimeout(() => this.toast.show = false, 3000);
    },

    cloneData(value) {
      try {
        return JSON.parse(JSON.stringify(value));
      } catch (error) {
        if (typeof structuredClone === 'function') return structuredClone(value);
        throw error;
      }
    },

    isValidLayoutMode(mode) {
      return ['dagre_tb', 'dagre_lr', 'klay_tb', 'klay_lr', 'elk_tb', 'elk_lr'].includes(mode);
    },

    layoutEngine(mode = this.layoutMode) {
      if (mode.startsWith('elk')) return 'elk';
      return mode.startsWith('klay') ? 'klay' : 'dagre';
    },

    layoutDirection(mode = this.layoutMode) {
      return mode.endsWith('_lr') ? 'LR' : 'TB';
    },

    loadLocalPrefs() {
      try {
        const raw = localStorage.getItem('cairn.localPrefs');
        if (!raw) {
          this.localPrefs.actor_name = 'Human';
        } else {
          const parsed = JSON.parse(raw);
          if (typeof parsed.actor_name === 'string') this.localPrefs.actor_name = parsed.actor_name;
          if (this.isValidLayoutMode(parsed.layout_mode)) {
            this.localPrefs.layout_mode = parsed.layout_mode;
          } else if (parsed.layout_dir === 'TB' || parsed.layout_dir === 'LR') {
            this.localPrefs.layout_mode = parsed.layout_dir === 'LR' ? 'dagre_lr' : 'dagre_tb';
          }
        }
        const rawPanelWidth = localStorage.getItem('cairn.sidePanelWidth');
        if (rawPanelWidth !== null) {
          const savedPanelWidth = Number(rawPanelWidth);
          if (Number.isFinite(savedPanelWidth)) this.sidePanelWidth = savedPanelWidth;
        }
      } catch (e) {
        console.error(e);
      }
      if (!this.localPrefs.actor_name.trim()) this.localPrefs.actor_name = 'Human';
      if (!this.isValidLayoutMode(this.localPrefs.layout_mode)) this.localPrefs.layout_mode = 'dagre_tb';
      this.layoutMode = this.localPrefs.layout_mode;
    },

    saveLocalPrefs() {
      try {
        localStorage.setItem('cairn.localPrefs', JSON.stringify(this.localPrefs));
      } catch (e) {
        console.error(e);
      }
    },

    saveSidePanelWidth() {
      try {
        localStorage.setItem('cairn.sidePanelWidth', String(this.sidePanelWidth));
      } catch (e) {
        console.error(e);
      }
    },

    async reopenProject() {
      const projectId = this.reopenForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${projectId}/reopen`, {
          description: this.reopenForm.description,
          creator: actor,
        });
        this.showReopenModal = false;
        this.reopenForm = { projectId: '', projectTitle: '', description: '' };
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
        this.showToast('项目已重新开启');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async openProject(id) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.rememberProjectListScroll();
      this.selectedProjectId = id;
      this.resetSessionState();
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.steerForm = { text: '' };
      this.scopeForm = { host: '' };
      this.constraintForm = { text: '', kind: 'deny' };
      this.showConstraintModal = false;
      await this.loadProject(id);
      this.view = 'graph';
      if (location.hash !== `#/projects/${id}`) location.hash = `/projects/${id}`;
      this.$nextTick(() => {
        const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
        if (clampedWidth !== this.sidePanelWidth) {
          this.sidePanelWidth = clampedWidth;
          this.saveSidePanelWidth();
        }
        this.teardownAutoFit();
        if (this.cy) { this.cy.destroy(); this.cy = null; }
        this.initGraph();
      });
    },

    backToList(fromRoute) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.view = 'list';
      this.shouldRestoreProjectListScroll = true;
      this.project = null;
      this.usage = null;
      this.findingsSummary = null;
      this.contextSummary = null;
      this._extrasProjectId = null;
      this.selectedProjectId = '';
      this.resetSessionState();
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.teardownAutoFit();
      if (this.cy) { this.cy.destroy(); this.cy = null; }
      if (!fromRoute) location.hash = '/';
      this.loadProjects().then(() => this.restoreProjectListScroll());
    },

    startPolling() {
      const schedule = () => {
        this.pollTimer = setTimeout(tick, this.pollDelay);
      };
      const tick = async () => {
        let changed = true;
        if (this.polling) {
          if (this.selectedProjectId && this.view === 'graph') {
            // A 304-backed poll returns the same snapshot; only rebuild the (expensive)
            // Cytoscape graph when the payload actually changed.
            const before = JSON.stringify(this.project);
            await this.loadProject(this.selectedProjectId);
            changed = JSON.stringify(this.project) !== before;
            if (changed) this.updateGraph();
          } else {
            const before = JSON.stringify(this.projects);
            await this.loadProjects();
            changed = JSON.stringify(this.projects) !== before;
          }
        }
        // Quiet projects back off toward a 30s cadence; the first change snaps back.
        this.pollDelay = changed ? 5000 : Math.min(this.pollDelay * 2, 30000);
        schedule();
      };
      this.pollDelay = 5000;
      schedule();
    },
};
