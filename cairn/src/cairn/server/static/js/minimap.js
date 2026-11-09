// Auto-fit and minimap navigation.

export const minimapMixins = {
  setupAutoFit() {
    this.teardownAutoFit();
    const container = document.getElementById('cy');
    if (!container || !this.cy) return;
    let fitTimer = null;
    this._resizeObserver = new ResizeObserver(() => {
      clearTimeout(fitTimer);
      fitTimer = setTimeout(() => {
        if (this.cy) {
          const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
          if (clampedWidth !== this.sidePanelWidth) {
            this.sidePanelWidth = clampedWidth;
            this.saveSidePanelWidth();
          }
          this.cy.resize();
          this.cy.fit(undefined, 50);
        }
      }, 200);
    });
    this._resizeObserver.observe(container);
  },

  teardownAutoFit() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  },

  // --- Minimap: a small canvas overview with a viewport rectangle ---------------

  setupMinimap() {
    const canvas = this.$refs.minimap;
    if (!canvas || !this.cy) return;
    this._minimapCanvas = canvas;
    this._minimapCtx = canvas.getContext('2d');
    this._minimapHandler = () => this.scheduleMinimap();
    this.cy.on('viewport layoutstop add remove position', this._minimapHandler);
    this._minimapResize = () => this.scheduleMinimap();
    window.addEventListener('resize', this._minimapResize);
    this.scheduleMinimap();
  },

  teardownMinimap() {
    if (this._minimapHandler && this.cy) {
      this.cy.off('viewport layoutstop add remove position', this._minimapHandler);
    }
    this._minimapHandler = null;
    if (this._minimapResize) {
      window.removeEventListener('resize', this._minimapResize);
      this._minimapResize = null;
    }
    if (this._minimapFrame) {
      cancelAnimationFrame(this._minimapFrame);
      this._minimapFrame = null;
    }
    this._minimapCanvas = null;
    this._minimapCtx = null;
    this._minimapTransform = null;
  },

  scheduleMinimap() {
    if (this._minimapFrame) return;
    this._minimapFrame = requestAnimationFrame(() => {
      this._minimapFrame = null;
      this.drawMinimap();
    });
  },

  drawMinimap() {
    const canvas = this._minimapCanvas;
    const ctx = this._minimapCtx;
    const cy = this.cy;
    if (!canvas || !ctx || !cy) return;
    const width = canvas.width;
    const height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    const nodes = cy.nodes();
    if (nodes.length === 0) return;
    const box = cy.elements().boundingBox();
    if (!(box.w > 0) || !(box.h > 0)) return;
    const pad = 7;
    const scale = Math.min((width - pad * 2) / box.w, (height - pad * 2) / box.h);
    const ox = pad + (width - pad * 2 - box.w * scale) / 2 - box.x1 * scale;
    const oy = pad + (height - pad * 2 - box.h * scale) / 2 - box.y1 * scale;
    this._minimapTransform = { scale, ox, oy };
    nodes.forEach((node) => {
      const pos = node.position();
      ctx.fillStyle = this.nodeColor(node.data());
      ctx.beginPath();
      ctx.arc(ox + pos.x * scale, oy + pos.y * scale, 2.2, 0, Math.PI * 2);
      ctx.fill();
    });
    const extent = cy.extent();
    ctx.strokeStyle = 'rgba(79, 70, 229, 0.85)';
    ctx.lineWidth = 1.2;
    ctx.strokeRect(
      ox + extent.x1 * scale,
      oy + extent.y1 * scale,
      Math.max(2, (extent.x2 - extent.x1) * scale),
      Math.max(2, (extent.y2 - extent.y1) * scale),
    );
  },

  onMinimapPointer(event) {
    this.panFromMinimap(event);
  },

  onMinimapPointerMove(event) {
    if (event.buttons === 1) this.panFromMinimap(event);
  },

  panFromMinimap(event) {
    const canvas = this._minimapCanvas;
    const cy = this.cy;
    const transform = this._minimapTransform;
    if (!canvas || !cy || !transform) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = (event.clientX - rect.left) * (canvas.width / rect.width);
    const y = (event.clientY - rect.top) * (canvas.height / rect.height);
    const point = {
      x: (x - transform.ox) / transform.scale,
      y: (y - transform.oy) / transform.scale,
    };
    const container = cy.container();
    if (!container) return;
    const zoom = cy.zoom();
    cy.animate(
      {
        pan: {
          x: container.clientWidth / 2 - point.x * zoom,
          y: container.clientHeight / 2 - point.y * zoom,
        },
      },
      { duration: 120, easing: 'ease-out' },
    );
  },
};
