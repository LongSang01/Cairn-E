// formatMixins are Alpine component methods; each test drives one with a fake
// component context built from createState().
import { describe, expect, it } from 'vitest';
import { formatMixins } from '../cairn/src/cairn/server/static/js/format.js';
import { createState } from '../cairn/src/cairn/server/static/js/state.js';

function ctx(overrides = {}) {
  return Object.assign(createState(), formatMixins, overrides);
}

describe('labels and badges', () => {
  it('actorName trims and falls back to Human', () => {
    expect(ctx({ localPrefs: { actor_name: '  Ada  ' } }).actorName()).toBe('Ada');
    expect(ctx({ localPrefs: { actor_name: '   ' } }).actorName()).toBe('Human');
  });

  it('projectStatusBadgeClass maps known statuses and falls back', () => {
    const c = ctx();
    expect(c.projectStatusBadgeClass('active')).toContain('teal');
    expect(c.projectStatusBadgeClass('stopped')).toContain('amber');
    expect(c.projectStatusBadgeClass('completed')).toContain('slate');
    expect(c.projectStatusBadgeClass('nonsense')).toBe(c.projectStatusBadgeClass(undefined));
  });

  it('decideBadgeText renders worker and optional trigger', () => {
    const c = ctx();
    expect(c.decideBadgeText(null)).toBe('');
    expect(c.decideBadgeText({ worker: 'w' }, true)).toBe('Decide · w');
    expect(c.decideBadgeText({ worker: 'w' })).toBe('决策中 · w');
    expect(c.decideBadgeText({ worker: 'w', trigger: 'facts' })).toBe('决策中 · w (facts)');
  });

  it('open step nodes classify status', () => {
    const c = ctx();
    expect(c.openStepNodeType({ status: 'abandoned' })).toBe('abandoned');
    expect(c.openStepNodeType({ status: 'running' })).toBe('in_progress');
    expect(c.openStepNodeType({ status: 'open', worker: 'w' })).toBe('in_progress');
    expect(c.openStepNodeType({ status: 'open' })).toBe('unclaimed');
    expect(c.openStepNodeLabel({ status: 'abandoned' })).toBe('×');
    expect(c.openStepNodeLabel({ status: 'open' })).toBe('?');
  });

  it('summarizeGoalLabel truncates by code points and falls back to id', () => {
    const c = ctx();
    expect(c.summarizeGoalLabel({ id: 'g1', description: 'short' })).toBe('short');
    expect(c.summarizeGoalLabel({ id: 'g1', description: '  ' })).toBe('g1');
    const long = '😀'.repeat(19);
    expect(c.summarizeGoalLabel({ id: 'g1', description: long })).toBe(`${'😀'.repeat(18)}…`);
  });

  it('goalNodeSize clamps width between 84 and 190', () => {
    const c = ctx();
    expect(c.goalNodeSize('')).toEqual({ width: 84, height: 30 });
    expect(c.goalNodeSize('x'.repeat(50))).toEqual({ width: 190, height: 30 });
    expect(c.goalNodeSize('x'.repeat(8))).toEqual({ width: 90, height: 30 });
  });
});

describe('severity and findings', () => {
  it('severity helpers fall back to info', () => {
    const c = ctx();
    expect(c.severityLabel('critical')).toBe('严重');
    expect(c.severityLabel('wat')).toBe(c.severityLabel('info'));
    expect(c.severityBadgeClass('high')).toContain('orange');
    expect(c.severityBadgeClass('wat')).toBe(c.severityBadgeClass('info'));
    expect(c.severityColor('low')).toBe('#0ea5e9');
    expect(c.severityColor('wat')).toBe(c.severityColor('info'));
  });

  it('findingSeverityCounts prefers the server summary', () => {
    const c = ctx({
      findingsSummary: { by_severity: { high: 2, low: 0, info: 1 } },
      project: { findings: [{ severity: 'critical' }] },
    });
    expect(c.findingSeverityCounts()).toEqual([
      { key: 'high', count: 2 },
      { key: 'info', count: 1 },
    ]);
  });

  it('findingSeverityCounts derives from findings without a server summary', () => {
    const c = ctx({
      project: {
        findings: [
          { severity: 'low' },
          { severity: 'low' },
          { severity: 'critical' },
          {},
        ],
      },
    });
    expect(c.findingSeverityCounts()).toEqual([
      { key: 'critical', count: 1 },
      { key: 'low', count: 2 },
      { key: 'info', count: 1 },
    ]);
  });

  it('sortedFindings orders by severity rank with unknowns last', () => {
    const c = ctx({
      project: {
        findings: [{ id: 'a', severity: 'info' }, { id: 'b', severity: 'zz' }, { id: 'c', severity: 'critical' }],
      },
    });
    expect(c.sortedFindings().map((f) => f.id)).toEqual(['c', 'a', 'b']);
  });
});

describe('digests and tokens', () => {
  it('digestEntries only includes non-blank sections', () => {
    expect(ctx().digestEntries()).toEqual([]);
    const c = ctx({
      contextSummary: {
        summary: ' 事实 ',
        step_summary: '   ',
        finding_summary: '发现',
        facts_covered: 3,
        findings_covered: 1,
      },
    });
    const entries = c.digestEntries();
    expect(entries.map((e) => e.kind)).toEqual(['facts', 'findings']);
    expect(c.contextCoveredTotal()).toBe(4);
  });

  it('formatTokens scales through k and M', () => {
    const c = ctx();
    expect(c.formatTokens(0)).toBe('0');
    expect(c.formatTokens(999)).toBe('999');
    expect(c.formatTokens(1500)).toBe('1.5k');
    expect(c.formatTokens(1_000_000)).toBe('1.00M');
    expect(c.formatTokens('2500000')).toBe('2.50M');
    expect(c.formatTokens(undefined)).toBe('0');
  });

  it('usageTooltip lists sessions, tokens and phases', () => {
    const c = ctx({
      usage: {
        sessions: 2,
        input_tokens: 1000,
        output_tokens: 500,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        by_phase: { decide: { total_tokens: 1500 } },
      },
    });
    const tooltip = c.usageTooltip();
    expect(tooltip).toContain('2 次运行');
    expect(tooltip).toContain('输入 1.0k');
    expect(tooltip).not.toContain('缓存读');
    expect(tooltip).toContain('decide: 1.5k');
  });
});

describe('session labels', () => {
  const steps = [{ id: 's1', title: '步骤一', description: 'desc' }];

  it('sessionSummary prefers summary, then step, then phase defaults', () => {
    const c = ctx({ project: { steps } });
    expect(c.sessionSummary({ summary: '现成', step: 's1' })).toBe('现成');
    expect(c.sessionSummary({ step: 's1' })).toBe('步骤一');
    expect(c.sessionSummary({ step: 'missing', phase: 'decide' })).toBe('决策下一步');
    expect(c.sessionSummary({ step: 'missing', phase: 'execute_conclude' })).toBe('收尾并提交结论');
    expect(c.sessionSummary({ step: 'missing' })).toBe('');
    expect(c.sessionSummary(null)).toBe('');
  });

  it('sessionUsageTitle is empty without tokens, else itemized', () => {
    const c = ctx();
    expect(c.sessionUsageTitle({ total_tokens: 0 })).toBe('');
    const title = c.sessionUsageTitle({
      input_tokens: 10,
      output_tokens: 20,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 30,
    });
    expect(title).toContain('输入 10');
    expect(title).toContain('合计 30');
  });
});
