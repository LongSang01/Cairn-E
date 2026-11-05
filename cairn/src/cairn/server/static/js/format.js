// Labels, badges, severity, digests and usage formatting.

import { SEVERITY_ORDER, SEVERITY_RANK, SEVERITY_LABELS, SEVERITY_BADGE_CLASSES, SEVERITY_TEXT_CLASSES, SEVERITY_COLORS } from './constants.js';

export const formatMixins = {
    actorName() {
      return this.localPrefs.actor_name.trim() || 'Human';
    },

    projectStatusBadgeClass(status) {
      const classes = {
        active: 'bg-teal-50 text-teal-600',
        stopped: 'bg-amber-50 text-amber-700',
        completed: 'bg-slate-100 text-slate-500',
      };
      return classes[status] || 'bg-slate-100 text-slate-500';
    },

    decideBadgeText(decide, compact = false) {
      if (!decide) return '';
      if (compact) return `Decide · ${decide.worker}`;
      const trigger = decide.trigger ? ` (${decide.trigger})` : '';
      return `决策中 · ${decide.worker}${trigger}`;
    },

    runningStepBadgeText(count) {
      return `Running · ${count}`;
    },

    openStepNodeType(step) {
      if (step.status === 'abandoned') return 'abandoned';
      return step.status === 'running' || step.worker ? 'in_progress' : 'unclaimed';
    },

    openStepNodeLabel(step) {
      return step.status === 'abandoned' ? '×' : '?';
    },

    openStepNodeSize(step) {
      return { width: 22, height: 22 };
    },

    summarizeGoalLabel(goal) {
      const normalized = (goal.description || '').replace(/\s+/g, ' ').trim();
      const chars = Array.from(normalized);
      if (chars.length <= 18) return normalized || goal.id;
      return `${chars.slice(0, 18).join('')}…`;
    },

    goalNodeSize(label) {
      const chars = Array.from(String(label || ''));
      const width = Math.min(190, Math.max(84, chars.length * 8 + 26));
      return { width, height: 30 };
    },

    projectIsActive() {
      return !this.replay.active && this.project?.project?.status === 'active';
    },

    countProjectsByStatus(status) {
      return this.projects.filter(project => project.status === status).length;
    },

    hasActiveProjects() {
      return this.projects.some(project => project.status === 'active');
    },

    projectCanWriteHints() {
      return !this.replay.active && ['active', 'stopped', 'completed'].includes(this.project?.project?.status);
    },

    canActOnSelectedFacts() {
      return this.projectIsActive() && this.selectedFacts.length > 0;
    },

    // --- Findings: severity grading and the aggregated report -------------------

    severityOrder() {
      return SEVERITY_ORDER;
    },

    severityLabel(severity) {
      return SEVERITY_LABELS[severity] || SEVERITY_LABELS.info;
    },

    severityBadgeClass(severity) {
      return SEVERITY_BADGE_CLASSES[severity] || SEVERITY_BADGE_CLASSES.info;
    },

    severityTextClass(severity) {
      return SEVERITY_TEXT_CLASSES[severity] || SEVERITY_TEXT_CLASSES.info;
    },

    severityColor(severity) {
      return SEVERITY_COLORS[severity] || SEVERITY_COLORS.info;
    },

    findingSeverityCounts() {
      const fromServer = this.findingsSummary?.by_severity;
      if (fromServer && typeof fromServer === 'object') {
        return this.severityOrder()
          .filter(key => Number(fromServer[key]) > 0)
          .map(key => ({ key, count: Number(fromServer[key]) }));
      }
      const counts = {};
      for (const finding of (this.project?.findings || [])) {
        const key = finding.severity || 'info';
        counts[key] = (counts[key] || 0) + 1;
      }
      return this.severityOrder()
        .filter(key => counts[key])
        .map(key => ({ key, count: counts[key] }));
    },

    sortedFindings() {
      const unknown = SEVERITY_ORDER.length;
      return [...(this.project?.findings || [])].sort((a, b) =>
        (SEVERITY_RANK[a.severity] ?? unknown) - (SEVERITY_RANK[b.severity] ?? unknown)
      );
    },

    graphNodeCount() {
      if (!this.project) return 0;
      return this.project.facts.length
        + (this.project.goals || []).length
        + (this.project.steps || []).length
        + (this.project.findings || []).length
        + (this.showDigests ? this.digestEntries().length : 0);
    },

    nodeColor(data) {
      const type = data?.nodeType;
      if (type === 'origin') return '#14b8a6';
      if (type === 'goal') return '#f43f5e';
      if (type === 'subgoal') return '#fda4af';
      if (type === 'fact') return '#6366f1';
      if (type === 'in_progress') return '#f59e0b';
      if (type === 'unclaimed' || type === 'abandoned') return '#cbd5e1';
      if (type === 'digest') return '#818cf8';
      if (type === 'finding') return this.severityColor(data?.findingSeverity || 'info');
      return '#94a3b8';
    },

    // --- Digest nodes: the rolling summaries folded back into the graph ---------

    digestEntries() {
      const summary = this.contextSummary;
      if (!summary) return [];
      const entries = [];
      if ((summary.summary || '').trim()) {
        entries.push({
          kind: 'facts',
          title: '事实摘要',
          unit: '条事实',
          text: summary.summary,
          covered: summary.facts_covered || 0,
        });
      }
      if ((summary.step_summary || '').trim()) {
        entries.push({
          kind: 'steps',
          title: '步骤摘要',
          unit: '个步骤',
          text: summary.step_summary,
          covered: summary.steps_covered || 0,
        });
      }
      if ((summary.finding_summary || '').trim()) {
        entries.push({
          kind: 'findings',
          title: '发现摘要',
          unit: '条发现',
          text: summary.finding_summary,
          covered: summary.findings_covered || 0,
        });
      }
      return entries;
    },

    contextCoveredTotal() {
      return this.digestEntries().reduce((total, entry) => total + (entry.covered || 0), 0);
    },

    digestRecord() {
      if (this.selectedNode?.type !== 'digest') return null;
      return this.digestEntries().find(entry => entry.kind === this.selectedNode.id) || null;
    },

    toggleDigests() {
      this.showDigests = !this.showDigests;
      this.updateGraph();
    },

    selectDigest(kind) {
      if (!kind) return;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type: 'digest', id: kind };
      if (!this.cy) return;
      this.cy.elements().removeClass('highlight focus faded');
      const node = this.cy.getElementById(`_digest_${kind}`);
      if (node.length > 0) node.addClass('focus');
    },

    // --- Token usage -------------------------------------------------------------

    formatTokens(value) {
      const number = Number(value) || 0;
      if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(2)}M`;
      if (number >= 1_000) return `${(number / 1_000).toFixed(1)}k`;
      return String(number);
    },

    usageTotal() {
      return this.usage?.total_tokens || 0;
    },

    usageTooltip() {
      if (!this.usage) return '';
      const parts = [`${this.usage.sessions || 0} 次运行`];
      parts.push(`输入 ${this.formatTokens(this.usage.input_tokens)}`);
      parts.push(`输出 ${this.formatTokens(this.usage.output_tokens)}`);
      if (this.usage.cache_read_tokens) parts.push(`缓存读 ${this.formatTokens(this.usage.cache_read_tokens)}`);
      if (this.usage.cache_write_tokens) parts.push(`缓存写 ${this.formatTokens(this.usage.cache_write_tokens)}`);
      for (const [phase, bucket] of Object.entries(this.usage.by_phase || {})) {
        parts.push(`${phase}: ${this.formatTokens(bucket.total_tokens)}`);
      }
      return parts.join(' · ');
    },

    sessionSummary(session) {
      if (!session) return '';
      if (session.summary) return session.summary;
      // Older rows predate the stored summary: derive one from the step they ran.
      const step = (this.project?.steps || []).find(item => item.id === session.step);
      if (step) return step.title || step.description || '';
      if (session.phase === 'decide') return '决策下一步';
      if (session.phase === 'execute_conclude') return '收尾并提交结论';
      return '';
    },

    sessionUsageTitle(session) {
      if (!session || !session.total_tokens) return '';
      return [
        `输入 ${this.formatTokens(session.input_tokens)}`,
        `输出 ${this.formatTokens(session.output_tokens)}`,
        `缓存读 ${this.formatTokens(session.cache_read_tokens)}`,
        `缓存写 ${this.formatTokens(session.cache_write_tokens)}`,
        `合计 ${this.formatTokens(session.total_tokens)}`,
      ].join(' · ');
    },
};
