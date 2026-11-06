// Frame-by-frame project replay.

export const replayMixins = {
    replayProgressLabel() {
      if (!this.replay.active || this.replay.frames.length === 0) return '回放';
      return `回放 ${Math.min(this.replay.frameIndex + 1, this.replay.frames.length)} / ${this.replay.frames.length}`;
    },

    stopReplayTimer() {
      if (!this.replay.timer) return;
      clearTimeout(this.replay.timer);
      this.replay.timer = null;
    },

    updateReplaySpeed() {
      if (!this.replay.active || !this.replay.playing) return;
      this.stopReplayTimer();
      this.scheduleReplayTick();
    },

    replayEventWeight(event) {
      const type = event?.type;
      if (type === 'decide_started') return 1.6;
      if (type === 'step_declared') return 1.05;
      if (type === 'step_running') return 0.75;
      if (type === 'step_concluded' || type === 'project_completed') return 1.6;
      if (type === 'step_abandoned') return 0.9;
      if (type === 'project_created' || type === 'hint_added') return 0.8;
      return 1.0;
    },

    replayEventDurationMs(event) {
      const base = Number(this.replay.stepMs) || 1100;
      return Math.round(base * this.replayEventWeight(event));
    },

    replayTimelineElapsedDurationMs(events, index) {
      let elapsed = 0;
      for (let i = 0; i < index; i += 1) {
        elapsed += this.replayEventDurationMs(events[i]);
      }
      return elapsed;
    },

    scheduleReplayTick() {
      if (!this.replay.active || !this.replay.playing) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        return;
      }
      this.stopReplayTimer();
      const currentFrame = this.replay.frames[this.replay.frameIndex];
      const delay = this.replayEventDurationMs(currentFrame?.event);
      this.replay.timer = setTimeout(() => this.advanceProjectReplay(), delay);
    },

    buildInitialReplayProject(sourceProject) {
      const origin = sourceProject.facts.find(fact => fact.id === 'origin');
      return {
        project: {
          ...this.cloneData(sourceProject.project),
          status: 'active',
          decide: null,
        },
        facts: [origin].filter(Boolean).map(fact => this.cloneData(fact)),
        goals: (sourceProject.goals || []).map(goal => this.cloneData(goal)),
        steps: [],
        findings: [],
        hints: [],
        constraints: [],
        steers: [],
        scope: [],
      };
    },

    buildReplayFrames(sourceProject, baseEvents) {
      const sourceSteps = new Map(sourceProject.steps.map(step => [step.id, step]));
      const replayEvents = [];

      for (const event of baseEvents) {
        if (event.type !== 'step_declared' || !event.stepId) {
          replayEvents.push(this.cloneData(event));
          continue;
        }

        const sourceStep = sourceSteps.get(event.stepId);
        replayEvents.push({
          id: `decide-started-${event.stepId}`,
          type: 'decide_started',
          timestamp: sourceStep?.created_at || event.timestamp,
          actor: sourceStep?.creator || event.actor || 'decideer',
          title: sourceStep?.description || event.title,
          meta: [
            sourceStep?.id || event.stepId,
            `from ${(sourceStep?.from || event.sourceFactIds || []).join(', ')}`,
          ],
          targetType: 'decide',
          targetId: sourceStep?.id || event.stepId,
          order: `${event.order}.decide`,
          stepId: sourceStep?.id || event.stepId,
          producedFactId: null,
          sourceFactIds: [...(sourceStep?.from || event.sourceFactIds || [])],
        });
        replayEvents.push(this.cloneData(event));
        if (!sourceStep?.worker) continue;
        replayEvents.push({
          id: `step-running-${sourceStep.id}`,
          type: 'step_running',
          timestamp: sourceStep.last_heartbeat_at || sourceStep.created_at,
          actor: sourceStep.worker,
          title: sourceStep.description,
          meta: [sourceStep.id, `worker ${sourceStep.worker}`],
          targetType: 'step',
          targetId: sourceStep.id,
          order: `${event.order}.run`,
          stepId: sourceStep.id,
          producedFactId: null,
          sourceFactIds: [...sourceStep.from],
        });
      }

      const replayProject = this.buildInitialReplayProject(sourceProject);
      const frames = [];
      for (const event of replayEvents) {
        this.applyReplayEvent(replayProject, sourceProject, event);
        frames.push({
          event: this.cloneData(event),
          project: this.cloneData(replayProject),
        });
      }

      if (frames.length > 0) {
        frames[frames.length - 1].project.project.status = sourceProject.project.status;
        frames[frames.length - 1].project.project.decide = this.cloneData(sourceProject.project.decide);
      }
      return frames;
    },

    applyReplayEvent(replayProject, sourceProject, event) {
      if (!replayProject || !sourceProject || !event) return;
      const sourceStep = event.stepId
        ? sourceProject.steps.find(step => step.id === event.stepId) || null
        : null;

      if (event.type === 'project_created') {
        replayProject.project.title = sourceProject.project.title;
        const rootGoal = (replayProject.goals || []).find(goal => !goal.parent);
        if (rootGoal) rootGoal.status = 'active';
        replayProject.project.status = 'active';
        return;
      }

      if (event.type === 'hint_added') {
        const hint = sourceProject.hints.find(item => item.id === event.targetId);
        if (hint && !replayProject.hints.some(item => item.id === hint.id)) {
          replayProject.hints.push(this.cloneData(hint));
        }
        return;
      }

      if (event.type === 'decide_started') {
        replayProject.project.decide = {
          worker: event.actor || 'decideer',
          trigger: 'new_facts',
          started_at: event.timestamp,
          last_heartbeat_at: event.timestamp,
        };
        return;
      }

      if (event.type === 'step_declared') {
        if (!sourceStep) return;
        replayProject.project.decide = null;
        if (!replayProject.steps.some(step => step.id === sourceStep.id)) {
          replayProject.steps.push({
            ...this.cloneData(sourceStep),
            to: null,
            worker: null,
            last_heartbeat_at: null,
            status: 'pending',
            concluded_at: null,
            note: null,
          });
        }
        return;
      }

      if (event.type === 'step_running') {
        if (!sourceStep) return;
        const replayStep = replayProject.steps.find(step => step.id === sourceStep.id);
        if (!replayStep) return;
        replayStep.worker = sourceStep.worker || sourceStep.creator;
        replayStep.last_heartbeat_at = sourceStep.last_heartbeat_at || sourceStep.created_at;
        return;
      }

      if (event.type === 'step_abandoned') {
        const abandoned = replayProject.steps.find(step => step.id === sourceStep?.id);
        if (abandoned) {
          abandoned.status = 'abandoned';
          abandoned.worker = sourceStep.worker || sourceStep.creator;
          abandoned.concluded_at = sourceStep.concluded_at;
          abandoned.note = sourceStep.note;
        }
        return;
      }

      if (event.type !== 'step_concluded' && event.type !== 'project_completed') return;
      if (!sourceStep) return;

      const replayStep = replayProject.steps.find(step => step.id === sourceStep.id);
      if (replayStep) {
        replayStep.to = sourceStep.to;
        replayStep.status = sourceStep.status;
        replayStep.worker = sourceStep.worker || sourceStep.creator;
        replayStep.last_heartbeat_at = sourceStep.last_heartbeat_at;
        replayStep.concluded_at = sourceStep.concluded_at;
      }

      if (event.type === 'project_completed') {
        replayProject.project.status = 'completed';
        const rootGoal = (replayProject.goals || []).find(goal => !goal.parent);
        if (rootGoal) {
          rootGoal.status = 'achieved';
          rootGoal.closed_at = sourceStep.concluded_at;
        }
        return;
      }

      const producedFact = sourceProject.facts.find(fact => fact.id === sourceStep.to);
      if (producedFact && !replayProject.facts.some(fact => fact.id === producedFact.id)) {
        replayProject.facts.push(this.cloneData(producedFact));
      }
      for (const finding of (sourceProject.findings || []).filter(item => item.step === sourceStep.id)) {
        if (!(replayProject.findings || []).some(item => item.id === finding.id)) {
          (replayProject.findings || (replayProject.findings = [])).push(this.cloneData(finding));
        }
      }
    },

    applyReplayFrame(frameIndex, options = {}) {
      if (!this.replay.active) return;
      const { reinitialize = false } = options;
      const frame = this.replay.frames[frameIndex];
      if (!frame) return;

      this.replay.frameIndex = frameIndex;
      this.project = this.cloneData(frame.project);
      this.replay.visibleEvents = this.replay.frames
        .slice(0, frameIndex + 1)
        .map(item => this.cloneData(item.event))
        .filter(entry => entry.type !== 'decide_started')
        .map((entry, index, list) => ({ ...entry, isLast: index === list.length - 1 }));
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];

      if (reinitialize) {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.$nextTick(() => {
          this.initGraph();
          this.followReplayTimelineTail();
        });
        return;
      }

      this.updateGraph();
      this.followReplayTimelineTail();
    },

    async startProjectReplay() {
      if (!this.project || this.replay.active || !this.selectedProjectId) return;
      try {
        const sourceProject = await this.api('GET', `/projects/${this.selectedProjectId}`);
        const baseEvents = this.timelineEvents().map(event => ({
          ...event,
          meta: [...(event.meta || [])],
          sourceFactIds: [...(event.sourceFactIds || [])],
        }));
        const frames = this.buildReplayFrames(sourceProject, baseEvents);
        if (frames.length === 0) {
          this.showToast('没有可回放的时间线', 'error');
          return;
        }

        const stepMs = String(this.replay.stepMs || '1100');
        this.stopReplayTimer();
        this.steerForm = { text: '' };
        this.scopeForm = { host: '' };
        this.constraintForm = { text: '', kind: 'deny' };
        this.showConstraintModal = false;
        this.replay = {
          active: true,
          playing: true,
          stepMs,
          frameIndex: -1,
          frames,
          visibleEvents: [],
          sourceProject,
          timer: null,
        };
        this.polling = false;
        this.sideTab = 'log';
        this.selectedNode = null;
        this.selectedFacts = [];
        this.selectedTimelineEntryId = null;
        this.applyReplayFrame(0, { reinitialize: true });
        this.scheduleReplayTick();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    advanceProjectReplay() {
      if (!this.replay.active) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      this.applyReplayFrame(this.replay.frameIndex + 1);
      this.scheduleReplayTick();
    },

    toggleProjectReplayPlayback() {
      if (!this.replay.active) return;
      if (this.replay.playing) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.restartProjectReplay();
        return;
      }
      this.replay.playing = true;
      this.scheduleReplayTick();
    },

    restartProjectReplay() {
      if (!this.replay.active || this.replay.frames.length === 0) return;
      this.stopReplayTimer();
      this.replay.playing = true;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.applyReplayFrame(0, { reinitialize: true });
      this.scheduleReplayTick();
    },

    async exitProjectReplay() {
      if (!this.replay.active) return;
      const projectId = this.selectedProjectId;
      const stepMs = String(this.replay.stepMs || '1100');
      this.stopReplayTimer();
      this.steerForm = { text: '' };
      this.scopeForm = { host: '' };
      this.constraintForm = { text: '', kind: 'deny' };
      this.showConstraintModal = false;
      this.replay = {
        active: false,
        playing: false,
        stepMs,
        frameIndex: -1,
        frames: [],
        visibleEvents: [],
        sourceProject: null,
        timer: null,
      };
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];
      this.polling = true;
      if (!projectId) return;
      await this.loadProject(projectId);
      this.$nextTick(() => {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.initGraph();
      });
    },
};
