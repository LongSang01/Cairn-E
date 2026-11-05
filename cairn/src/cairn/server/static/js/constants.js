// Shared vocabulary for the graph, legend and findings list.

export const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];
export const SEVERITY_RANK = Object.fromEntries(SEVERITY_ORDER.map((key, index) => [key, index]));
export const SEVERITY_LABELS = { critical: '严重', high: '高危', medium: '中危', low: '低危', info: '信息' };
export const SEVERITY_BADGE_CLASSES = {
  critical: 'bg-rose-100 text-rose-700',
  high: 'bg-orange-100 text-orange-700',
  medium: 'bg-amber-100 text-amber-700',
  low: 'bg-sky-100 text-sky-700',
  info: 'bg-slate-100 text-slate-600',
};
export const SEVERITY_TEXT_CLASSES = {
  critical: 'text-rose-600',
  high: 'text-orange-600',
  medium: 'text-amber-600',
  low: 'text-sky-600',
  info: 'text-slate-500',
};
export const SEVERITY_COLORS = {
  critical: '#dc2626',
  high: '#f97316',
  medium: '#eab308',
  low: '#0ea5e9',
  info: '#94a3b8',
};
export const PHASE_LABELS = { decide: '决策', execute: '执行', execute_conclude: '收尾', summary: '摘要', ask: '问答' };
