// Live session streams and transcript presentation.

import { PHASE_LABELS } from './constants.js';

export const sessionsMixins = {
  isSessionOpen(sessionId) {
    return this.openSessionIds.includes(sessionId);
  },

  sessionMessages(sessionId) {
    return this.sessionMessageCache[sessionId] || [];
  },

  toggleSession(sessionId) {
    if (this.isSessionOpen(sessionId)) {
      this.openSessionIds = this.openSessionIds.filter((id) => id !== sessionId);
      this.stopSessionStream(sessionId);
      return;
    }
    this.openSessionIds = [...this.openSessionIds, sessionId];
    this.startSessionStream(sessionId);
  },

  startSessionStream(sessionId) {
    if (!this.selectedProjectId || this.sessionStreams[sessionId]) return;
    const url = `/projects/${this.selectedProjectId}/sessions/${sessionId}/stream`;
    const source = new EventSource(url);
    this.sessionStreams = { ...this.sessionStreams, [sessionId]: source };
    this.sessionLoadingId = sessionId;

    const applySnapshot = (payload) => {
      if (this.selectedProjectId !== this.project?.project?.id && this.project) return;
      this.sessionLoadingId = this.sessionLoadingId === sessionId ? null : this.sessionLoadingId;
      this.sessionMessageCache = {
        ...this.sessionMessageCache,
        [sessionId]: payload.messages || [],
      };
      this.applySessionMeta(payload.session);
      this.scrollSessionIfPinned();
    };

    source.addEventListener('snapshot', (event) => applySnapshot(JSON.parse(event.data)));
    source.addEventListener('messages', (event) => {
      const payload = JSON.parse(event.data);
      const known = this.sessionMessageCache[sessionId] || [];
      const next = known.concat(payload.messages || []);
      this.sessionMessageCache = { ...this.sessionMessageCache, [sessionId]: next };
      this.sessionLoadingId = this.sessionLoadingId === sessionId ? null : this.sessionLoadingId;
      this.applySessionMeta(payload.session);
      this.scrollSessionIfPinned();
    });
    source.addEventListener('end', (event) => {
      const payload = JSON.parse(event.data);
      this.applySessionMeta(payload.session);
      this.stopSessionStream(sessionId);
    });
    source.addEventListener('gone', () => this.stopSessionStream(sessionId));
    source.onerror = () => {
      // A finished session closes the stream on purpose; only surface real drops.
      if (source.readyState === EventSource.CLOSED) this.stopSessionStream(sessionId);
    };
  },

  stopSessionStream(sessionId) {
    const source = this.sessionStreams[sessionId];
    if (!source) return;
    source.close();
    const next = { ...this.sessionStreams };
    delete next[sessionId];
    this.sessionStreams = next;
  },

  stopAllSessionStreams() {
    Object.keys(this.sessionStreams).forEach((id) => this.stopSessionStream(id));
  },

  applySessionMeta(session) {
    if (!session) return;
    const sessions = (this.project && this.project.sessions) || [];
    const index = sessions.findIndex((item) => item.id === session.id);
    if (index === -1) return;
    sessions.splice(index, 1, { ...sessions[index], ...session });
  },

  scrollSessionIfPinned() {
    if (this.sideTab !== 'sessions') return;
    this.$nextTick(() => {
      (this.$el || document).querySelectorAll('.session-transcript').forEach((el) => {
        const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        if (pinned) el.scrollTop = el.scrollHeight;
      });
      const list = this.$refs.sessionList;
      if (!list) return;
      const pinned = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
      if (pinned) list.scrollTop = list.scrollHeight;
    });
  },

  isSessionRunning(sessionId) {
    const meta = ((this.project && this.project.sessions) || []).find(
      (item) => item.id === sessionId,
    );
    return !!meta && meta.status === 'running';
  },

  isToolMessage(role) {
    return role === 'tool_call' || role === 'tool_result' || role === 'tool';
  },

  isToolMessageOpen(sessionId, seq) {
    return !!this.openToolMessages[`${sessionId}:${seq}`];
  },

  toggleToolMessage(sessionId, seq) {
    const key = `${sessionId}:${seq}`;
    this.openToolMessages = { ...this.openToolMessages, [key]: !this.openToolMessages[key] };
  },

  toolMessageSummary(message) {
    const firstLine =
      String(message.content || '')
        .split('\n')
        .find((line) => line.trim()) || '';
    return firstLine.length > 80 ? `${firstLine.slice(0, 80)}…` : firstLine;
  },

  isSessionListAtBottom() {
    const el = this.$refs.sessionList;
    if (!el) return false;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  },

  stickSessionListToBottom() {
    this.$nextTick(() => {
      const el = this.$refs.sessionList;
      if (el) el.scrollTop = el.scrollHeight;
    });
  },

  resetSessionState() {
    this.stopAllSessionStreams();
    this.openSessionIds = [];
    this.sessionLoadingId = null;
    this.sessionMessageCache = {};
    this.openToolMessages = {};
  },

  // --- Side questions: read-only Q&A, separate from the graph ----------------

  sessionPhaseClass(phase) {
    if (phase === 'decide') return 'bg-violet-50 text-violet-600 ring-1 ring-violet-200';
    if (phase === 'execute') return 'bg-teal-50 text-teal-600 ring-1 ring-teal-200';
    return 'bg-amber-50 text-amber-600 ring-1 ring-amber-200';
  },

  sessionPhaseLabel(phase) {
    return PHASE_LABELS[phase] || phase;
  },

  sessionStatusDotClass(status) {
    if (status === 'running') return 'bg-sky-400 animate-pulse';
    if (status === 'success') return 'bg-emerald-400';
    if (status === 'rejected') return 'bg-amber-400';
    if (status === 'cancelled') return 'bg-slate-300';
    return 'bg-rose-400';
  },

  sessionMessageClass(role) {
    if (role === 'user') return 'bg-brand-50/70 border-brand-100 text-slate-700';
    if (role === 'assistant') return 'bg-white border-slate-200 text-slate-700';
    if (role === 'thinking')
      return 'bg-transparent border-dashed border-slate-200 text-slate-500 italic';
    if (role === 'tool_call') return 'bg-amber-50/70 border-amber-100 text-amber-800 font-mono';
    if (role === 'tool_result') return 'bg-slate-50 border-slate-200 text-slate-600 font-mono';
    if (role === 'system') return 'bg-slate-100 border-slate-200 text-slate-500';
    return 'bg-slate-50 border-slate-100 text-slate-500 font-mono';
  },

  sessionRoleClass(role) {
    if (role === 'user') return 'text-brand-500';
    if (role === 'assistant') return 'text-slate-500';
    if (role === 'thinking') return 'text-violet-400';
    if (role === 'system') return 'text-slate-400';
    return 'text-slate-400';
  },

  sessionRoleLabel(role) {
    const labels = {
      system: '系统',
      user: '用户',
      assistant: '助手',
      thinking: '思考',
      tool_call: '调用',
      tool_result: '结果',
      tool: '输出',
    };
    return labels[role] || role;
  },
};
