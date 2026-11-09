// Layout engines are fetched on demand.
//
// dagre ships with the first paint (the default mode); klay and elk are ~2.1 MB of
// vendor code that only a user who switches the layout needs. Loading them lazily keeps
// the initial payload at roughly a third of the old size without removing any mode.

const ASSETS = {
  klay: ['/static/vendor/klay.js', '/static/vendor/cytoscape-klay.js'],
  elk: ['/static/vendor/elk.bundled.js', '/static/vendor/cytoscape-elk.js'],
};

const loaded = new Set(['dagre']);
const pending = new Map();

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    // Dynamic scripts default to async; keep execution ordered with the chain below.
    script.async = false;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`无法加载布局资源：${src}`));
    document.head.appendChild(script);
  });
}

export function isLayoutLoaded(engine) {
  return loaded.has(engine);
}

export function ensureLayout(engine) {
  if (!engine || engine === 'dagre' || loaded.has(engine)) return Promise.resolve();
  if (!ASSETS[engine]) return Promise.resolve();
  let task = pending.get(engine);
  if (!task) {
    // Sequential: the cytoscape adapter registers itself against the engine's global,
    // so it must run after the engine script instead of in parallel with it.
    task = ASSETS[engine]
      .reduce((chain, src) => chain.then(() => loadScript(src)), Promise.resolve())
      .then(() => {
        loaded.add(engine);
        pending.delete(engine);
      })
      .catch((error) => {
        pending.delete(engine);
        throw error;
      });
    pending.set(engine, task);
  }
  return task;
}
