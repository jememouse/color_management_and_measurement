/**
 * Color Workbench 主逻辑。
 *
 * 数据流是单向的:
 *
 *     用户操作 -> api.start(...) -> 后端 spawn ArgyllCMS
 *                                        |
 *     界面渲染 <- SSE 事件 <- 解析器 <----+
 *
 * 界面不自行推断状态, 一切以 SSE 推来的事件为准。这样刷新页面、多开标签页、
 * 甚至中途接入, 看到的都是同一份真实状态。
 */

import { api, ApiError, connectStream } from './api.js';
import { SpectrumChart, MAX_SERIES } from './spectrum.js';

// ==========================================================================
// 状态
// ==========================================================================

const state = {
  env: null,
  devices: { instruments: [], displays: [], has_instrument: false },
  options: null,
  session: { state: 'idle', label: null },
  readings: [],
  activePanel: 'spot',
  /** 显示器校准流程走到第几步 */
  displayStep: 0,
  displaySteps: [],
  /** 当前会话是哪个流程发起的, 用于在结束时推进步骤 */
  pendingAction: null,
  /** 仪器自检档案, 由 SSE 的 instrument_info 事件累积 */
  instrument: {},
};

let chart = null;
let stream = null;

const $ = (id) => document.getElementById(id);
const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

// ==========================================================================
// 通用 UI
// ==========================================================================

function banner(containerId, kind, message, detail) {
  const icons = { error: '✕', warning: '!', info: 'i', success: '✓' };
  const host = $(containerId);
  if (!host) return;
  host.innerHTML = `
    <div class="banner banner--${kind}">
      <span class="banner__icon">${icons[kind] || 'i'}</span>
      <div>
        <div>${escapeHtml(message)}</div>
        ${detail ? `<div class="small muted" style="margin-top:2px">${escapeHtml(detail)}</div>` : ''}
      </div>
    </div>`;
}

function clearBanner(containerId) {
  const host = $(containerId);
  if (host) host.innerHTML = '';
}

/** 当前面板对应的提示条容器 */
function currentBannerId() {
  return { spot: 'spot-banner', display: 'display-banner', chart: 'chart-banner' }[state.activePanel] || 'spot-banner';
}

function reportError(error) {
  const message = error instanceof ApiError ? error.message : String(error);
  banner(currentBannerId(), 'error', message);
  appendConsole(`\n[界面] ${message}\n`);
}

// ==========================================================================
// 终端
// ==========================================================================

const consoleBody = $('console-body');
let consoleAtBottom = true;

consoleBody.addEventListener('scroll', () => {
  const gap = consoleBody.scrollHeight - consoleBody.scrollTop - consoleBody.clientHeight;
  consoleAtBottom = gap < 40;
});

function appendConsole(text) {
  if (!text) return;
  consoleBody.append(document.createTextNode(text));

  // 只保留末尾若干节点, 否则长任务(dispread 上千色块)会把 DOM 撑爆
  while (consoleBody.childNodes.length > 900) consoleBody.firstChild.remove();

  // 用户往上翻看历史时不要把他拽回底部
  if (consoleAtBottom) consoleBody.scrollTop = consoleBody.scrollHeight;
}

function setConsole(text) {
  consoleBody.textContent = text || '';
  consoleBody.scrollTop = consoleBody.scrollHeight;
  consoleAtBottom = true;
}

// ==========================================================================
// 会话状态
// ==========================================================================

function isRunning() {
  return state.session.state === 'running';
}

function updateSessionChip() {
  const chip = $('chip-session');
  const text = $('chip-session-text');
  chip.classList.remove('chip--ok', 'chip--busy', 'chip--off');

  if (isRunning()) {
    chip.classList.add('chip--busy');
    text.textContent = state.session.label || '运行中';
  } else {
    text.textContent = '空闲';
  }
  syncControls();
}

/** 按当前会话状态与设备可用性统一开关所有按钮。 */
function syncControls() {
  const running = isRunning();
  const hasInstrument = state.devices.has_instrument;

  $('btn-spot-start').disabled = running || !hasInstrument;
  $('btn-spot-measure').disabled = !running;
  $('btn-spot-stop').disabled = !running;
  $('btn-chart-start').disabled = running || !hasInstrument;
  $('btn-chart-stop').disabled = !running;
  $('btn-disp-stop').disabled = !running;

  document.querySelectorAll('.keybtn').forEach((btn) => {
    btn.disabled = !running;
  });
  document.querySelectorAll('[data-step-action]').forEach((btn) => {
    btn.disabled = running || !hasInstrument;
  });

  $('btn-export-csv').disabled = state.readings.length === 0;
  $('btn-clear-history').disabled = state.readings.length === 0;
  $('btn-clear-spectra').hidden = !chart || chart.count === 0;
}

// ==========================================================================
// SSE 事件
// ==========================================================================

function handleParsed(payload) {
  const event = payload.payload;
  if (!event || !event.type) return;

  switch (event.type) {
    case 'reading':
      onReading(event);
      break;
    case 'spectrum':
      onSpectrum(event);
      break;
    case 'prompt':
      onPrompt(event);
      break;
    case 'error':
      banner(currentBannerId(), event.severity === 'warning' ? 'warning' : 'error', event.message);
      break;
    case 'progress':
      onProgress(event);
      break;
    case 'calibration':
      if (event.status === 'complete') {
        banner(currentBannerId(), 'success', '校准完成，可以开始测量了');
      }
      break;
    case 'profile_check':
      banner(
        'display-banner',
        'success',
        `Profile 校验完成 — 平均色差 ΔE ${event.avg_de.toFixed(2)}，最大 ΔE ${event.peak_de.toFixed(2)}`,
        event.avg_de < 1.5 ? '精度良好' : '平均色差偏大，可考虑提高色块数或校准质量',
      );
      break;
    case 'ambient':
      onAmbient(event);
      break;
    case 'instrument_info':
      onInstrumentInfo(event);
      break;
    default:
      break;
  }
}

function connectSSE() {
  stream = connectStream({
    open: () => clearBanner('spot-banner'),

    hello: () => {},

    snapshot: (data) => {
      state.session = { state: data.state || 'idle', label: data.label || null };
      setConsole(data.output || '');
      updateSessionChip();
    },

    output: (payload) => appendConsole(payload.payload),

    state: (payload) => {
      const info = payload.payload || {};
      state.session = { state: info.state || 'idle', label: info.label || state.session.label };
      $('console-command').textContent = info.argv ? info.argv.slice(1).join(' ').slice(0, 80) : '—';
      updateSessionChip();
    },

    exit: (payload) => {
      const info = payload.payload || {};
      state.session = { state: 'exited', label: null };
      updateSessionChip();
      onSessionExit(info);
    },

    parsed: handleParsed,

    error: () => {
      // EventSource 会自动重连, 这里只在界面上留个痕迹
      $('chip-session-text').textContent = '连接中断，重连中…';
    },
  });
}

function onSessionExit(info) {
  const ok = info.exit_code === 0;
  const action = state.pendingAction;
  state.pendingAction = null;

  if (!ok && info.exit_code !== null && info.exit_code !== -15 && info.exit_code !== -9) {
    banner(currentBannerId(), 'warning', `任务结束，退出码 ${info.exit_code}`, '详情见下方命令输出');
  }

  // 显示器校准流程: 成功则推进到下一步
  if (ok && action && state.displaySteps.length) {
    const index = state.displaySteps.findIndex((s) => s.step === action);
    if (index >= 0 && index === state.displayStep) {
      state.displayStep = Math.min(index + 1, state.displaySteps.length);
      renderDisplaySteps();
      banner('display-banner', 'success', `「${state.displaySteps[index].title}」完成`);
    }
  }

  if (action === 'chartread' || action === 'colprof' || action === 'targen') {
    refreshFiles();
  }
}

// ==========================================================================
// 点测量
// ==========================================================================

function onReading(event) {
  if (event.partial) {
    $('spot-readout').innerHTML = `
      <div class="metrics">
        <div><div class="metric__label">Y</div><div class="metric__value">${event.y.toFixed(3)}</div></div>
        <div><div class="metric__label">L*</div><div class="metric__value">${event.lstar.toFixed(3)}</div></div>
      </div>
      <p class="small muted" style="margin:10px 0 0">仅测得亮度，无完整色度数据</p>`;
    return;
  }

  state.readings.push(event);
  $('spot-reading-index').textContent = `第 ${event.index} 次`;

  const [x, y, z] = event.xyz;
  const [l, a, b] = event.lab;
  const [, c, h] = event.lch;

  $('spot-readout').innerHTML = `
    <div class="readout">
      <div>
        <div class="swatch"><div class="swatch__fill" style="background:${event.hex}"></div></div>
        <div class="swatch__caption">${event.hex}</div>
        ${event.in_gamut ? '' : '<div class="swatch__gamut">超出 sRGB 色域，显示为近似色</div>'}
      </div>
      <div class="metrics">
        <div><div class="metric__label">L*</div><div class="metric__value">${l.toFixed(2)}</div></div>
        <div><div class="metric__label">a*</div><div class="metric__value">${a.toFixed(2)}</div></div>
        <div><div class="metric__label">b*</div><div class="metric__value">${b.toFixed(2)}</div></div>
        <div><div class="metric__label">C*</div><div class="metric__value">${c.toFixed(2)}</div></div>
        <div><div class="metric__label">h°</div><div class="metric__value">${h.toFixed(1)}</div></div>
        <div><div class="metric__label">CCT</div><div class="metric__value">${
          event.cct ? `${Math.round(event.cct)}K` : '—'
        }</div></div>
        <div><div class="metric__label">XYZ</div><div class="metric__value metric__value--wide">${x.toFixed(2)} ${y.toFixed(2)} ${z.toFixed(2)}</div></div>
        <div><div class="metric__label">xy</div><div class="metric__value metric__value--wide">${event.xy[0].toFixed(4)} ${event.xy[1].toFixed(4)}</div></div>
        <div><div class="metric__label">Duv</div><div class="metric__value">${
          event.duv === null || event.duv === undefined ? '—' : event.duv.toFixed(4)
        }</div></div>
      </div>
    </div>`;

  renderHistory();
  syncControls();
}

function onSpectrum(event) {
  chart.add(event, `#${event.reading_index}`);
  if (chart.count >= MAX_SERIES) {
    banner('spot-banner', 'info', `已叠加 ${MAX_SERIES} 条曲线，新的测量会挤掉最旧的一条`);
  }
  syncControls();
}

/**
 * 仪器档案。
 *
 * 除了展示, 还有一个实际作用: 根据 U.V. filter 一栏禁用 M1/M2 选项。
 * 实测发现未配 UV 滤镜的 i1Pro2 一旦选了 M1, spotread 会直接以
 * "Setting requested filter not supported" 退出 —— 与其让用户撞一次墙,
 * 不如在界面上先把不可用的选项关掉。
 */
function onInstrumentInfo(event) {
  state.instrument[event.field] = { label: event.label, value: event.value };

  if (event.field === 'uv_filter') {
    applyFilterCapability(event.supports_uv_filter === true);
  }
  renderInstrumentCard();
}

function applyFilterCapability(supported) {
  for (const id of ['spot-filter', 'chart-filter']) {
    const select = $(id);
    if (!select) continue;
    select.querySelectorAll('option[value]').forEach((option) => {
      if (!option.value) return;
      option.disabled = !supported;
      option.textContent = option.textContent.replace(/（本机不支持）$/, '');
      if (!supported) option.textContent += '（本机不支持）';
    });
    if (!supported && select.value) select.value = '';
  }

  const hint = $('filter-hint');
  if (hint && !supported) {
    hint.textContent = '本机未配备 UV 滤镜硬件，M0–M3 均不可用，请使用「不指定」';
  }
}

function renderInstrumentCard() {
  const entries = Object.entries(state.instrument);
  if (!entries.length) return;

  const rows = entries
    .map(([, info]) => `<tr><td>${escapeHtml(info.label)}</td><td class="mono">${escapeHtml(info.value)}</td></tr>`)
    .join('');

  const host = $('about-instrument');
  if (host) {
    host.innerHTML = `<div class="tablewrap"><table><tbody>${rows}</tbody></table></div>`;
  }
}

function onAmbient(event) {
  const parts = [];
  if (event.lux !== undefined) parts.push(`照度 ${event.lux.toFixed(1)} lx`);
  if (event.cct) parts.push(`色温 ${Math.round(event.cct)}K`);
  if (event.duv !== undefined && event.duv !== null) parts.push(`Duv ${event.duv.toFixed(4)}`);
  if (parts.length) banner('spot-banner', 'info', `环境光：${parts.join(' · ')}`, event.note || '');
}

const PROMPT_TEXT = {
  calibrate_white: '请把仪器扣在白色校准底座上（听到咔哒声，确保完全贴合），然后按空格',
  calibrate_black: '请把仪器置于遮光位置或黑场参考上进行暗电流校准',
  calibrate_transmissive: '请把仪器置于透射白光源上',
  calibrate_tile: '请把仪器放在校准瓷板上',
  calibrate_lamp: '正在做灯管漂移检查 — 请把仪器放在校准瓷板上',
  measure: '请把探头平贴在待测样品上，然后按空格读数',
  measure_patch: '请把探头贴在指定的测试色块上',
  measure_ambient: '请把仪器朝上放在显示器旁，测量环境光',
  return_to_display: '请把仪器放回屏幕上的测试窗口',
  reading_patch: '正在读取色块，请保持仪器不动',
  ready: '就绪 — 按空格读数，按 q 退出',
  confirm: '请按空格继续',
  retry: '读数失败 — 按空格重试，或按 q 放弃',
};

function onPrompt(event) {
  const hint = PROMPT_TEXT[event.kind];
  $('spot-hint').textContent = hint || '';
  // 需要用户动手的提示才弹横幅; 单纯的状态说明只更新行内提示, 免得刷屏
  const NEEDS_ACTION = new Set([
    'calibrate_white', 'calibrate_black', 'calibrate_transmissive',
    'calibrate_tile', 'calibrate_lamp', 'measure_patch',
    'return_to_display', 'measure_ambient', 'retry',
  ]);

  if (NEEDS_ACTION.has(event.kind)) {
    banner(currentBannerId(), event.kind === 'retry' ? 'warning' : 'info', hint || event.text);
  } else if (event.kind === 'ready' || event.kind === 'measure') {
    clearBanner(currentBannerId());
  }
}

function renderHistory() {
  const host = $('history-container');
  if (state.readings.length === 0) {
    host.innerHTML = '<div class="empty">测量记录将在此累积，可导出为 CSV</div>';
    return;
  }

  const rows = state.readings
    .slice()
    .reverse()
    .map((r) => {
      const [l, a, b] = r.lab;
      const [, c, h] = r.lch;
      return `<tr>
        <td class="num">${r.index}</td>
        <td><span class="swatch-cell" style="background:${r.hex}"></span></td>
        <td class="num">${l.toFixed(2)}</td>
        <td class="num">${a.toFixed(2)}</td>
        <td class="num">${b.toFixed(2)}</td>
        <td class="num">${c.toFixed(2)}</td>
        <td class="num">${h.toFixed(1)}</td>
        <td class="num">${r.cct ? Math.round(r.cct) : '—'}</td>
        <td class="num">${r.hex}</td>
      </tr>`;
    })
    .join('');

  host.innerHTML = `
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th>#</th><th>色</th><th>L*</th><th>a*</th><th>b*</th>
          <th>C*</th><th>h°</th><th>CCT</th><th>Hex</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function exportCsv() {
  const header = ['index', 'X', 'Y', 'Z', 'L*', 'a*', 'b*', 'C*', 'h', 'x', 'y', 'CCT', 'Duv', 'hex', 'illuminant'];
  const lines = [header.join(',')];

  for (const r of state.readings) {
    lines.push(
      [
        r.index,
        ...r.xyz.map((v) => v.toFixed(6)),
        ...r.lab.map((v) => v.toFixed(4)),
        r.lch[1].toFixed(4),
        r.lch[2].toFixed(3),
        r.xy[0].toFixed(6),
        r.xy[1].toFixed(6),
        r.cct ? r.cct.toFixed(0) : '',
        r.duv !== null && r.duv !== undefined ? r.duv.toFixed(5) : '',
        r.hex,
        r.illuminant,
      ].join(','),
    );
  }

  // BOM: 让 Excel 正确识别 UTF-8, 否则中文表头会乱码
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `measurements-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

async function startSpotMeasure() {
  clearBanner('spot-banner');
  const mode = $('spot-mode').value;

  const params = {
    instrument: Number($('spot-instrument').value) || 1,
    mode,
    spectrum: $('spot-spectrum').checked,
    high_res: $('spot-highres').checked,
    illuminant: $('spot-illuminant').value || undefined,
    observer: $('spot-observer').value || undefined,
  };

  if (mode === 'reflective' || mode === 'transmissive') {
    const filter = $('spot-filter').value;
    if (filter) params.filter_mode = filter;
  }
  if (mode === 'emissive') {
    params.display_type = $('spot-displaytype').value;
  }

  try {
    state.pendingAction = 'spotread';
    await api.start('spotread', params);
    banner('spot-banner', 'info', '正在初始化仪器…', '首次使用可能需要先做暗电流校准');
  } catch (error) {
    state.pendingAction = null;
    reportError(error);
  }
}

// ==========================================================================
// 显示器校准流程
// ==========================================================================

function displayParams() {
  const name = $('disp-name').value.trim();
  const temp = $('disp-temp').value.trim();
  const brightness = $('disp-brightness').value.trim();

  return {
    name,
    display: Number($('disp-display').value) || 1,
    instrument: Number($('disp-instrument').value) || 1,
    quality: $('disp-quality').value,
    gamma: Number($('disp-gamma').value) || 2.2,
    white_point: temp ? Number(temp) : null,
    brightness: brightness ? Number(brightness) : null,
    patches: Number($('disp-patches').value) || 500,
    algorithm: $('disp-algorithm').value,
    interactive: $('disp-interactive').checked,
  };
}

/** 把界面参数翻译成各步骤的后端 action + params。 */
function stepRequest(step) {
  const p = displayParams();

  switch (step) {
    case 'dispcal':
      return [
        'dispcal',
        {
          name: p.name,
          display: p.display,
          instrument: p.instrument,
          quality: p.quality,
          white_point: p.white_point,
          brightness: p.brightness,
          gamma: p.gamma,
          interactive_adjust: p.interactive,
        },
      ];
    case 'targen':
      return ['targen', { name: p.name, patches: p.patches, device: 'rgb' }];
    case 'dispread':
      return [
        'dispread',
        { name: p.name, display: p.display, instrument: p.instrument, use_calibration: true },
      ];
    case 'colprof':
      return [
        'colprof',
        {
          name: p.name,
          quality: p.quality,
          algorithm: p.algorithm,
          description: `${p.name} (Color Workbench)`,
        },
      ];
    case 'dispwin':
      return ['install_profile', { name: p.name, display: p.display }];
    default:
      throw new Error(`未知步骤 ${step}`);
  }
}

function renderDisplaySteps() {
  const host = $('display-steps');
  host.innerHTML = state.displaySteps
    .map((step, index) => {
      const done = index < state.displayStep;
      const active = index === state.displayStep;
      const cls = done ? 'step step--done' : active ? 'step step--active' : 'step';
      return `
        <div class="${cls}">
          <div class="step__num">${done ? '✓' : index + 1}</div>
          <div>
            <div class="step__title">${escapeHtml(step.title)}</div>
            <div class="step__detail">${escapeHtml(step.detail)}</div>
          </div>
          <button class="btn btn--sm ${active ? 'btn--primary' : ''}" data-step-action="${step.step}">
            ${done ? '重新运行' : '运行'}
          </button>
        </div>`;
    })
    .join('');

  host.querySelectorAll('[data-step-action]').forEach((btn) => {
    btn.addEventListener('click', () => runDisplayStep(btn.dataset.stepAction));
  });
  syncControls();
}

async function runDisplayStep(step) {
  clearBanner('display-banner');
  try {
    const [action, params] = stepRequest(step);
    state.pendingAction = step;
    await api.start(action, params);

    const messages = {
      dispcal: '校准中 — 请把仪器贴在屏幕中央并保持不动，全程可能需要 5–20 分钟',
      targen: '正在生成测试色块…',
      dispread: '正在逐块测量 — 请勿移动仪器，也不要遮挡屏幕',
      colprof: '正在拟合 ICC Profile…',
      dispwin: '正在安装 Profile…',
    };
    banner('display-banner', 'info', messages[step] || '任务已启动');
  } catch (error) {
    state.pendingAction = null;
    reportError(error);
  }
}

function onProgress(event) {
  const label = { patch: '色块', iteration: '迭代', verify: '验证' }[event.phase] || event.phase;
  const pct = event.total ? Math.round(((event.current || 0) / event.total) * 100) : 0;

  const html = `
    <div class="progress"><div class="progress__bar" style="width:${pct}%"></div></div>
    <div class="progress__text">${label} ${event.current} / ${event.total} · ${pct}%</div>`;

  if (state.activePanel === 'chart') {
    $('chart-progress').innerHTML = html;
  } else {
    $('display-progress').innerHTML = html;
  }
}

// ==========================================================================
// 色卡扫描
// ==========================================================================

async function startChartRead() {
  clearBanner('chart-banner');
  const name = $('chart-name').value;
  if (!name) {
    banner('chart-banner', 'warning', '请先选择色卡定义文件', 'work/ 目录中需要有 .ti2 文件');
    return;
  }

  try {
    state.pendingAction = 'chartread';
    await api.start('chartread', {
      name,
      instrument: Number($('chart-instrument').value) || 1,
      filter_mode: $('chart-filter').value,
      strip_mode: $('chart-strip').checked,
    });
    banner('chart-banner', 'info', '扫描已启动 — 请按屏幕提示逐块或逐行测量');
  } catch (error) {
    state.pendingAction = null;
    reportError(error);
  }
}

// ==========================================================================
// 文件
// ==========================================================================

async function refreshFiles() {
  try {
    const { files } = await api.files();
    renderFiles(files);
    populateChartFiles(files);
  } catch (error) {
    $('files-container').innerHTML = `<div class="empty">${escapeHtml(String(error.message))}</div>`;
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

const SUFFIX_LABEL = {
  '.ti1': '测试色块定义',
  '.ti2': '色卡定义',
  '.ti3': '测量数据',
  '.cal': '校准曲线',
  '.icc': 'ICC Profile',
  '.icm': 'ICC Profile',
  '.sp': '光谱数据',
  '.ccss': '光谱校正',
  '.ccmx': '校正矩阵',
};

function renderFiles(files) {
  const host = $('files-container');
  if (!files.length) {
    host.innerHTML = '<div class="empty">工作目录为空 — 运行测量或校准后会在此产生文件</div>';
    return;
  }

  const rows = files
    .map((file) => {
      const when = new Date(file.modified * 1000).toLocaleString('zh-CN', { hour12: false });
      const kind = SUFFIX_LABEL[file.suffix] || file.suffix.replace('.', '').toUpperCase();
      return `<tr>
        <td class="mono">${escapeHtml(file.name)}</td>
        <td>${escapeHtml(kind)}</td>
        <td class="num">${formatSize(file.size)}</td>
        <td class="mono small">${escapeHtml(when)}</td>
        <td>
          ${file.downloadable ? `<a class="btn btn--sm" href="${api.downloadUrl(file.name)}" download>下载</a>` : ''}
          <button class="btn btn--sm btn--danger" data-delete="${escapeHtml(file.name)}">删除</button>
        </td>
      </tr>`;
    })
    .join('');

  host.innerHTML = `
    <div class="tablewrap">
      <table>
        <thead><tr><th>文件名</th><th>类型</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll('[data-delete]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.delete;
      if (!window.confirm(`确定删除 ${name}？此操作不可撤销。`)) return;
      try {
        await api.deleteFile(name);
        refreshFiles();
      } catch (error) {
        reportError(error);
      }
    });
  });
}

function populateChartFiles(files) {
  const select = $('chart-name');
  const charts = files.filter((f) => f.suffix === '.ti2');
  const previous = select.value;

  select.innerHTML = charts.length
    ? charts.map((f) => {
        const base = f.name.replace(/\.ti2$/, '');
        return `<option value="${escapeHtml(base)}">${escapeHtml(base)}</option>`;
      }).join('')
    : '<option value="">（work/ 中没有 .ti2 文件）</option>';

  if (previous && charts.some((f) => f.name === `${previous}.ti2`)) select.value = previous;
}

// ==========================================================================
// 设备与环境
// ==========================================================================

function fillSelect(select, items, { value, label, selected } = {}) {
  select.innerHTML = items
    .map((item) => {
      const v = value(item);
      return `<option value="${escapeHtml(v)}"${v === selected ? ' selected' : ''}>${escapeHtml(label(item))}</option>`;
    })
    .join('');
}

async function refreshDevices() {
  try {
    state.devices = await api.devices();
  } catch (error) {
    reportError(error);
    return;
  }

  const usable = state.devices.instruments.filter((i) => i.is_measuring_device);
  const chip = $('chip-instrument');
  const chipText = $('chip-instrument-text');
  chip.classList.remove('chip--ok', 'chip--off');

  if (usable.length) {
    chip.classList.add('chip--ok');
    chipText.textContent = usable[0].model || '仪器已连接';
  } else {
    chip.classList.add('chip--off');
    chipText.textContent = '未检测到仪器';
    banner(
      'spot-banner',
      'warning',
      '未检测到测量仪器',
      '请确认 USB 已连接；若 i1Profiler 正在运行，它会独占设备，需先退出',
    );
  }

  for (const id of ['spot-instrument', 'chart-instrument', 'disp-instrument']) {
    fillSelect($(id), usable, {
      value: (i) => String(i.index),
      label: (i) => `${i.index}. ${i.model || i.description}`,
    });
  }

  fillSelect($('disp-display'), state.devices.displays, {
    value: (d) => String(d.index),
    label: (d) => `${d.index}. ${d.name}${d.is_primary ? ' (主)' : ''} — ${d.width}×${d.height}`,
  });

  renderAboutDevices();
  syncControls();
}

function renderAboutDevices() {
  const { instruments, displays } = state.devices;
  $('about-devices').innerHTML = `
    <div>
      <div class="metric__label">测量仪器</div>
      ${
        instruments.length
          ? `<ul class="small" style="margin:6px 0 0;padding-left:18px">${instruments
              .map((i) => `<li>${escapeHtml(i.description)}${i.is_measuring_device ? '' : ' <span class="muted">（非测量设备）</span>'}</li>`)
              .join('')}</ul>`
          : '<p class="small muted">无</p>'
      }
    </div>
    <div>
      <div class="metric__label">显示器</div>
      <ul class="small" style="margin:6px 0 0;padding-left:18px">${displays
        .map((d) => `<li>${escapeHtml(d.name)} — ${d.width}×${d.height} @ (${d.x}, ${d.y})${d.is_primary ? ' <strong>主</strong>' : ''}</li>`)
        .join('')}</ul>
    </div>`;
}

async function loadEnvironment() {
  state.env = await api.status();
  $('argyll-version').textContent = state.env.argyll_version ? `ArgyllCMS ${state.env.argyll_version}` : '';

  $('about-env').innerHTML = `
    <div><div class="metric__label">Python</div><div class="mono">${escapeHtml(state.env.python)}</div></div>
    <div><div class="metric__label">ArgyllCMS 版本</div><div class="mono">${escapeHtml(state.env.argyll_version || '未知')}</div></div>
    <div><div class="metric__label">安装位置</div><div class="mono small">${escapeHtml(state.env.argyll_bin || '未找到')}</div></div>
    <div><div class="metric__label">工作目录</div><div class="mono small">${escapeHtml(state.env.work_dir)}</div></div>`;

  const tools = Object.entries(state.env.tools || {});
  $('about-tools').innerHTML = `
    <div class="tablewrap">
      <table>
        <thead><tr><th>工具</th><th>状态</th></tr></thead>
        <tbody>${tools
          .map(
            ([name, ok]) =>
              `<tr><td class="mono">${escapeHtml(name)}</td><td>${
                ok ? '<span class="chip chip--ok"><span class="chip__dot"></span>可用</span>' : '<span class="chip chip--off"><span class="chip__dot"></span>缺失</span>'
              }</td></tr>`,
          )
          .join('')}</tbody>
      </table>
    </div>`;
}

async function loadOptions() {
  state.options = await api.options();

  fillSelect($('spot-illuminant'), state.options.illuminants, {
    value: (v) => v,
    label: (v) => v,
    selected: 'D50',
  });
  fillSelect($('spot-observer'), state.options.observers, {
    value: (v) => v,
    label: (v) => ({ '1931_2': 'CIE 1931 2°（标准）', '1964_10': 'CIE 1964 10°', '2015_2': 'CIE 2015 2°', '2015_10': 'CIE 2015 10°' }[v] || v),
    selected: '1931_2',
  });

  const { display_profile } = await api.workflow();
  state.displaySteps = display_profile;
  renderDisplaySteps();
}

// ==========================================================================
// 交互绑定
// ==========================================================================

function switchPanel(name) {
  state.activePanel = name;
  document.querySelectorAll('.navitem').forEach((btn) => {
    btn.setAttribute('aria-selected', String(btn.dataset.panel === name));
  });
  document.querySelectorAll('.panel').forEach((panel) => {
    panel.hidden = panel.id !== `panel-${name}`;
  });
  if (name === 'files') refreshFiles();
}

function bindEvents() {
  document.querySelectorAll('.navitem').forEach((btn) => {
    btn.addEventListener('click', () => switchPanel(btn.dataset.panel));
  });

  // 测量模式切换时联动显示相关字段 —— 滤镜对自发光体没有物理意义
  $('spot-mode').addEventListener('change', () => {
    const mode = $('spot-mode').value;
    $('field-filter').hidden = mode === 'emissive' || mode === 'ambient';
    $('field-displaytype').hidden = mode !== 'emissive';
  });

  $('btn-spot-start').addEventListener('click', startSpotMeasure);
  $('btn-spot-measure').addEventListener('click', () => api.sendKey('space').catch(reportError));
  $('btn-spot-stop').addEventListener('click', () => stopSession());
  $('btn-chart-start').addEventListener('click', startChartRead);
  $('btn-chart-stop').addEventListener('click', () => stopSession());
  $('btn-disp-stop').addEventListener('click', () => stopSession());

  $('btn-export-csv').addEventListener('click', exportCsv);
  $('btn-clear-history').addEventListener('click', () => {
    state.readings = [];
    renderHistory();
    $('spot-readout').innerHTML = '<div class="empty">尚无读数</div>';
    $('spot-reading-index').textContent = '';
    syncControls();
  });
  $('btn-clear-spectra').addEventListener('click', () => {
    chart.clear();
    syncControls();
  });

  $('btn-refresh-devices').addEventListener('click', refreshDevices);
  $('btn-refresh-files').addEventListener('click', refreshFiles);

  document.querySelectorAll('.keybtn').forEach((btn) => {
    btn.addEventListener('click', () => api.sendKey(btn.dataset.key).catch(reportError));
  });

  $('console-head').addEventListener('click', (event) => {
    if (event.target.closest('button') && event.target.id !== 'btn-console-toggle') return;
    toggleConsole();
  });
  $('btn-console-toggle').addEventListener('click', (event) => {
    event.stopPropagation();
    toggleConsole();
  });
  $('btn-console-clear').addEventListener('click', (event) => {
    event.stopPropagation();
    setConsole('');
  });

  $('btn-theme').addEventListener('click', toggleTheme);

  // 全局键盘: 会话运行时把按键直接转发给 ArgyllCMS
  document.addEventListener('keydown', (event) => {
    if (!isRunning()) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

    let key = null;
    if (event.key === ' ') key = 'space';
    else if (event.key === 'Enter') key = 'enter';
    else if (event.key === 'Escape') key = 'esc';
    else if (event.key.length === 1) key = event.key;

    if (key) {
      event.preventDefault();
      api.sendKey(key).catch(reportError);
    }
  });
}

async function stopSession() {
  try {
    await api.stop(false);
  } catch (error) {
    reportError(error);
  }
}

function toggleConsole() {
  const box = $('console');
  const collapsed = box.dataset.collapsed === 'true';
  box.dataset.collapsed = String(!collapsed);
  const btn = $('btn-console-toggle');
  btn.textContent = collapsed ? '收起' : '展开';
  btn.setAttribute('aria-expanded', String(collapsed));
}

function toggleTheme() {
  const root = document.documentElement;
  const current = root.getAttribute('data-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const next = current ? (current === 'dark' ? 'light' : 'dark') : prefersDark ? 'light' : 'dark';

  root.setAttribute('data-theme', next);
  localStorage.setItem('workbench-theme', next);
  // 曲线颜色取自 CSS 变量, 主题切换后必须重绘
  if (chart) chart.render();
}

function restoreTheme() {
  const saved = localStorage.getItem('workbench-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
}

// ==========================================================================
// 启动
// ==========================================================================

async function init() {
  restoreTheme();
  chart = new SpectrumChart($('spectrum-chart'));
  bindEvents();
  connectSSE();

  try {
    await loadEnvironment();
    await loadOptions();
  } catch (error) {
    reportError(error);
  }

  await refreshDevices();
  await refreshFiles();
  syncControls();
}

init();
