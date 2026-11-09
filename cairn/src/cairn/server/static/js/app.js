// Assemble the Alpine component from focused mixins.
//
// Alpine's CDN build calls start() in a microtask as soon as its own script runs.
// A deferred ES module would finish after that, so we put cairnApp on window first
// and load Alpine here; x-data="cairnApp()" then always resolves.

import { createState } from './state.js';
import { coreMixins } from './core.js';
import { formatMixins } from './format.js';
import { markupMixins } from './markup.js';
import { projectsMixins } from './projects.js';
import { sessionsMixins } from './sessions.js';
import { questionsMixins } from './questions.js';
import { replayMixins } from './replay.js';
import { graphMixins } from './graph.js';
import { panelsMixins } from './panels.js';
import { minimapMixins } from './minimap.js';
import { selectionMixins } from './selection.js';
import { timelineMixins } from './timeline.js';
import { actionsMixins } from './actions.js';

// Each entry is [name, mixin]; later mixins win under Object.assign.
const mixins = [
  ['core', coreMixins],
  ['format', formatMixins],
  ['markup', markupMixins],
  ['projects', projectsMixins],
  ['sessions', sessionsMixins],
  ['questions', questionsMixins],
  ['replay', replayMixins],
  ['graph', graphMixins],
  ['panels', panelsMixins],
  ['minimap', minimapMixins],
  ['selection', selectionMixins],
  ['timeline', timelineMixins],
  ['actions', actionsMixins],
];

function cairnApp() {
  return Object.assign(createState(), ...mixins.map(([, mixin]) => mixin));
}

// Duplicate names would be silently shadowed by the later mixin (or a method
// would silently replace state), so warn at startup instead of hiding the bug.
function warnOnNameCollisions() {
  const owners = new Map(Object.keys(createState()).map((key) => [key, 'state']));
  for (const [name, mixin] of mixins) {
    for (const key of Object.keys(mixin)) {
      const owner = owners.get(key);
      if (owner !== undefined) {
        console.warn(`[cairn] "${key}" from the ${name} mixin shadows the one from ${owner}`);
      } else {
        owners.set(key, name);
      }
    }
  }
}
warnOnNameCollisions();

window.cairnApp = cairnApp;

function loadAlpine() {
  if (window.Alpine || document.querySelector('script[data-cairn-alpine]')) return;

  const alpineScript = document.createElement('script');
  alpineScript.src = '/static/vendor/alpine.min.js';
  alpineScript.dataset.cairnAlpine = 'true';
  alpineScript.onerror = () => {
    alpineScript.remove();
    const banner = document.createElement('div');
    banner.setAttribute('role', 'alert');
    banner.textContent = '前端脚本加载失败，请刷新页面重试。';
    banner.style.cssText =
      'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:#f8fafc;color:#b91c1c;font:14px system-ui,sans-serif;';
    document.body.appendChild(banner);
  };
  document.head.appendChild(alpineScript);
}

loadAlpine();
