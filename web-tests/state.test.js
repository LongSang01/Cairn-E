// Pure-state factory: every instance must be independent with sane defaults.
import { describe, expect, it } from 'vitest';
import { createState } from '../cairn/src/cairn/server/static/js/state.js';

describe('createState', () => {
  it('returns independent instances', () => {
    const a = createState();
    const b = createState();
    expect(a).not.toBe(b);
    a.openSessionIds.push('s1');
    expect(b.openSessionIds).toEqual([]);
  });

  it('defaults the view shell', () => {
    const s = createState();
    expect(s.view).toBe('list');
    expect(s.polling).toBe(true);
    expect(s.pollDelay).toBe(5000);
    expect(s.layoutMode).toBe('dagre_tb');
    expect(s.sidePanelWidth).toBe(320);
    expect(s.openSessionIds).toEqual([]);
    expect(s.sessionMessageCache).toEqual({});
  });

  it('defaults forms and transient UI state', () => {
    const s = createState();
    expect(s.localPrefs).toEqual({ actor_name: 'Human', layout_mode: 'dagre_tb' });
    expect(s.newProject.hints).toEqual([{ content: '' }]);
    expect(s.toast).toEqual({ show: false, message: '', type: 'info' });
    expect(s.replay.active).toBe(false);
    expect(s.replay.frameIndex).toBe(-1);
    expect(s.selectedFacts).toEqual([]);
    expect(s.selectedNode).toBe(null);
  });
});
