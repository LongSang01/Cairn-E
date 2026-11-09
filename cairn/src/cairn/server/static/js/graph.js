// Cytoscape graph construction, styles, layout and updates.

import { ensureLayout, isLayoutLoaded } from './layouts.js';

export const graphMixins = {
  buildElements() {
    const nodes = [];
    const edges = [];
    const goals = this.project.goals || [];
    const findings = this.project.findings || [];
    const rootGoal = goals.find((goal) => !goal.parent) || null;

    // Facts: the world state. The origin is a fact too, not a special node kind.
    for (const f of this.project.facts) {
      const nodeType = f.id === 'origin' ? 'origin' : 'fact';
      const label = this.summarizeFactLabel(f);
      const size = this.factNodeSize(label, nodeType);
      nodes.push({
        data: {
          id: f.id,
          label,
          description: f.description,
          nodeType,
          width: size.width,
          height: size.height,
        },
      });
    }

    // Goals: the termination conditions, rendered as a small tree.
    for (const g of goals) {
      const label = this.summarizeGoalLabel(g);
      const size = this.goalNodeSize(label);
      nodes.push({
        data: {
          id: g.id,
          label,
          description: g.description,
          nodeType: g.parent ? 'subgoal' : 'goal',
          goalStatus: g.status,
          goalId: g.id,
          width: size.width,
          height: size.height,
        },
      });
      if (g.parent) {
        edges.push({
          data: {
            id: `${g.id}_parent`,
            source: g.parent,
            target: g.id,
            edgeType: 'goal_parent',
            status: g.status,
          },
        });
      }
    }

    // Steps: pending/running/abandoned get a placeholder; done ones collapse into the fact
    // they produced (or into the goal for the completion step).
    const targetByStep = new Map();
    for (const step of this.project.steps) {
      const lbl = step.description || step.id;
      const goalTarget = step.goal || (rootGoal ? rootGoal.id : null);
      let target = null;

      if (step.kind === 'completion') {
        target = goalTarget;
        for (const src of step.from) {
          edges.push({
            data: {
              id: `${step.id}_${src}`,
              source: src,
              target,
              stepId: step.id,
              label: lbl,
              status: 'concluded',
              edgeType: 'completion',
            },
          });
        }
        targetByStep.set(step.id, target);
        continue;
      }

      if (step.to) {
        target = step.to;
        for (const src of step.from) {
          edges.push({
            data: {
              id: `${step.id}_${src}`,
              source: src,
              target,
              stepId: step.id,
              label: lbl,
              status: 'concluded',
            },
          });
        }
      } else {
        const phId = `_ph_${step.id}`;
        const nodeSize = this.openStepNodeSize(step);
        const nodeType = this.openStepNodeType(step);
        nodes.push({
          data: {
            id: phId,
            label: this.openStepNodeLabel(step),
            description: step.description,
            nodeType,
            stepId: step.id,
            width: nodeSize.width,
            height: nodeSize.height,
          },
        });
        target = phId;
        for (const src of step.from) {
          edges.push({
            data: {
              id: `${step.id}_${src}`,
              source: src,
              target: phId,
              stepId: step.id,
              label: lbl,
              status: nodeType,
            },
          });
        }
      }

      targetByStep.set(step.id, target);
      if (goalTarget) {
        edges.push({
          data: {
            id: `${step.id}_goal`,
            source: target,
            target: goalTarget,
            stepId: step.id,
            label: '',
            status: step.status,
            edgeType: 'goal_scope',
          },
        });
      }
    }

    // Findings: products of the search, hung off the step that produced them.
    for (const d of findings) {
      const anchor = d.step ? targetByStep.get(d.step) : null;
      const label = d.kind || 'finding';
      const size = this.goalNodeSize(label);
      nodes.push({
        data: {
          id: d.id,
          label,
          description: d.description,
          nodeType: 'finding',
          findingId: d.id,
          findingKind: d.kind,
          findingSeverity: d.severity || 'info',
          width: size.width,
          height: Math.min(size.height, 24),
        },
      });
      if (anchor) {
        edges.push({
          data: { id: `${d.id}_anchor`, source: anchor, target: d.id, edgeType: 'finding' },
        });
      }
    }

    // Digest nodes: the rolling summaries that folded older history away. They hang
    // off the world state so the graph still shows where the omitted detail came from.
    if (this.showDigests && !this.replay.active) {
      const digestGoal = goals.find((goal) => !goal.parent) || null;
      const fallbackAnchor = this.project.facts.some((fact) => fact.id === 'origin')
        ? 'origin'
        : this.project.facts[0]?.id || (digestGoal ? digestGoal.id : null);
      for (const entry of this.digestEntries()) {
        const digestId = `_digest_${entry.kind}`;
        const anchor =
          entry.kind === 'facts' ? fallbackAnchor : digestGoal ? digestGoal.id : fallbackAnchor;
        const label = `${entry.title} · ${entry.covered}`;
        nodes.push({
          data: {
            id: digestId,
            label,
            description: entry.text,
            nodeType: 'digest',
            digestKind: entry.kind,
            width: Math.min(150, Math.max(88, Array.from(label).length * 7 + 20)),
            height: 28,
          },
        });
        if (anchor) {
          edges.push({
            data: {
              id: `${digestId}_anchor`,
              source: anchor,
              target: digestId,
              edgeType: 'digest',
            },
          });
        }
      }
    }
    return { nodes, edges };
  },

  initGraph() {
    // Every entry point rebuilds the cytoscape instance, so the minimap's window
    // listener must be removed here rather than at each call site.
    this.teardownMinimap();
    const container = document.getElementById('cy');
    if (!container) return;
    const { nodes, edges } = this.buildElements();
    // klay/elk may not be fetched yet (see layouts.js). Start with dagre, then re-run
    // the saved mode once its scripts arrive; waiting here would leave `this.cy` unset
    // and the callers that immediately update the graph would draw nothing.
    const engine = this.layoutEngine();
    const ready = engine === 'dagre' || isLayoutLoaded(engine);
    const rawCy = cytoscape({
      container,
      elements: [...nodes, ...edges],
      style: this.graphStyles(),
      layout: this.layoutOpts(true, ready ? engine : 'dagre'),
      minZoom: 0.15,
      maxZoom: 3.5,
    });
    this.cy = rawCy;
    const self = this;
    rawCy.on('tap', 'node', (e) => self.onNodeTap(e));
    rawCy.on('tap', 'edge', (e) => self.onEdgeTap(e));
    rawCy.on('tap', (e) => {
      if (e.target === rawCy) self.clearSelection();
    });
    if (!ready) {
      ensureLayout(engine)
        .then(() => {
          // The user may have switched to another (still loading) engine meanwhile;
          // that switch schedules its own relayout, so this one must not run unloaded.
          if (this.cy === rawCy && isLayoutLoaded(this.layoutEngine())) {
            rawCy.layout(this.layoutOpts()).run();
          }
        })
        .catch((error) => console.error(error));
    }
    this.pulseActive();
    this.setupAutoFit();
    this.setupMinimap();
  },

  graphStyles() {
    const common = {
      'text-valign': 'center',
      'text-halign': 'center',
      'font-family': '-apple-system,BlinkMacSystemFont,Inter,sans-serif',
    };
    return [
      {
        selector: 'node[nodeType="origin"]',
        style: {
          ...common,
          shape: 'round-rectangle',
          'background-color': '#14b8a6',
          label: 'data(label)',
          color: '#fff',
          'font-size': '11px',
          'font-weight': 'bold',
          'text-wrap': 'wrap',
          'text-max-width': '92px',
          'text-overflow-wrap': 'anywhere',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 0,
        },
      },
      {
        selector: 'node[nodeType="goal"]',
        style: {
          ...common,
          shape: 'round-rectangle',
          'background-color': '#f43f5e',
          label: 'data(label)',
          color: '#fff',
          'font-size': '11px',
          'font-weight': 'bold',
          'text-wrap': 'wrap',
          'text-max-width': '92px',
          'text-overflow-wrap': 'anywhere',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 0,
        },
      },
      {
        selector: 'node[nodeType="fact"]',
        style: {
          ...common,
          shape: 'round-rectangle',
          'background-color': '#6366f1',
          label: 'data(label)',
          color: '#fff',
          'font-size': '10px',
          'font-weight': 'bold',
          'text-wrap': 'wrap',
          'text-max-width': '116px',
          'text-overflow-wrap': 'anywhere',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 0,
        },
      },
      {
        selector: 'node[nodeType="in_progress"]',
        style: {
          ...common,
          shape: 'ellipse',
          'background-color': '#f59e0b',
          'background-opacity': 0.8,
          label: '?',
          color: '#fff',
          'font-size': '11px',
          'font-weight': 'bold',
          width: 22,
          height: 22,
          'border-width': 2,
          'border-color': '#d97706',
        },
      },
      {
        selector: 'node[nodeType="unclaimed"]',
        style: {
          ...common,
          shape: 'ellipse',
          'background-color': '#cbd5e1',
          'background-opacity': 0.5,
          label: '?',
          color: '#94a3b8',
          'font-size': '11px',
          'font-weight': 'bold',
          width: 20,
          height: 20,
          'border-width': 1.5,
          'border-color': '#94a3b8',
          'border-style': 'dashed',
        },
      },
      {
        selector: 'node[nodeType="subgoal"]',
        style: {
          ...common,
          shape: 'round-rectangle',
          'background-color': '#fda4af',
          label: 'data(label)',
          color: '#7f1d1d',
          'font-size': '10px',
          'font-weight': 'bold',
          'text-wrap': 'wrap',
          'text-max-width': '130px',
          'text-overflow-wrap': 'anywhere',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 1,
          'border-color': '#fb7185',
        },
      },
      {
        selector: 'node[goalStatus="achieved"]',
        style: { 'background-color': '#34d399', color: '#065f46', 'border-color': '#10b981' },
      },
      {
        selector: 'node[goalStatus="abandoned"], node[nodeType="abandoned"]',
        style: {
          'background-color': '#e2e8f0',
          color: '#94a3b8',
          'border-color': '#cbd5e1',
          'border-style': 'dashed',
          opacity: 0.75,
        },
      },
      {
        selector: 'node[nodeType="finding"]',
        style: {
          ...common,
          shape: 'diamond',
          'background-color': '#eab308',
          label: 'data(label)',
          color: '#422006',
          'font-size': '9px',
          'font-weight': 'bold',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 1,
          'border-color': '#ca8a04',
          'text-wrap': 'wrap',
          'text-max-width': '90px',
        },
      },
      {
        selector: 'node[findingSeverity="critical"]',
        style: { 'background-color': '#dc2626', 'border-color': '#991b1b', color: '#fff' },
      },
      {
        selector: 'node[findingSeverity="high"]',
        style: { 'background-color': '#f97316', 'border-color': '#c2410c', color: '#fff' },
      },
      {
        selector: 'node[findingSeverity="medium"]',
        style: { 'background-color': '#eab308', 'border-color': '#ca8a04', color: '#422006' },
      },
      {
        selector: 'node[findingSeverity="low"]',
        style: { 'background-color': '#0ea5e9', 'border-color': '#0369a1', color: '#fff' },
      },
      {
        selector: 'node[findingSeverity="info"]',
        style: { 'background-color': '#cbd5e1', 'border-color': '#94a3b8', color: '#334155' },
      },
      {
        selector: 'node[nodeType="digest"]',
        style: {
          ...common,
          shape: 'round-rectangle',
          'background-color': '#e0e7ff',
          'background-opacity': 0.92,
          label: 'data(label)',
          color: '#4338ca',
          'font-size': '9px',
          'font-weight': 'bold',
          'text-wrap': 'wrap',
          'text-max-width': '96px',
          'text-overflow-wrap': 'anywhere',
          width: 'data(width)',
          height: 'data(height)',
          'border-width': 1.2,
          'border-color': '#818cf8',
          'border-style': 'dashed',
        },
      },

      {
        selector: 'edge[status="concluded"]',
        style: {
          width: 2,
          'line-color': '#6ee7b7',
          'target-arrow-color': '#6ee7b7',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          label: 'data(label)',
          'font-size': '7px',
          color: '#94a3b8',
          'text-rotation': 'autorotate',
          'text-margin-y': -9,
          'text-max-width': '80px',
          'text-wrap': 'ellipsis',
          'text-background-color': '#f8fafc',
          'text-background-opacity': 0.85,
          'text-background-padding': '2px',
          'text-events': 'yes',
          'arrow-scale': 0.9,
        },
      },
      {
        selector: 'edge[status="in_progress"]',
        style: {
          width: 2,
          'line-color': '#fbbf24',
          'line-style': 'dashed',
          'line-dash-pattern': [8, 4],
          'line-dash-offset': 0,
          'target-arrow-color': '#fbbf24',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          label: 'data(label)',
          'font-size': '7px',
          color: '#b45309',
          'text-rotation': 'autorotate',
          'text-margin-y': -9,
          'text-max-width': '80px',
          'text-wrap': 'ellipsis',
          'text-background-color': '#fffbeb',
          'text-background-opacity': 0.85,
          'text-background-padding': '2px',
          'text-events': 'yes',
          'arrow-scale': 0.9,
        },
      },
      {
        selector: 'edge[status="unclaimed"]',
        style: {
          width: 1.5,
          'line-color': '#cbd5e1',
          'line-style': 'dashed',
          'line-dash-pattern': [5, 5],
          'target-arrow-color': '#cbd5e1',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          label: 'data(label)',
          'font-size': '7px',
          color: '#94a3b8',
          'text-rotation': 'autorotate',
          'text-margin-y': -9,
          'text-max-width': '80px',
          'text-wrap': 'ellipsis',
          'text-background-color': '#f8fafc',
          'text-background-opacity': 0.85,
          'text-background-padding': '2px',
          'text-events': 'yes',
          'arrow-scale': 0.7,
        },
      },
      {
        selector: 'edge[status="abandoned"]',
        style: {
          width: 1.6,
          'line-color': '#cbd5e1',
          'line-style': 'dashed',
          'target-arrow-color': '#cbd5e1',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          label: 'data(label)',
          'font-size': '7px',
          color: '#94a3b8',
          'text-rotation': 'autorotate',
          'text-margin-y': -9,
          'text-max-width': '88px',
          'text-wrap': 'ellipsis',
          opacity: 0.8,
          'arrow-scale': 0.8,
        },
      },
      {
        selector: 'edge[edgeType="goal_parent"]',
        style: {
          label: '',
          width: 1.6,
          'line-color': '#fecdd3',
          'target-arrow-color': '#fecdd3',
          'curve-style': 'bezier',
          'line-style': 'solid',
          'target-arrow-shape': 'triangle',
          'arrow-scale': 0.75,
          'target-distance-from-node': 2,
        },
      },
      {
        selector: 'edge[edgeType="goal_scope"]',
        style: {
          label: '',
          width: 1.4,
          'line-color': '#fda4af',
          'target-arrow-color': '#fda4af',
          'curve-style': 'bezier',
          'line-style': 'dotted',
          'line-dash-pattern': [2, 5],
          'target-arrow-shape': 'triangle-backcurve',
          'arrow-scale': 0.7,
          'target-distance-from-node': 2,
        },
      },
      {
        selector: 'edge[edgeType="completion"]',
        style: { width: 3, 'line-color': '#fb7185', 'target-arrow-color': '#fb7185' },
      },
      {
        selector: 'edge[edgeType="finding"]',
        style: {
          label: '',
          width: 1.4,
          'line-color': '#facc15',
          'target-arrow-color': '#facc15',
          'curve-style': 'bezier',
          'line-style': 'dashed',
          'line-dash-pattern': [3, 3],
          'target-arrow-shape': 'diamond',
          'arrow-scale': 0.6,
        },
      },
      {
        selector: 'edge[edgeType="digest"]',
        style: {
          label: '',
          width: 1.2,
          'line-color': '#a5b4fc',
          'target-arrow-color': '#a5b4fc',
          'curve-style': 'bezier',
          'line-style': 'dashed',
          'line-dash-pattern': [2, 4],
          'target-arrow-shape': 'none',
          opacity: 0.8,
        },
      },

      { selector: '.highlight', style: { 'z-index': 999 } },
      { selector: 'edge.highlight', style: { 'z-index': 999 } },
      {
        selector: 'node.focus',
        style: {
          'border-width': 3,
          'border-color': '#312e81',
          'border-opacity': 0.95,
          'z-index': 1000,
        },
      },
      {
        selector: 'edge.focus',
        style: {
          'z-index': 1000,
          'overlay-color': '#93c5fd',
          'overlay-opacity': 0.22,
          'overlay-padding': 5,
        },
      },
      {
        selector: 'node.selected-fact',
        style: {
          'border-width': 0,
          'underlay-color': '#93c5fd',
          'underlay-padding': 8,
          'underlay-opacity': 0.28,
          'z-index': 1001,
        },
      },
      { selector: '.faded', style: { opacity: 0.5 } },
      { selector: '.fresh', style: { opacity: 0 } },
    ];
  },

  layoutOpts(animate = true, engine = this.layoutEngine()) {
    const direction = this.layoutDirection();
    if (engine === 'elk') {
      const elkDirection = direction === 'TB' ? 'DOWN' : 'RIGHT';
      return {
        name: 'elk',
        fit: true,
        padding: 50,
        animate,
        animationDuration: 350,
        animationEasing: 'ease-in-out-cubic',
        elk: {
          algorithm: 'layered',
          'elk.direction': elkDirection,
          'elk.aspectRatio': '1.5',
          'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
          'elk.spacing.nodeNode': '50',
          'elk.layered.spacing.nodeNodeBetweenLayers': '80',
          'elk.spacing.edgeNode': '25',
          'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
          'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
        },
      };
    }
    if (engine === 'klay') {
      const isHorizontal = direction === 'LR';
      return {
        name: 'klay',
        fit: true,
        padding: 50,
        animate,
        animationDuration: 400,
        animationEasing: 'ease-in-out-cubic',
        klay: {
          direction: direction === 'TB' ? 'DOWN' : 'RIGHT',
          edgeRouting: 'POLYLINE',
          crossingMinimization: 'LAYER_SWEEP',
          nodeLayering: 'NETWORK_SIMPLEX',
          nodePlacement: 'BRANDES_KOEPF',
          separateConnectedComponents: true,
          spacing: isHorizontal ? 52 : 40,
          inLayerSpacingFactor: isHorizontal ? 1.15 : 1.0,
          thoroughness: 8,
        },
      };
    }
    return {
      name: 'dagre',
      rankDir: direction,
      nodeSep: 60,
      rankSep: 80,
      padding: 50,
      fit: true,
      animate,
      animationDuration: 400,
      animationEasing: 'ease-in-out-cubic',
    };
  },

  async relayout(animate = true) {
    if (!this.cy) return;
    const engine = this.layoutEngine();
    try {
      await ensureLayout(engine);
    } catch (error) {
      // Keep the current layout; the mode stays selected and a retry re-attempts load.
      console.error(error);
      return;
    }
    // A newer switch owns the graph now; running a stale mode would either fail (its
    // engine is still loading) or fight the newer layout.
    if (!this.cy || this.layoutEngine() !== engine) return;
    this.cy.layout(this.layoutOpts(animate)).run();
  },

  snapshotNodePositions() {
    const positions = new Map();
    if (!this.cy) return positions;
    this.cy.nodes().forEach((node) => {
      positions.set(node.id(), { x: node.position('x'), y: node.position('y') });
    });
    return positions;
  },

  anchorPositionFromIds(nodeIds, previousPositions, offset = 36) {
    const anchors = [];
    for (const nodeId of nodeIds) {
      const existing = this.cy?.getElementById(nodeId);
      if (existing?.length) {
        anchors.push({ x: existing.position('x'), y: existing.position('y') });
        continue;
      }
      const previous = previousPositions.get(nodeId);
      if (previous) anchors.push(previous);
    }
    if (anchors.length === 0) return null;
    const center = anchors.reduce((acc, pos) => ({ x: acc.x + pos.x, y: acc.y + pos.y }), {
      x: 0,
      y: 0,
    });
    const avg = { x: center.x / anchors.length, y: center.y / anchors.length };
    return this.layoutDirection() === 'TB'
      ? { x: avg.x, y: avg.y + offset }
      : { x: avg.x + offset, y: avg.y };
  },

  initialPositionForNode(nodeData, previousPositions) {
    if (!this.project) return null;
    if (nodeData.stepId) {
      const previousPlaceholder = previousPositions.get(nodeData.id);
      if (previousPlaceholder) return previousPlaceholder;
      const step = this.project.steps.find((item) => item.id === nodeData.stepId);
      return step ? this.anchorPositionFromIds(step.from, previousPositions, 32) : null;
    }

    const producingStep = this.project.steps.find((item) => item.to === nodeData.id);
    if (!producingStep) return null;
    const placeholderPosition = previousPositions.get(`_ph_${producingStep.id}`);
    if (placeholderPosition) return placeholderPosition;
    return this.anchorPositionFromIds(producingStep.from, previousPositions, 44);
  },

  fadeInFreshElement(element) {
    if (!element || element.length === 0) return;
    element.addClass('fresh');
    setTimeout(() => {
      if (!element.inside()) return;
      element.animate(
        { style: { opacity: 1 } },
        {
          duration: 500,
          complete: () => {
            if (!element.inside()) return;
            element.removeStyle('opacity');
          },
        },
      );
      element.removeClass('fresh');
    }, 30);
  },

  updateGraph() {
    if (!this.cy || !this.project) return;
    const { nodes, edges } = this.buildElements();
    const wantNodes = new Set(nodes.map((n) => n.data.id));
    const wantEdges = new Set(edges.map((e) => e.data.id));
    const previousPositions = this.snapshotNodePositions();
    let changed = false;

    this.cy.nodes().forEach((n) => {
      if (!wantNodes.has(n.id())) {
        n.remove();
        changed = true;
      }
    });
    this.cy.edges().forEach((e) => {
      if (!wantEdges.has(e.id())) {
        e.remove();
        changed = true;
      }
    });

    for (const n of nodes) {
      const ex = this.cy.getElementById(n.data.id);
      if (ex.length === 0) {
        const initialPosition = this.initialPositionForNode(n.data, previousPositions);
        const a = this.cy.add(initialPosition ? { ...n, position: initialPosition } : n);
        this.fadeInFreshElement(a);
        changed = true;
      } else if (
        ex.data('nodeType') !== n.data.nodeType ||
        ex.data('label') !== n.data.label ||
        ex.data('description') !== n.data.description ||
        ex.data('width') !== n.data.width ||
        ex.data('height') !== n.data.height ||
        ex.data('findingSeverity') !== n.data.findingSeverity ||
        ex.data('digestKind') !== n.data.digestKind
      ) {
        // Repaint data in place; only a size change can disturb the layout.
        const resized = ex.data('width') !== n.data.width || ex.data('height') !== n.data.height;
        ex.data(n.data);
        if (resized) changed = true;
      }
    }
    for (const e of edges) {
      const ex = this.cy.getElementById(e.data.id);
      if (ex.length === 0) {
        const a = this.cy.add(e);
        this.fadeInFreshElement(a);
      } else if (ex.data('status') !== e.data.status || ex.data('edgeType') !== e.data.edgeType) {
        ex.data(e.data);
      }
    }
    if (changed) this.relayout();
    this.pulseActive();
    this.refreshGraphDecorations();
  },

  pulseActive() {
    if (!this.cy) return;
    this.cy.nodes('[nodeType="in_progress"]').forEach((node) => {
      if (node.scratch('_pulseActive')) return;
      node.scratch('_pulseActive', true);
      const minOpacity = 0.35;
      const maxOpacity = 0.8;
      const pulseOut = () => {
        if (!node.inside()) {
          node.removeScratch('_pulseActive');
          return;
        }
        node.animate(
          { style: { 'background-opacity': minOpacity } },
          { duration: 900, complete: pulseIn },
        );
      };
      const pulseIn = () => {
        if (!node.inside()) {
          node.removeScratch('_pulseActive');
          return;
        }
        node.animate(
          { style: { 'background-opacity': maxOpacity } },
          { duration: 900, complete: pulseOut },
        );
      };
      pulseOut();
    });
    this.cy.edges('[status="in_progress"]').forEach((edge) => {
      if (edge.scratch('_flowActive')) return;
      edge.scratch('_flowActive', true);
      const dashOffset = -12;
      const flow = () => {
        if (!edge.inside() || edge.data('status') !== 'in_progress') {
          edge.removeScratch('_flowActive');
          return;
        }
        edge.animate(
          { style: { 'line-dash-offset': dashOffset } },
          {
            duration: 700,
            complete: () => {
              if (!edge.inside() || edge.data('status') !== 'in_progress') {
                edge.removeScratch('_flowActive');
                return;
              }
              edge.style('line-dash-offset', 0);
              flow();
            },
          },
        );
      };
      flow();
    });
  },

  fitGraph() {
    if (this.cy) this.cy.fit(undefined, 50);
  },

  centerGraphOnElements(eles) {
    if (!this.cy || !eles || eles.length === 0) return;
    if (this._centerAnimation) this._centerAnimation.stop();
    this._centerAnimation = this.cy.animation({
      center: { eles },
      duration: 220,
      easing: 'ease-in-out-cubic',
    });
    this._centerAnimation.play();
  },

  centerGraphOnFact(factId) {
    if (!this.cy || !factId) return;
    const node = this.cy.getElementById(factId);
    if (node.length > 0) this.centerGraphOnElements(node);
  },

  centerGraphOnStep(stepId) {
    if (!this.cy || !stepId) return;
    const edges = this.cy.edges(`[stepId="${stepId}"]`);
    if (edges.length === 0) return;
    this.centerGraphOnElements(edges.add(edges.sources()).add(edges.targets()));
  },

  summarizeFactLabel(fact) {
    if (fact.id === 'origin') return '起点';
    const normalized = fact.description.replace(/\s+/g, ' ').trim();
    const chars = Array.from(normalized);
    if (chars.length <= 24) return normalized || fact.id;
    return `${chars.slice(0, 24).join('')}…`;
  },

  factNodeSize(label, nodeType) {
    const preset =
      nodeType === 'fact'
        ? {
            fontSize: 10,
            maxTextWidth: 116,
            minWidth: 52,
            minHeight: 34,
            paddingX: 10,
            paddingY: 10,
          }
        : {
            fontSize: 11,
            maxTextWidth: 92,
            minWidth: 58,
            minHeight: 38,
            paddingX: 10,
            paddingY: 10,
          };

    const measured = this.measureWrappedText(label, preset.maxTextWidth, preset.fontSize);
    return {
      width: Math.max(preset.minWidth, Math.ceil(measured.width + preset.paddingX * 2)),
      height: Math.max(preset.minHeight, Math.ceil(measured.height + preset.paddingY * 2)),
    };
  },

  measureWrappedText(text, maxWidth, fontSize) {
    const content = (text || '').trim() || ' ';
    const lines = [];
    let currentWidth = 0;
    let currentChars = 0;
    let maxLineWidth = 0;

    const pushLine = () => {
      if (currentChars === 0 && lines.length > 0) lines.push(0);
      else if (currentChars > 0) lines.push(currentWidth);
      maxLineWidth = Math.max(maxLineWidth, currentWidth);
      currentWidth = 0;
      currentChars = 0;
    };

    for (const char of Array.from(content)) {
      if (char === '\n') {
        pushLine();
        continue;
      }
      const charWidth = this.estimateLabelCharWidth(char, fontSize);
      if (currentChars > 0 && currentWidth + charWidth > maxWidth) pushLine();
      currentWidth += charWidth;
      currentChars += 1;
    }

    pushLine();

    const lineCount = Math.max(1, lines.length);
    const lineHeight = fontSize * 1.35;
    return {
      width: Math.min(maxWidth, Math.max(fontSize * 1.6, maxLineWidth)),
      height: lineCount * lineHeight,
    };
  },

  estimateLabelCharWidth(char, fontSize) {
    if (/\s/.test(char)) return fontSize * 0.35;
    if (
      /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE10-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(
        char,
      )
    ) {
      return fontSize * 1.0;
    }
    return fontSize * 0.58;
  },

  applySelectedLayout() {
    this.layoutMode = this.isValidLayoutMode(this.layoutMode) ? this.layoutMode : 'dagre_tb';
    this.localPrefs.layout_mode = this.layoutMode;
    this.saveLocalPrefs();
    if (this.cy) this.relayout();
  },
};
