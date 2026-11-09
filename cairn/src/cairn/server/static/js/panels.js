// Side panel width persistence: pure DOM chrome, kept out of the graph module.

export const panelsMixins = {
  clampPanelWidth(width) {
    const containerWidth =
      document.getElementById('graphLayout')?.getBoundingClientRect().width || window.innerWidth;
    const min = 260;
    const max = Math.max(min, containerWidth - 260);
    return Math.min(max, Math.max(min, width));
  },

  startPanelResize(e) {
    e.currentTarget?.setPointerCapture?.(e.pointerId);
    this.isResizingPanel = true;
    this.onPanelResize(e);
  },

  onPanelResize(e) {
    if (!this.isResizingPanel) return;
    const rect = document.getElementById('graphLayout')?.getBoundingClientRect();
    if (!rect) return;
    this.sidePanelWidth = this.clampPanelWidth(rect.right - e.clientX);
  },

  stopPanelResize() {
    if (!this.isResizingPanel) return;
    this.isResizingPanel = false;
    this.saveSidePanelWidth();
  },
};
