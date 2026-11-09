// Node selection and lineage highlighting.

export const selectionMixins = {
  clearGraphSelection(preserveTimeline = false) {
    this.selectedNode = null;
    this.selectedFacts = [];
    if (!preserveTimeline) this.selectedTimelineEntryId = null;
    if (this.cy) this.cy.elements().removeClass('highlight focus faded selected-fact');
  },

  clearSelection() {
    this.clearGraphSelection(false);
  },

  toggleFactSelection(fid) {
    this.selectedTimelineEntryId = null;
    const idx = this.selectedFacts.indexOf(fid);
    if (idx >= 0) {
      this.selectedFacts.splice(idx, 1);
      if (this.selectedFacts.length === 0) {
        this.clearSelection();
        return;
      }
      if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
        this.selectedNode = { type: 'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
      }
    } else {
      this.selectedFacts.push(fid);
      this.selectedNode = { type: 'fact', id: fid };
    }
    this.refreshGraphDecorations();
  },

  removeFactSelection(fid) {
    this.selectedTimelineEntryId = null;
    const idx = this.selectedFacts.indexOf(fid);
    if (idx < 0) return;
    this.selectedFacts.splice(idx, 1);
    if (this.selectedFacts.length === 0) {
      this.clearSelection();
      return;
    }
    if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
      this.selectedNode = { type: 'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
    }
    this.refreshGraphDecorations();
  },

  onNodeTap(e) {
    const node = e.target;
    const id = node.data('id');
    const nt = node.data('nodeType');
    const keepLogFocus = this.sideTab === 'log';

    if (['in_progress', 'unclaimed', 'abandoned'].includes(nt)) {
      this.selectStep(node.data('stepId'));
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
      return;
    }
    if (nt === 'goal' || nt === 'subgoal') {
      this.selectGoal(node.data('goalId'));
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
      return;
    }
    if (nt === 'finding') {
      this.selectFinding(node.data('findingId'));
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
      return;
    }
    if (nt === 'digest') {
      this.selectDigest(node.data('digestKind'));
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
      return;
    }
    if (e.originalEvent.shiftKey) {
      this.toggleFactSelection(id);
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
      return;
    } else {
      this.selectedFacts = [id];
      this.selectFact(id);
    }
    if (keepLogFocus) this.scrollTimelineToSelection();
    else this.sideTab = 'detail';
  },

  onEdgeTap(e) {
    this.selectStep(e.target.data('stepId'));
    if (this.sideTab === 'log') this.scrollTimelineToSelection();
    else this.sideTab = 'detail';
  },

  selectStep(step) {
    const stepId = typeof step === 'string' ? step : step?.id;
    if (!stepId) return;
    this.selectedFacts = [];
    this.selectedTimelineEntryId = null;
    this.selectedNode = { type: 'step', id: stepId };
    this.applyLineageHighlightForStep(stepId);
  },

  selectFact(fact) {
    const factId = typeof fact === 'string' ? fact : fact?.id;
    if (!factId) return;
    if (!this.selectedFacts.includes(factId)) this.selectedFacts = [factId];
    this.selectedTimelineEntryId = null;
    this.selectedNode = { type: 'fact', id: factId };
    if (this.selectedFacts.length > 1) {
      this.applyMultiFactSelectionHighlight();
      return;
    }
    this.applyLineageHighlightForFact(factId);
  },

  selectGoal(goalId) {
    if (!goalId) return;
    this.selectedFacts = [];
    this.selectedTimelineEntryId = null;
    this.selectedNode = { type: 'goal', id: goalId };
    if (!this.cy) return;
    this.cy.elements().removeClass('highlight focus faded');
    const node = this.cy.getElementById(goalId);
    if (node.length > 0) node.addClass('focus');
  },

  selectFinding(findingId) {
    if (!findingId) return;
    this.selectedFacts = [];
    this.selectedTimelineEntryId = null;
    this.selectedNode = { type: 'finding', id: findingId };
    if (!this.cy) return;
    this.cy.elements().removeClass('highlight focus faded');
    const node = this.cy.getElementById(findingId);
    if (node.length > 0) node.addClass('focus');
  },

  selectedGoalRecord() {
    if (!this.project || this.selectedNode?.type !== 'goal') return null;
    return (this.project.goals || []).find((goal) => goal.id === this.selectedNode.id) || null;
  },

  selectedFindingRecord() {
    if (!this.project || this.selectedNode?.type !== 'finding') return null;
    return (
      (this.project.findings || []).find((finding) => finding.id === this.selectedNode.id) || null
    );
  },

  refreshGraphDecorations() {
    if (!this.selectedNode) {
      this.syncFactSelections();
      return;
    }
    if (this.selectedNode.type === 'step') this.applyLineageHighlightForStep(this.selectedNode.id);
    if (
      this.selectedNode.type === 'goal' ||
      this.selectedNode.type === 'finding' ||
      this.selectedNode.type === 'digest'
    ) {
      this.syncFactSelections();
      return;
    }
    if (this.selectedNode.type === 'fact') {
      if (this.selectedFacts.length > 1 && this.selectedFacts.includes(this.selectedNode.id)) {
        this.applyMultiFactSelectionHighlight();
        return;
      }
      this.applyLineageHighlightForFact(this.selectedNode.id);
    }
  },

  syncFactSelections() {
    if (!this.cy) return;
    this.cy.nodes().removeClass('selected-fact');
    for (const fid of this.selectedFacts) {
      const node = this.cy.getElementById(fid);
      if (node.length > 0) node.addClass('selected-fact');
    }
  },

  collectFactLineage(fid) {
    const upstreamFacts = new Set();
    const upstreamSteps = new Set();

    // The two walkers are mutually recursive; declare both names before either runs.
    let walkStepUpstream;

    const walkFactUpstream = (factId) => {
      if (upstreamFacts.has(factId) || !this.project) return;
      upstreamFacts.add(factId);
      for (const step of this.project.steps) {
        if (step.to === factId) walkStepUpstream(step.id);
      }
    };

    walkStepUpstream = (iid) => {
      if (upstreamSteps.has(iid) || !this.project) return;
      upstreamSteps.add(iid);
      const step = this.project.steps.find((i) => i.id === iid);
      if (!step) return;
      for (const sourceId of step.from) walkFactUpstream(sourceId);
    };

    walkFactUpstream(fid);
    return { upstreamFacts, upstreamSteps };
  },

  collectStepElements(step, nodeIds, edgeIds) {
    if (step.to) nodeIds.add(step.to);
    else nodeIds.add(`_ph_${step.id}`);
    for (const sourceId of step.from) {
      nodeIds.add(sourceId);
      edgeIds.add(`${step.id}_${sourceId}`);
    }
    if (step.goal) {
      nodeIds.add(step.goal);
      edgeIds.add(`${step.id}_goal`);
    }
  },

  applyLineageHighlightForStep(stepId) {
    if (!this.cy || !this.project) return;
    const step = this.project.steps.find((i) => i.id === stepId);
    if (!step) return;
    this.cy.elements().removeClass('highlight focus faded');

    const nodeIds = new Set();
    const edgeIds = new Set();
    this.collectStepElements(step, nodeIds, edgeIds);
    const highlightNodes = this.cy.nodes().filter((n) => nodeIds.has(n.id()));
    const focusEdges = this.cy.edges().filter((e) => edgeIds.has(e.id()));

    highlightNodes.addClass('highlight');
    focusEdges.addClass('focus');

    const visible = highlightNodes.add(focusEdges);
    this.cy.elements().not(visible).addClass('faded');
    this.syncFactSelections();
  },

  applyLineageHighlightForFact(factId) {
    if (!this.cy || !this.project) return;
    const { upstreamFacts, upstreamSteps } = this.collectFactLineage(factId);
    const nodeIds = new Set(upstreamFacts);
    const edgeIds = new Set();

    for (const iid of upstreamSteps) {
      const step = this.project.steps.find((i) => i.id === iid);
      if (step) this.collectStepElements(step, nodeIds, edgeIds);
    }

    this.cy.elements().removeClass('highlight focus faded');
    const highlightNodes = this.cy.nodes().filter((n) => nodeIds.has(n.id()));
    const highlightEdges = this.cy.edges().filter((e) => edgeIds.has(e.id()));
    const focusNode = this.cy.getElementById(factId);

    highlightNodes.addClass('highlight');
    highlightEdges.addClass('highlight');
    focusNode.addClass('focus');

    const visible = highlightNodes.add(highlightEdges).add(focusNode);
    this.cy.elements().not(visible).addClass('faded');
    this.syncFactSelections();
  },

  applyMultiFactSelectionHighlight() {
    if (!this.cy) return;
    this.cy.elements().removeClass('highlight focus faded selected-fact');
    this.syncFactSelections();
  },

  getProducingStep(fid) {
    return this.project ? this.project.steps.find((i) => i.to === fid) || null : null;
  },

  selectedFactId() {
    return this.selectedNode?.type === 'fact' ? this.selectedNode.id : null;
  },

  selectedFactRecord() {
    const factId = this.selectedFactId();
    if (!this.project || !factId) return null;
    return this.project.facts.find((f) => f.id === factId) || null;
  },

  selectedFactProducingStep() {
    const factId = this.selectedFactId();
    return factId ? this.getProducingStep(factId) : null;
  },

  selectedFactRecords() {
    if (!this.project || this.selectedFacts.length === 0) return [];
    const factsById = new Map(this.project.facts.map((f) => [f.id, f]));
    return this.selectedFacts.map((fid) => factsById.get(fid)).filter(Boolean);
  },

  selectedStepId() {
    return this.selectedNode?.type === 'step' ? this.selectedNode.id : null;
  },

  selectedStepRecord() {
    const stepId = this.selectedStepId();
    if (!this.project || !stepId) return null;
    return this.project.steps.find((i) => i.id === stepId) || null;
  },

  selectedOpenStepRecord() {
    const step = this.selectedStepRecord();
    if (!step || step.to || !this.projectIsActive()) return null;
    return step;
  },

  selectedActionableOpenStepRecord() {
    const step = this.selectedOpenStepRecord();
    if (!step) return null;
    const actor = this.actorName();
    return !step.worker || step.worker === actor ? step : null;
  },

  selectedReleasableOpenStepRecord() {
    const step = this.selectedOpenStepRecord();
    if (!step?.worker) return null;
    return step.worker === this.actorName() ? step : null;
  },

  selectedStepPrimaryActionLabel() {
    const step = this.selectedOpenStepRecord();
    if (!step) return '认领';
    if (!step.worker) return '认领';
    return step.worker === this.actorName() ? '心跳' : '已认领';
  },

  getFactRecord(fid) {
    return this.project ? this.project.facts.find((f) => f.id === fid) || null : null;
  },

  stepDotClass(i) {
    if (i.status === 'abandoned') return 'bg-slate-300';
    if (i.to) return 'bg-teal-400';
    return i.status === 'running' || i.worker ? 'bg-amber-400' : 'bg-slate-300';
  },

  stepStatusClass(i) {
    if (i.status === 'abandoned') return 'text-slate-400';
    if (i.to) return 'text-teal-600';
    return i.status === 'running' || i.worker ? 'text-amber-600' : 'text-slate-400';
  },

  stepStatusLabel(i) {
    if (i.status === 'abandoned') return '已放弃';
    if (i.to) return '已完成';
    return i.status === 'running' || i.worker ? '进行中' : '待处理';
  },
};
