// The budget dialog is the one place a human types a token budget, so the M -> tokens
// conversion is pinned here: a regression would silently multiply (or divide) a cap by
// a million and the request would still look valid to the server.
import { describe, expect, it } from 'vitest';
import { createState } from '../cairn/src/cairn/server/static/js/state.js';
import { formatMixins } from '../cairn/src/cairn/server/static/js/format.js';
import { projectsMixins } from '../cairn/src/cairn/server/static/js/projects.js';

function ctx(overrides = {}) {
  return Object.assign(createState(), formatMixins, projectsMixins, overrides);
}

function recorder() {
  const calls = [];
  const toasts = [];
  return {
    calls,
    toasts,
    api(method, path, body) {
      calls.push({ method, path, body });
      return Promise.resolve({
        status: 'active',
        token_budget: body.token_budget,
        blocked_reason: null,
      });
    },
    showToast(message, kind) {
      toasts.push({ message, kind });
    },
    loadProjects() {
      return Promise.resolve();
    },
  };
}

describe('token budget dialog', () => {
  it('prefills the stored tokens as millions', () => {
    const c = ctx({ project: { project: { token_budget: 1_500_000 } } });

    c.openBudgetDialog();

    expect(c.budgetForm.value).toBe('1.5');
    expect(c.showBudgetModal).toBe(true);
  });

  it('leaves the field empty when no budget is set', () => {
    const c = ctx({ project: { project: { token_budget: null } } });

    c.openBudgetDialog();

    expect(c.budgetForm.value).toBe('');
  });

  it('sends raw tokens when saving a value typed in millions', async () => {
    const recorderStub = recorder();
    const c = ctx({
      project: { project: { id: 'p1', token_budget: null } },
      selectedProjectId: 'p1',
      budgetForm: { value: '0.25' },
      api: recorderStub.api,
      showToast: recorderStub.showToast,
      loadProjects: recorderStub.loadProjects,
    });

    await c.saveBudget();

    expect(recorderStub.calls).toEqual([
      { method: 'PUT', path: '/projects/p1/budget', body: { token_budget: 250_000 } },
    ]);
    expect(c.showBudgetModal).toBe(false);
    expect(c.project.project.token_budget).toBe(250_000);
  });

  it('clears the budget with a null', async () => {
    const recorderStub = recorder();
    const c = ctx({
      project: { project: { id: 'p1', token_budget: 1_000_000 } },
      selectedProjectId: 'p1',
      budgetForm: { value: '1' },
      api: recorderStub.api,
      showToast: recorderStub.showToast,
      loadProjects: recorderStub.loadProjects,
    });

    await c.saveBudget(true);

    expect(recorderStub.calls[0].body).toEqual({ token_budget: null });
  });

  it('rejects input that rounds to zero instead of clearing the budget', async () => {
    const recorderStub = recorder();
    const c = ctx({
      project: { project: { id: 'p1', token_budget: 1_000_000 } },
      selectedProjectId: 'p1',
      api: recorderStub.api,
      showToast: recorderStub.showToast,
      loadProjects: recorderStub.loadProjects,
    });
    c.openBudgetDialog();
    c.budgetForm.value = '0.0000001';

    await c.saveBudget();

    expect(recorderStub.calls).toEqual([]);
    expect(recorderStub.toasts[0].kind).toBe('error');
    expect(recorderStub.toasts[0].message).toContain('M');
    // The dialog stays open so the typo can be fixed.
    expect(c.showBudgetModal).toBe(true);
  });
});
