// YAML / timeline / report highlighting and clipboard helpers.

export const markupMixins = {
  escapeHtml(text) {
    return String(text ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },

  highlightYamlScalar(value) {
    const escaped = this.escapeHtml(value);
    if (!value.trim()) return escaped;
    if (/^\s*#.*$/.test(value)) return `<span style="color:#64748b">${escaped}</span>`;
    if (/^\s*['"].*['"]\s*$/.test(value)) return `<span style="color:#15803d">${escaped}</span>`;
    if (/^\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})\s*$/.test(value))
      return `<span style="color:#b45309">${escaped}</span>`;
    if (/^\s*(true|false|null|~)\s*$/i.test(value))
      return `<span style="color:#b91c1c">${escaped}</span>`;
    if (/^\s*-?\d+(\.\d+)?\s*$/.test(value)) return `<span style="color:#0f766e">${escaped}</span>`;
    if (/^\s*(origin|goal|f\d+|i\d+)\s*$/i.test(value))
      return `<span style="color:#6d28d9">${escaped}</span>`;
    return `<span style="color:#0f172a">${escaped}</span>`;
  },

  highlightYamlLine(line) {
    if (/^\s*$/.test(line)) return '';
    if (/^\s*#/.test(line)) return `<span style="color:#64748b">${this.escapeHtml(line)}</span>`;

    const listKeyMatch = line.match(/^(\s*-\s+)([^:#\n][^:]*):(.*)$/);
    if (listKeyMatch) {
      const [, prefix, key, rest] = listKeyMatch;
      return `${this.escapeHtml(prefix)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
    }

    const keyMatch = line.match(/^(\s*)([^:#\n][^:]*):(.*)$/);
    if (keyMatch) {
      const [, indent, key, rest] = keyMatch;
      return `${this.escapeHtml(indent)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
    }

    const listValueMatch = line.match(/^(\s*-\s+)(.*)$/);
    if (listValueMatch) {
      const [, prefix, rest] = listValueMatch;
      return `${this.escapeHtml(prefix)}${this.highlightYamlScalar(rest)}`;
    }

    return this.highlightYamlScalar(line);
  },

  yamlSectionTint(sectionName) {
    const tints = {
      project: { header: '#eff6ff', body: '#fafcff', itemA: '#f3f8ff', itemB: '#edf5ff' },
      hints: { header: '#fffbeb', body: '#fffef8', itemA: '#fffaf0', itemB: '#fff6e8' },
      facts: { header: '#eef2ff', body: '#fafaff', itemA: '#f5f7ff', itemB: '#eef3ff' },
      steps: { header: '#ecfdf5', body: '#f8fdfb', itemA: '#f1fbf5', itemB: '#eaf8ef' },
      findings: { header: '#fffbeb', body: '#fffef8', itemA: '#fffaf0', itemB: '#fff6e8' },
      documents: { header: '#f0f9ff', body: '#f8fdff', itemA: '#f1faff', itemB: '#e9f7ff' },
    };
    return (
      tints[sectionName] || {
        header: '#f8fafc',
        body: '#ffffff',
        itemA: '#f8fafc',
        itemB: '#f1f5f9',
      }
    );
  },

  async copyYamlPreview() {
    if (!this.yamlPreviewText) return;
    try {
      await this.copyText(this.yamlPreviewText);
      this.showToast('已复制');
    } catch {
      this.showToast('复制失败', 'error');
    }
  },

  async copyText(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '-9999px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    try {
      if (!document.execCommand('copy')) {
        throw new Error('copy command rejected');
      }
    } finally {
      document.body.removeChild(textarea);
    }
  },

  highlightYaml(text) {
    const lines = String(text ?? '').split('\n');
    let currentSection = '';
    let activeItemIndent = null;
    let activeItemStripe = 0;
    let nextItemStripe = 0;

    return lines
      .map((line) => {
        const topLevelMatch = line.match(/^([A-Za-z_][A-Za-z0-9_-]*):\s*$/);
        if (topLevelMatch) {
          currentSection = topLevelMatch[1];
          activeItemIndent = null;
          activeItemStripe = 0;
          nextItemStripe = 0;
        }

        const tint = this.yamlSectionTint(currentSection);
        const indent = (line.match(/^(\s*)/) || ['', ''])[1].length;
        const isBlank = /^\s*$/.test(line);
        const isListItem = /^(\s*)-\s+/.test(line);
        const isTopLevelItem = isListItem && indent === 0;

        if (isTopLevelItem) {
          activeItemIndent = indent;
          activeItemStripe = nextItemStripe;
          nextItemStripe = nextItemStripe === 0 ? 1 : 0;
        } else if (!isBlank && activeItemIndent !== null && indent === 0) {
          activeItemIndent = null;
        }

        const isNestedListItem =
          isListItem && activeItemIndent !== null && indent > activeItemIndent;
        const isItemContinuation =
          !isListItem && !isBlank && activeItemIndent !== null && indent > activeItemIndent;
        const isBlankWithinItem = isBlank && activeItemIndent !== null;
        const isItemLine =
          isTopLevelItem || isNestedListItem || isItemContinuation || isBlankWithinItem;
        const lineHtml = isBlank ? '&nbsp;' : this.highlightYamlLine(line);
        let background = currentSection ? tint.body : 'transparent';
        let fontWeight = '400';

        if (topLevelMatch) {
          background = tint.header;
          fontWeight = '700';
        } else if (isItemLine) {
          background = activeItemStripe === 0 ? tint.itemA : tint.itemB;
        }

        return `<div style="white-space:pre;padding:0 16px;background:${background};font-weight:${fontWeight};color:#0f172a">${lineHtml}</div>`;
      })
      .join('');
  },

  highlightTimeline(text) {
    const lines = String(text ?? '').split('\n');
    const stripes = ['#fffbf5', '#fef5ee'];
    let blockIndex = -1;
    return lines
      .map((line) => {
        if (/^\[/.test(line)) blockIndex++;
        const bg = blockIndex < 0 ? stripes[0] : stripes[blockIndex % 2];
        const isBlank = /^\s*$/.test(line);
        return `<div style="white-space:pre;padding:0 16px;background:${bg};color:#0f172a">${isBlank ? '&nbsp;' : this.escapeHtml(line)}</div>`;
      })
      .join('');
  },

  highlightReport(text) {
    const lines = String(text ?? '').split('\n');
    return lines
      .map((line) => {
        if (/^# /.test(line)) {
          return `<div style="white-space:pre-wrap;padding:10px 16px;background:#fffbeb;color:#92400e;font-weight:700">${this.escapeHtml(line)}</div>`;
        }
        if (/^## /.test(line)) {
          return `<div style="white-space:pre-wrap;padding:6px 16px;background:#fef3c7;color:#92400e;font-weight:600">${this.escapeHtml(line)}</div>`;
        }
        const isBlank = /^\s*$/.test(line);
        return `<div style="white-space:pre-wrap;padding:0 16px;color:#0f172a">${isBlank ? '&nbsp;' : this.escapeHtml(line)}</div>`;
      })
      .join('');
  },
};
