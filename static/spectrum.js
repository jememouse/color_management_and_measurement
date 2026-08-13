/**
 * 光谱曲线图 —— 内联 SVG, 无依赖。
 *
 * ## 关于横轴下方的光谱色带
 *
 * 通用的图表规范会告诫"不要用彩虹配色", 那条规则针对的是**用彩虹表示数值
 * 大小** —— 因为彩虹的明度不单调, 读者无法从颜色判断大小顺序。
 *
 * 这里的情况正好相反: 横轴本身就是波长, 色带里每个位置的颜色**就是该波长
 * 的实际颜色**, 是一一对应的物理事实, 而非人为的数值映射。对做色彩工作的人
 * 来说, "反射率在 450nm 处的凹陷"和"那一段是蓝光"之间的联系是直觉性的,
 * 画出来比标注文字更快。
 *
 * 曲线本身仍严格遵循规范: 单序列不配图例(标题已说明), 多序列按固定顺序取
 * 分类色、绝不循环, 2px 线宽, 网格线退到背景。
 */

const NS = 'http://www.w3.org/2000/svg';

// 分类色按固定顺序取用。超过 5 条时不再生成新色, 而是提示用户清理 ——
// 叠太多条曲线本身也已经读不出信息了。
const SERIES_VARS = ['--series-1', '--series-2', '--series-3', '--series-4', '--series-5'];

export const MAX_SERIES = SERIES_VARS.length;

const MARGIN = { top: 12, right: 14, bottom: 40, left: 46 };
const VIEW_W = 640;
const VIEW_H = 260;

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

/**
 * 波长 → 近似 sRGB。
 *
 * 采用 Dan Bruton 的经典分段线性近似 —— 它不是色度学意义上的精确转换
 * (真正精确需要 CIE 配色函数再转 sRGB), 但作为轴上的视觉参考完全够用,
 * 且在两端有自然的强度衰减, 不会出现突兀的色块边界。
 */
export function wavelengthToRgb(nm) {
  let r = 0;
  let g = 0;
  let b = 0;

  if (nm >= 380 && nm < 440) {
    r = -(nm - 440) / (440 - 380);
    b = 1;
  } else if (nm < 490) {
    g = (nm - 440) / (490 - 440);
    b = 1;
  } else if (nm < 510) {
    g = 1;
    b = -(nm - 510) / (510 - 490);
  } else if (nm < 580) {
    r = (nm - 510) / (580 - 510);
    g = 1;
  } else if (nm < 645) {
    r = 1;
    g = -(nm - 645) / (645 - 580);
  } else if (nm <= 780) {
    r = 1;
  }

  // 人眼在可见光两端的敏感度下降, 相应压暗
  let factor = 1;
  if (nm >= 380 && nm < 420) factor = 0.3 + (0.7 * (nm - 380)) / 40;
  else if (nm > 700 && nm <= 780) factor = 0.3 + (0.7 * (780 - nm)) / 80;
  else if (nm < 380 || nm > 780) factor = 0;

  const gamma = (c) => Math.round(255 * Math.pow(Math.max(0, c) * factor, 0.8));
  return [gamma(r), gamma(g), gamma(b)];
}

function niceTicks(min, max, count) {
  const span = max - min;
  if (span <= 0) return [min];
  const rawStep = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * magnitude);
  const step = candidates.find((c) => c >= rawStep) ?? candidates.at(-1);

  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) {
    ticks.push(Number(v.toFixed(6)));
  }
  return ticks;
}

export class SpectrumChart {
  /**
   * @param {HTMLElement} container 图表容器
   */
  constructor(container) {
    this.container = container;
    this.series = [];
    this.tooltip = null;
    this._onResize = () => this.render();
    window.addEventListener('resize', this._onResize);
  }

  /** 追加一条曲线; 超出上限时挤掉最旧的一条。 */
  add(spectrum, label) {
    this.series.push({
      label: label || `#${spectrum.reading_index ?? this.series.length + 1}`,
      wavelengths: spectrum.wavelengths,
      values: spectrum.values,
    });
    while (this.series.length > MAX_SERIES) this.series.shift();
    this.render();
  }

  clear() {
    this.series = [];
    this.render();
  }

  get count() {
    return this.series.length;
  }

  destroy() {
    window.removeEventListener('resize', this._onResize);
  }

  render() {
    this.container.replaceChildren();

    if (this.series.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'chart__empty';
      empty.textContent = '测量后显示 380–730nm 光谱曲线';
      this.container.append(empty);
      return;
    }

    const allWl = this.series.flatMap((s) => s.wavelengths);
    const allVal = this.series.flatMap((s) => s.values);
    const xMin = Math.min(...allWl);
    const xMax = Math.max(...allWl);

    // y 轴从 0 起 —— 反射率的"零"有物理意义, 截断会夸大曲线起伏
    const yMax = Math.max(1e-6, Math.max(...allVal)) * 1.08;
    const yMin = 0;

    const plotW = VIEW_W - MARGIN.left - MARGIN.right;
    const plotH = VIEW_H - MARGIN.top - MARGIN.bottom;

    const sx = (v) => MARGIN.left + ((v - xMin) / (xMax - xMin || 1)) * plotW;
    const sy = (v) => MARGIN.top + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;

    const svg = el('svg', {
      viewBox: `0 0 ${VIEW_W} ${VIEW_H}`,
      preserveAspectRatio: 'xMidYMid meet',
      role: 'img',
      'aria-label': `光谱曲线, ${this.series.length} 条`,
    });

    // ---- 网格(退到背景) ----
    const grid = el('g', { class: 'chart__grid' });
    const yTicks = niceTicks(yMin, yMax, 4);
    for (const t of yTicks) {
      grid.append(el('line', { x1: MARGIN.left, y1: sy(t), x2: VIEW_W - MARGIN.right, y2: sy(t) }));
    }
    svg.append(grid);

    // ---- 波长色带 ----
    const bandY = MARGIN.top + plotH + 5;
    const bandH = 7;
    const gradientId = `wl-gradient-${Math.random().toString(36).slice(2, 9)}`;
    const defs = el('defs');
    const gradient = el('linearGradient', { id: gradientId, x1: '0', x2: '1', y1: '0', y2: '0' });
    for (let i = 0; i <= 40; i += 1) {
      const nm = xMin + ((xMax - xMin) * i) / 40;
      const [r, g, b] = wavelengthToRgb(nm);
      gradient.append(el('stop', { offset: `${(i / 40) * 100}%`, 'stop-color': `rgb(${r},${g},${b})` }));
    }
    defs.append(gradient);
    svg.append(defs);
    svg.append(
      el('rect', {
        x: MARGIN.left,
        y: bandY,
        width: plotW,
        height: bandH,
        rx: 2,
        fill: `url(#${gradientId})`,
        opacity: 0.85,
      }),
    );

    // ---- 坐标轴 ----
    const axis = el('g', { class: 'chart__axis' });
    axis.append(
      el('line', {
        x1: MARGIN.left,
        y1: MARGIN.top + plotH,
        x2: VIEW_W - MARGIN.right,
        y2: MARGIN.top + plotH,
      }),
    );
    svg.append(axis);

    for (const t of yTicks) {
      const label = el('text', {
        x: MARGIN.left - 7,
        y: sy(t) + 3.5,
        'text-anchor': 'end',
        class: 'chart__tick',
      });
      label.textContent = t.toFixed(yMax > 10 ? 0 : 2);
      svg.append(label);
    }

    for (const t of niceTicks(xMin, xMax, 6)) {
      if (t < xMin || t > xMax) continue;
      const label = el('text', {
        x: sx(t),
        y: bandY + bandH + 13,
        'text-anchor': 'middle',
        class: 'chart__tick',
      });
      label.textContent = String(Math.round(t));
      svg.append(label);
    }

    const xLabel = el('text', {
      x: MARGIN.left + plotW / 2,
      y: VIEW_H - 3,
      'text-anchor': 'middle',
      class: 'chart__axislabel',
    });
    xLabel.textContent = '波长 (nm)';
    svg.append(xLabel);

    const yLabel = el('text', {
      x: 11,
      y: MARGIN.top + plotH / 2,
      'text-anchor': 'middle',
      class: 'chart__axislabel',
      transform: `rotate(-90 11 ${MARGIN.top + plotH / 2})`,
    });
    yLabel.textContent = '反射率 / 强度';
    svg.append(yLabel);

    // ---- 曲线 ----
    const styles = getComputedStyle(document.documentElement);
    this.series.forEach((s, i) => {
      const color = styles.getPropertyValue(SERIES_VARS[i % SERIES_VARS.length]).trim();
      const d = s.wavelengths
        .map((wl, idx) => `${idx === 0 ? 'M' : 'L'}${sx(wl).toFixed(2)},${sy(s.values[idx]).toFixed(2)}`)
        .join(' ');
      svg.append(el('path', { d, class: 'chart__line', stroke: color }));
    });

    // ---- 悬停层 ----
    const crosshair = el('line', {
      class: 'chart__crosshair',
      y1: MARGIN.top,
      y2: MARGIN.top + plotH,
      opacity: 0,
    });
    svg.append(crosshair);

    const markers = this.series.map((_, i) => {
      const color = styles.getPropertyValue(SERIES_VARS[i % SERIES_VARS.length]).trim();
      const dot = el('circle', { r: 4, fill: color, class: 'chart__marker', opacity: 0 });
      svg.append(dot);
      return dot;
    });

    const hit = el('rect', {
      x: MARGIN.left,
      y: MARGIN.top,
      width: plotW,
      height: plotH,
      fill: 'transparent',
    });
    svg.append(hit);

    this.container.append(svg);

    const tooltip = document.createElement('div');
    tooltip.className = 'tooltip';
    tooltip.hidden = true;
    this.container.append(tooltip);

    const primary = this.series[0];
    hit.addEventListener('pointermove', (event) => {
      const rect = svg.getBoundingClientRect();
      const scale = VIEW_W / rect.width;
      const svgX = (event.clientX - rect.left) * scale;

      // 找最近的采样点
      let nearest = 0;
      let best = Infinity;
      primary.wavelengths.forEach((wl, idx) => {
        const dist = Math.abs(sx(wl) - svgX);
        if (dist < best) {
          best = dist;
          nearest = idx;
        }
      });

      const wl = primary.wavelengths[nearest];
      crosshair.setAttribute('x1', sx(wl));
      crosshair.setAttribute('x2', sx(wl));
      crosshair.setAttribute('opacity', 1);

      markers.forEach((dot, i) => {
        const s = this.series[i];
        if (nearest >= s.values.length) {
          dot.setAttribute('opacity', 0);
          return;
        }
        dot.setAttribute('cx', sx(s.wavelengths[nearest]));
        dot.setAttribute('cy', sy(s.values[nearest]));
        dot.setAttribute('opacity', 1);
      });

      const rows = this.series
        .map((s, i) => {
          if (nearest >= s.values.length) return '';
          const color = styles.getPropertyValue(SERIES_VARS[i % SERIES_VARS.length]).trim();
          return `<div><span style="color:${color}">●</span> ${s.label}: ${s.values[nearest].toFixed(4)}</div>`;
        })
        .join('');
      tooltip.innerHTML = `<div class="tooltip__key">${wl.toFixed(0)} nm</div>${rows}`;
      tooltip.hidden = false;
      tooltip.style.left = `${(sx(wl) / VIEW_W) * 100}%`;
      tooltip.style.top = `${(sy(primary.values[nearest]) / VIEW_H) * 100}%`;
    });

    hit.addEventListener('pointerleave', () => {
      crosshair.setAttribute('opacity', 0);
      markers.forEach((dot) => dot.setAttribute('opacity', 0));
      tooltip.hidden = true;
    });
  }
}
