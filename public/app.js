/*
 * F1 Strategy Lab - dashboard logic (plain JavaScript, no build step).
 *
 * HOW THE PAGE IS ORGANISED
 *   1. Pure helpers      formatting, CSV, grouping, the strategy simulator (no DOM, unit-testable in Node)
 *   2. State + loading   what is selected (year / Grand Prix / driver / tab) and which JSON files are fetched
 *   3. Charts            one function per plot type; ALL of them draw from `chart.rows`
 *   4. Expand modal      Chart | Data | Method | Rejected tabs for every chart
 *   5. Track map         canvas drawing + time slider + running order
 *   6. Tabs              Overall, Season, Race, Driver, Track map, Strategy lab, Data quality
 *
 * KEY RULE: the chart and its data table are both built from the SAME `chart.rows` list that the
 * pipeline wrote, so they can never disagree. Nothing is recalculated here except the strategy
 * simulator and the track-map data table.
 */
'use strict';

/* ====================================================================== 1. PURE HELPERS */
const COMPOUND = { SOFT: '#e10600', MEDIUM: '#ffd12e', HARD: '#e8e8e8', INTERMEDIATE: '#39b54a', WET: '#2f7fe0' };
const PALETTE = ['#4cc9f0', '#f72585', '#ffd166', '#06d6a0', '#b388ff', '#ff9f1c', '#80ed99', '#e5989b'];
const LAP_KEYS = new Set(['lap_time', 'best_lap', 'median_lap', 'avg_pace']);

/** Seconds -> "1:32.456" (lap-time style). */
function fmtLap(s) {
  if (s == null) return '–';
  const m = Math.floor(s / 60), r = s - m * 60;
  return m + ':' + r.toFixed(3).padStart(6, '0');
}
/** One table cell value as text. */
function fmtVal(v, key) {
  if (v == null) return '–';
  if (typeof v === 'number') return LAP_KEYS.has(key) && v > 60 ? fmtLap(v) : String(Number(v.toFixed(3)));
  return String(v);
}
/** Group rows by a key, keeping first-seen order. Returns a Map. */
function groupBy(rows, key) {
  const m = new Map();
  for (const r of rows) { if (!m.has(r[key])) m.set(r[key], []); m.get(r[key]).push(r); }
  return m;
}
/** Rows + column definitions -> CSV text (header uses the labels incl. units). */
function toCSV(columns, rows) {
  const esc = v => { v = v == null ? '' : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const head = columns.map(c => esc(c.label + (c.unit ? ` (${c.unit})` : '')));
  return [head.join(','), ...rows.map(r => columns.map(c => esc(r[c.key])).join(','))].join('\n');
}
/** removed / received, as a percentage string. */
function rate(removed, received) { return received ? (100 * removed / received).toFixed(1) + '%' : '–'; }

/** compound -> sorted [[age, delta], ...] from degradation rows. */
function makeCurves(rows) {
  const out = {};
  for (const r of rows) (out[r.compound] ||= []).push([r.age, r.delta]);
  for (const c in out) out[c].sort((a, b) => a[0] - b[0]);
  return out;
}
/** Delta (s) at a tyre age: interpolate inside the measured range, extend the last slope beyond it. */
function curveAt(pts, age) {
  if (age <= pts[0][0]) return pts[0][1];
  for (let i = 1; i < pts.length; i++) {
    if (age <= pts[i][0]) {
      const [a0, d0] = pts[i - 1], [a1, d1] = pts[i];
      return d0 + (d1 - d0) * (age - a0) / (a1 - a0);
    }
  }
  const n = pts.length, k = Math.min(3, n - 1);
  const slope = k ? Math.max(0, (pts[n - 1][1] - pts[n - 1 - k][1]) / (pts[n - 1][0] - pts[n - 1 - k][0])) : 0;
  return pts[n - 1][1] + slope * (age - pts[n - 1][0]);
}
/**
 * Strategy simulator. Total "time lost" of a strategy = sum of tyre-wear deltas over every lap
 * + pit loss per stop. Base pace is identical for every strategy, so only differences matter.
 * Tries every 1-stop and 2-stop plan using at least two different dry compounds.
 */
function simulate(curves, laps, pitLoss, minStint = 6) {
  const comps = Object.keys(curves).filter(c => c !== 'INTERMEDIATE' && c !== 'WET');
  const cum = {};
  for (const c of comps) {
    cum[c] = [0];
    for (let a = 1; a <= laps; a++) cum[c].push(cum[c][a - 1] + curveAt(curves[c], a));
  }
  const res = [];
  for (const a of comps) for (const b of comps) if (a !== b)
    for (let p = minStint; p <= laps - minStint; p++)
      res.push({ tyres: [a, b], stops: [p], loss: cum[a][p] + cum[b][laps - p] + pitLoss });
  for (const a of comps) for (const b of comps) for (const c of comps) {
    if (a === b && b === c) continue;
    for (let p1 = minStint; p1 <= laps - 2 * minStint; p1++)
      for (let p2 = p1 + minStint; p2 <= laps - minStint; p2++)
        res.push({ tyres: [a, b, c], stops: [p1, p2],
                   loss: cum[a][p1] + cum[b][p2 - p1] + cum[c][laps - p2] + 2 * pitLoss });
  }
  return res.sort((x, y) => x.loss - y.loss);
}
if (typeof module !== 'undefined') module.exports = { fmtLap, fmtVal, groupBy, toCSV, rate, makeCurves, curveAt, simulate };
if (typeof document !== 'undefined') {   // ---- everything below needs a browser (skipped under Node tests)

/* ============================================================== 2. STATE + LOADING */
const DATA = 'data/';
const S = {            // everything the page knows about "what is selected"
  index: null, year: null, key: null, driver: null, tab: 'overall',
  charts: [],          // live Chart.js instances (destroyed on every re-render)
  timers: [],          // setInterval ids of the track animation
  marker: null,        // lap shown as a vertical line on lap-based charts (set by the track slider)
  trackState: null,    // {t, i, race} of the most recently drawn track map
};
const cache = new Map();
const drawQueue = [];   // chart drawings waiting until their canvas is attached to the page
function flushDraws() { while (drawQueue.length) drawQueue.shift()(); }
const TABS = [['overall', 'Overall'], ['season', 'Season'], ['race', 'Race'], ['driver', 'Driver'],
              ['track', 'Track map'], ['strategy', 'Strategy lab'], ['quality', 'Data quality']];
const $ = (sel, el = document) => el.querySelector(sel);

/** Fetch + cache a JSON file from data/. */
async function load(file) {
  if (!cache.has(file)) {
    cache.set(file, fetch(DATA + file).then(r => {
      if (!r.ok) throw new Error(`${file}: HTTP ${r.status}`);
      return r.json();
    }));
  }
  try { return await cache.get(file); } catch (e) { cache.delete(file); throw e; }
}

/** Tiny DOM builder: h('div', {class:'x', onclick: fn}, child, ...). */
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
function select(options, value, onchange) {
  const el = h('select', { onchange: e => onchange(e.target.value) },
    options.map(([v, label]) => h('option', { value: v }, label)));
  el.value = value ?? '';
  return el;
}

/* URL <-> state, so a link reopens the same view. */
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  S.year = p.get('y') ? +p.get('y') : null; S.key = p.get('gp') ? +p.get('gp') : null;
  S.driver = p.get('d') || null; S.tab = p.get('tab') || 'overall';
}
function writeHash() {
  const p = new URLSearchParams();
  if (S.year) p.set('y', S.year); if (S.key) p.set('gp', S.key);
  if (S.driver) p.set('d', S.driver); p.set('tab', S.tab);
  history.replaceState(null, '', '#' + p);
}

const sessionsOf = y => (S.index.sessions[String(y)] || []);
const sessionInfo = () => sessionsOf(S.year).find(s => s.session_key === S.key);
let teamColours = {};
function indexTeamColours() {
  for (const y in S.index.sessions) for (const s of S.index.sessions[y]) for (const d of s.drivers)
    if (d.colour) teamColours[d.team] = d.colour;
}
const teamColour = t => teamColours[t] || '#8a8f9c';

/** Colour + dash for a driver code within a race (teammates share a colour; the 2nd is dashed). */
function driverStyle(code, race) {
  const d = race && race.drivers.find(x => x.code === code);
  if (!d) return { color: PALETTE[0], dash: [] };
  const mates = race.drivers.filter(x => x.team === d.team).sort((a, b) => a.number - b.number);
  return { color: d.colour || teamColour(d.team), dash: mates[0] === d ? [] : [6, 3] };
}

/* ======================================================================== 3. CHARTS */
Chart.defaults.color = '#98a0b3';
Chart.defaults.borderColor = '#2a2f3a';
Chart.register({   // draws a vertical line at S.marker on charts whose x axis is "lap"
  id: 'marker',
  afterDatasetsDraw(c) {
    if (!c.options.plugins.marker || !c.options.plugins.marker.on || S.marker == null || !c.scales.x) return;
    const x = c.scales.x.getPixelForValue(S.marker), a = c.chartArea, g = c.ctx;
    g.save(); g.strokeStyle = '#ffffff'; g.setLineDash([4, 3]); g.beginPath();
    g.moveTo(x, a.top); g.lineTo(x, a.bottom); g.stroke(); g.restore();
  },
});

/** Rows to show: in the Driver tab, charts with a "driver" column are cut down to the selected driver. */
function visibleRows(chart) {
  const hasDriver = chart.columns.some(c => c.key === 'driver');
  return (S.tab === 'driver' && S.driver && hasDriver) ? chart.rows.filter(r => r.driver === S.driver) : chart.rows;
}
const labelOf = (chart, key) => (chart.columns.find(c => c.key === key) || { label: key }).label;

function axisOpts(p, extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'nearest', intersect: false },
    scales: {
      x: { title: { display: !!p.xlabel, text: p.xlabel }, ...(extra.x || {}) },
      y: { title: { display: !!p.ylabel, text: p.ylabel }, reverse: !!p.yreverse, ...(extra.y || {}) },
    },
    plugins: { legend: { display: extra.legend !== false, labels: { boxWidth: 10, font: { size: 10 } } },
               marker: { on: p.x === 'lap' }, ...(extra.plugins || {}) },
  };
}

/** Draw one chart type into a canvas. Returns the Chart.js instance. */
function drawChart(cv, chart, rows, race) {
  const p = chart.plot;
  if (p.type === 'line') {
    const mk = (label, pts, color, dash) => ({ label, data: pts, borderColor: color, backgroundColor: color,
      borderDash: dash || [], pointRadius: 0, pointHoverRadius: 3, borderWidth: 1.6, spanGaps: true, parsing: false });
    let ds = [];
    if (p.group) {
      for (const [g, rs] of groupBy(rows, p.group)) {
        const st = p.group === 'driver' ? driverStyle(g, race)
          : { color: COMPOUND[g] || PALETTE[ds.length % PALETTE.length], dash: [] };
        ds.push(mk(g, rs.filter(r => r[p.y] != null).map(r => ({ x: r[p.x], y: r[p.y] })), st.color, st.dash));
      }
    } else {
      p.ys.forEach((y, i) => ds.push(mk(labelOf(chart, y),
        rows.filter(r => r[y] != null).map(r => ({ x: r[p.x], y: r[y] })), PALETTE[i % PALETTE.length])));
    }
    return new Chart(cv, { type: 'line', data: { datasets: ds }, options: axisOpts(p, { x: { type: 'linear' } }) });
  }
  if (p.type === 'bar') {
    const colors = rows.map(r => p.color_key ? teamColour(r[p.color_key]) : '#e10600');
    return new Chart(cv, { type: 'bar',
      data: { labels: rows.map(r => r[p.x]), datasets: [{ data: rows.map(r => r[p.y]), backgroundColor: colors }] },
      options: axisOpts(p, { legend: false }) });
  }
  if (p.type === 'stacked') {
    const cats = [...new Set(rows.map(r => r[p.x]))];
    const ds = [...groupBy(rows, p.group)].map(([g, rs], i) => ({ label: g, backgroundColor: COMPOUND[g] || PALETTE[i],
      data: cats.map(c => (rs.find(r => r[p.x] === c) || {})[p.y] || 0) }));
    return new Chart(cv, { type: 'bar', data: { labels: cats, datasets: ds },
      options: axisOpts(p, { x: { stacked: true }, y: { stacked: true, max: 100 } }) });
  }
  if (p.type === 'stints') {   // one floating bar per stint, drawn on top of each other (grouped:false)
    const drivers = [...new Set(rows.map(r => r.driver))];
    const maxStint = Math.max(...rows.map(r => r.stint));
    const ds = [];
    for (let n = 1; n <= maxStint; n++) {
      const per = drivers.map(d => rows.find(r => r.driver === d && r.stint === n));
      ds.push({ label: 'Stint ' + n, grouped: false, barPercentage: 0.75,
        data: per.map(r => r ? [r.lap_start - 1, r.lap_end] : null),
        backgroundColor: per.map(r => r ? (COMPOUND[r.compound] || '#888') : 'transparent'),
        borderColor: '#0f1115', borderWidth: 1, _rows: per });
    }
    return new Chart(cv, { type: 'bar', data: { labels: drivers, datasets: ds },
      options: { ...axisOpts({ xlabel: 'Lap', ylabel: '' }, { legend: false, x: { type: 'linear', min: 0 } }),
        indexAxis: 'y',
        plugins: { legend: { display: false }, marker: { on: true },
          tooltip: { callbacks: { label: c => { const r = c.dataset._rows[c.dataIndex];
            return r ? `${r.compound}: laps ${r.lap_start}-${r.lap_end}` : ''; } } } } } });
  }
  throw new Error('unknown plot type ' + p.type);
}

/** Sortable table. `columns` come from the chart, `rows` are the plotted rows. */
function tableEl(columns, rows, opt = {}) {
  let sortKey = null, dir = 1;
  const wrap = h('div', { class: 'tablewrap' });
  const draw = () => {
    const data = sortKey ? [...rows].sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      return (x == null) - (y == null) || (x > y ? 1 : x < y ? -1 : 0) * dir;
    }) : rows;
    const shown = data.slice(0, opt.limit || 2000);
    wrap.replaceChildren(h('table', {},
      h('thead', {}, h('tr', {}, columns.map(c => h('th', { onclick: () => {
        dir = sortKey === c.key ? -dir : 1; sortKey = c.key; draw(); } },
        c.label + (c.unit ? ` (${c.unit})` : '') + (sortKey === c.key ? (dir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, shown.map(r => h('tr', {}, columns.map(c => {
        const v = r[c.key];
        if (c.key === 'compound' && v) return h('td', {}, h('span', { class: 'chip', style: `background:${COMPOUND[v] || '#888'}` }), v);
        return h('td', { class: (typeof v === 'number' ? 'num ' : '') + (c.key === 'raw' ? 'raw' : '') }, fmtVal(v, c.key));
      }))))));
    if (data.length > shown.length) wrap.append(h('p', { class: 'muted', style: 'padding:6px' },
      `Showing the first ${shown.length} of ${data.length} rows. Download the CSV for all of them.`));
  };
  draw();
  return wrap;
}

function stintLegend() {
  return h('div', { class: 'legend' }, Object.entries(COMPOUND).map(([c, col]) =>
    h('span', {}, h('span', { class: 'chip', style: `background:${col}` }), c)));
}

/** Body of a chart card (or of the Chart tab in the modal). */
async function chartBody(chart, race, big) {
  if (chart.status !== 'ok') {
    return h('div', { class: 'empty' }, `Not enough data to draw this (have ${chart.n} rows, need ${chart.need}). `,
      'Open Expand → Rejected to see what was removed.');
  }
  if (chart.plot.type === 'track') return trackView(chart, race);
  const rows = visibleRows(chart);
  if (!rows.length) return h('div', { class: 'empty' }, 'No rows for this selection.');
  if (chart.plot.type === 'table') return tableEl(chart.columns, rows);
  const cv = h('canvas');
  const box = h('div', { class: 'chartbox' + (big ? ' big' : '') }, cv);
  drawQueue.push(() => S.charts.push(drawChart(cv, chart, rows, race)));
  return h('div', {}, box, chart.plot.type === 'stints' ? stintLegend() : null);
}

/** One card: title, badges, Expand button, chart. `meta` = {quality, session_keys, drivers}. */
async function card(chart, race, meta) {
  const wide = ['table', 'track'].includes(chart.plot.type) || chart.id === 'tyre_strategy';
  const rej = chart.tables.reduce((n, t) => n + (meta.quality[t] ? meta.quality[t].rejected + meta.quality[t].excluded : 0), 0);
  return h('section', { class: 'card' + (wide ? ' wide' : '') },
    h('header', {}, h('h2', {}, chart.title),
      h('div', {}, h('span', { class: 'badge' + (rej ? ' warn' : '') }, `${rej} rows removed`),
        h('button', { onclick: () => openModal(chart, race, meta) }, '⤢ Expand'))),
    await chartBody(chart, race, false));
}

/* =================================================================== 4. EXPAND MODAL */
async function openModal(chart, race, meta) {
  const modal = $('#modal');
  let modalChart = null, tab = 'chart';
  const body = h('div', { class: 'mbody' });
  const tabsEl = h('div', { class: 'mtabs' });
  const close = () => { if (modalChart) modalChart.destroy(); modal.hidden = true; modal.replaceChildren(); document.onkeydown = null; };
  document.onkeydown = e => { if (e.key === 'Escape') close(); };

  const rows = visibleRows(chart);
  async function show(t) {
    tab = t;
    if (modalChart) { modalChart.destroy(); modalChart = null; }
    [...tabsEl.children].forEach(b => b.classList.toggle('on', b.dataset.t === t));
    body.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
    if (t === 'chart') {
      if (chart.status === 'ok' && !['table', 'track'].includes(chart.plot.type) && rows.length) {
        const cv = h('canvas');
        body.replaceChildren(h('div', { class: 'chartbox big' }, cv), chart.plot.type === 'stints' ? stintLegend() : null);
        modalChart = drawChart(cv, chart, rows, race);
      } else body.replaceChildren(await chartBody(chart, race, true));
    } else if (t === 'data') body.replaceChildren(...dataTab(chart, race, rows));
    else if (t === 'method') body.replaceChildren(...methodTab(chart));
    else body.replaceChildren(...await rejectedTab(chart, race, meta));
  }
  [['chart', 'Chart'], ['data', 'Data'], ['method', 'Method'], ['rejected', 'Rejected / excluded']].forEach(([t, l]) =>
    tabsEl.append(h('button', { 'data-t': t, onclick: () => show(t) }, l)));
  modal.replaceChildren(h('div', { class: 'box' },
    h('div', { class: 'mhead' }, h('h2', {}, chart.title), h('button', { onclick: close }, '✕ Close')), tabsEl, body));
  modal.hidden = false;
  flushDraws();
  modal.onclick = e => { if (e.target === modal) close(); };
  show('chart');
}

function download(name, text) {
  const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: 'text/csv' })), download: name });
  document.body.append(a); a.click(); a.remove();
}

/** Data tab: the EXACT rows that were plotted. */
function dataTab(chart, race, rows) {
  let cols = chart.columns;
  if (chart.plot.type === 'track') { rows = trackRows(); }
  return [
    h('div', { class: 'toolbar' }, h('span', { class: 'badge' }, `${rows.length} rows (n = ${rows.length})`),
      h('span', { class: 'muted' }, 'These are the exact rows the chart is drawn from.'),
      h('button', { onclick: () => download(chart.id + '.csv', toCSV(cols, rows)) }, '⬇ Download CSV')),
    tableEl(cols, rows)];
}

/** Method tab: rendered from the method object written by the pipeline (methods.py). */
function methodTab(chart) {
  const m = chart.method, list = (t, a) => a.length ? [h('h3', {}, t), h('ul', {}, a.map(x => h('li', {}, x)))] : [];
  return [h('p', {}, m.summary),
    h('h3', {}, 'Data source'), h('p', {}, m.source.map(s => h('span', { class: 'badge' }, s))),
    h('h3', {}, 'Steps'), h('ol', {}, m.steps.map(s => h('li', {}, s))),
    h('h3', {}, 'Formula'), h('div', { class: 'formula' }, m.formula),
    h('h3', {}, 'Baseline'), h('p', {}, m.baseline),
    ...list('Assumptions', m.assumptions), ...list('Limitations', m.limitations)];
}

/** Rejected tab: counts + every removed row relevant to this chart. */
async function rejectedTab(chart, race, meta) {
  const keys = meta.session_keys;
  const files = await Promise.all(keys.map(k => load(`rejected_${k}.json`)));
  let rows = files.flatMap(f => f.rows).filter(r => chart.tables.includes(r.table));
  rows = rows.concat(chart.dropped);
  const drivers = meta.drivers || [];
  const num = (drivers.find(d => d.code === S.driver) || {}).number;
  if (S.tab === 'driver' && S.driver && num != null) rows = rows.filter(r => r.driver_number === num);
  rows.forEach(r => { r.driver = (drivers.find(d => d.number === r.driver_number) || {}).code || r.driver_number; });

  const counts = chart.tables.map(t => { const q = meta.quality[t] || { received: 0, rejected: 0, excluded: 0, used: 0 };
    return h('tr', {}, h('td', {}, t), [q.received, q.rejected, q.excluded, q.used].map(v => h('td', { class: 'num' }, v)),
      h('td', { class: 'num' }, rate(q.rejected, q.received)), h('td', { class: 'num' }, rate(q.excluded, q.received))); });
  const summary = h('table', {}, h('thead', {}, h('tr', {}, ['Table', 'Received', 'Rejected (validation)', 'Excluded (cleaning)',
    'Used', 'Rejection rate', 'Exclusion rate'].map(x => h('th', {}, x)))), h('tbody', {}, counts));

  const stage = select([['', 'all stages'], ['validation', 'validation'], ['cleaning', 'cleaning'], ['aggregation', 'aggregation']], '', () => draw());
  const rule = select([['', 'all rules'], ...[...new Set(rows.map(r => r.rule))].sort().map(x => [x, x])], '', () => draw());
  const rawBox = h('input', { type: 'checkbox', onchange: () => draw() });
  const out = h('div'), cols = [col('stage', 'Stage'), col('table', 'Table'), col('rule', 'Rule'), col('reason', 'Reason'),
    col('driver', 'Driver'), col('lap_number', 'Lap')];
  let shown = [];
  function draw() {
    shown = rows.filter(r => (!stage.value || r.stage === stage.value) && (!rule.value || r.rule === rule.value));
    const c = rawBox.checked ? [...cols, col('raw', 'Raw record')] : cols;
    out.replaceChildren(shown.length ? tableEl(c, shown, { limit: 500 }) : h('div', { class: 'empty' }, 'Nothing was removed here.'));
  }
  draw();
  return [h('p', { class: 'muted' }, 'Validation = failed a sanity check when loaded. Cleaning = valid but removed from the analysis ' +
      '(out-laps, safety-car laps, slow laps...). Aggregation = an age/team group too small to plot. ' +
      'For every table: received = rejected + excluded + used.'),
    summary, h('div', { class: 'toolbar', style: 'margin-top:12px' }, stage, rule,
      h('label', {}, rawBox, ' show raw records'), h('span', { class: 'badge' }, `${rows.length} rows`),
      h('button', { onclick: () => download(chart.id + '_removed.csv', toCSV([...cols, col('raw', 'Raw record')], shown)) }, '⬇ CSV')),
    out];
}
function col(key, label, unit = '') { return { key, label, unit }; }

/* ===================================================================== 5. TRACK MAP */
/** Rows shown in the Data tab of the track map: the dots currently on screen. */
function trackRows() {
  const st = S.trackState;
  if (!st) return [];
  const { t, i } = st;
  return Object.entries(t.cars).map(([code, c]) => ({ driver: code, x: c.x[i], y: c.y[i], position: c.pos[i],
    time_s: Math.round(t.t_start + i * t.step) })).sort((a, b) => (a.position ?? 99) - (b.position ?? 99));
}

async function trackView(chart, race) {
  if (!race.track_file) return h('div', { class: 'empty' }, 'Track map unavailable for this race (no location data).');
  const t = await load(race.track_file);
  const st = S.trackState = { t, i: 0, race };
  const cv = h('canvas'), order = h('div', { class: 'order' }), label = h('span', { class: 'muted' });
  const slider = h('input', { type: 'range', min: 0, max: t.n - 1, value: 0 });
  const speed = select([['1', 'slow'], ['3', 'normal'], ['8', 'fast'], ['20', 'very fast']], '3', () => {});
  const play = h('button', { class: 'primary' }, '▶ Play');

  // fixed scale from the outline so dots never jump
  const xs = t.outline.map(p => p[0]), ys = t.outline.map(p => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];

  let tries = 0;
  function draw() {
    if (!cv.clientWidth && tries++ < 30) { requestAnimationFrame(draw); return; }   // not attached yet
    const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext('2d'); g.scale(dpr, dpr);
    const pad = 28, sc = Math.min((W - 2 * pad) / ((x1 - x0) || 1), (H - 2 * pad) / ((y1 - y0) || 1));
    const px = x => pad + (x - x0) * sc + (W - 2 * pad - (x1 - x0) * sc) / 2;
    const py = y => H - (pad + (y - y0) * sc + (H - 2 * pad - (y1 - y0) * sc) / 2);   // flip y: up = north
    g.lineJoin = g.lineCap = 'round'; g.strokeStyle = '#3a4150'; g.lineWidth = 9; g.beginPath();
    t.outline.forEach((p, k) => k ? g.lineTo(px(p[0]), py(p[1])) : g.moveTo(px(p[0]), py(p[1]))); g.closePath(); g.stroke();
    g.font = '10px system-ui'; g.textAlign = 'center';
    cv._dots = [];
    for (const [code, c] of Object.entries(t.cars)) {
      if (c.x[st.i] == null) continue;
      const X = px(c.x[st.i]), Y = py(c.y[st.i]);
      g.fillStyle = driverStyle(code, race).color; g.strokeStyle = '#0f1115'; g.lineWidth = 2;
      g.beginPath(); g.arc(X, Y, 6, 0, 7); g.fill(); g.stroke();
      g.fillStyle = '#fff'; g.fillText(code, X, Y - 10); cv._dots.push([code, X, Y]);
    }
    const lap = t.lap_at[st.i];
    label.textContent = `race time ${Math.floor((t.t_start + st.i * t.step) / 60)}:${String(Math.floor((t.t_start + st.i * t.step) % 60)).padStart(2, '0')} · leader on lap ${lap}`;
    order.replaceChildren(h('div', { class: 'muted' }, 'Running order'),
      ...trackRows().map(r => h('div', {}, h('b', {}, r.position ?? '–'),
        h('span', { class: 'chip', style: `background:${driverStyle(r.driver, race).color}` }), r.driver)));
    S.marker = lap;
    S.charts.forEach(ch => { if (ch.options.plugins.marker && ch.options.plugins.marker.on) ch.draw(); });
  }
  slider.oninput = () => { st.i = +slider.value; draw(); };
  play.onclick = () => {
    if (S.timers.length) { S.timers.forEach(clearInterval); S.timers = []; play.textContent = '▶ Play'; return; }
    play.textContent = '⏸ Pause';
    S.timers.push(setInterval(() => {
      st.i = Math.min(t.n - 1, st.i + +speed.value); slider.value = st.i; draw();
      if (st.i >= t.n - 1) { S.timers.forEach(clearInterval); S.timers = []; play.textContent = '▶ Play'; }
    }, 100));
  };
  cv.onclick = e => {                                    // click a dot -> open that driver
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = (cv._dots || []).find(([, X, Y]) => Math.hypot(X - x, Y - y) < 12);
    if (hit) { S.driver = hit[0]; S.tab = 'driver'; render(); }
  };
  const view = h('div', {}, h('div', { class: 'trackgrid' }, cv, order),
    h('div', { class: 'controls' }, play, slider, speed), h('div', {}, label));
  requestAnimationFrame(draw);
  return view;
}

/* ======================================================================== 6. TABS */
async function renderCharts(charts, race, meta, filter) {
  const grid = h('div', { class: 'grid' });
  for (const c of charts.filter(filter)) grid.append(await card(c, race, meta));
  return grid;
}
const needRace = msg => h('div', { class: 'empty' }, msg);

function qualityBadges(q) {
  const l = q.laps;
  return h('p', {}, h('span', { class: 'badge' }, `laps received ${l.received}`),
    h('span', { class: 'badge warn' }, `rejected ${l.rejected} (${rate(l.rejected, l.received)})`),
    h('span', { class: 'badge warn' }, `excluded ${l.excluded} (${rate(l.excluded, l.received)})`),
    h('span', { class: 'badge' }, `used ${l.used}`));
}

async function getRace() {
  const s = sessionInfo();
  return s ? load(s.file) : null;
}
function raceMeta(race) {
  return { quality: race.quality, session_keys: [race.session_key], drivers: race.drivers };
}

async function renderTab(main) {
  const tab = S.tab;
  if (tab === 'overall' || tab === 'season') {
    if (tab === 'season' && !S.year) return main.append(needRace('Pick a year first.'));
    const p = await load(tab === 'overall' ? S.index.overall_file : `season_${S.year}.json`);
    main.append(h('h2', {}, `Pooled analysis: ${p.label}`),
      h('p', { class: 'muted' }, `${p.session_keys.length} race(s) pooled. Pick a Grand Prix above for single-race analysis.`),
      qualityBadges(p.quality),
      await renderCharts(p.charts, null, { quality: p.quality, session_keys: p.session_keys, drivers: null }, () => true));
    return;
  }
  if (tab === 'quality') return renderQuality(main);
  if (tab === 'strategy') return renderStrategy(main);

  const race = await getRace();
  if (!race) return main.append(needRace('Pick a year and a Grand Prix in the bar above.'));
  const meta = raceMeta(race);
  const head = h('div', {}, h('h2', {}, `${race.year} ${race.name}`),
    h('p', { class: 'muted' }, `${race.circuit}, ${race.country} · ${(race.date || '').slice(0, 10)}`), qualityBadges(race.quality));

  if (tab === 'race') {
    main.append(head, await renderCharts(race.charts, race, meta, c => c.scope === 'race' || c.scope === 'both'));
  } else if (tab === 'driver') {
    if (!S.driver) return main.append(head, needRace('Pick a driver in the bar above (or click a dot on the track map).'));
    const d = race.drivers.find(x => x.code === S.driver);
    main.append(head, h('h2', {}, d ? `${d.name} · ${d.team}` : S.driver),
      await renderCharts(race.charts, race, meta, c => ['both', 'driver'].includes(c.scope)));
  } else if (tab === 'track') {
    const byId = id => race.charts.find(c => c.id === id);
    main.append(head, await renderCharts(race.charts, race, meta, c => c.id === 'track'));
    S.marker = null;
    main.append(h('p', { class: 'muted' }, 'The white dashed line on the charts below follows the map’s time slider.'),
      await renderCharts(race.charts, race, meta, c => c.id === 'position_by_lap' || c.id === 'lap_times'));
  }
}

function renderQuality(main) {
  const rows = [], totals = {};
  for (const y of S.index.years) for (const s of sessionsOf(y)) {
    const l = s.quality.laps;
    rows.push({ year: y, race: s.name, received: l.received, rejected: l.rejected, excluded: l.excluded, used: l.used,
      rej_rate: rate(l.rejected, l.received), exc_rate: rate(l.excluded, l.received),
      other: Object.entries(s.quality).filter(([t]) => t !== 'laps').reduce((n, [, q]) => n + q.rejected + q.excluded, 0) });
    for (const [t, q] of Object.entries(s.quality)) {
      const k = `${y}|${t}`; totals[k] ||= { year: y, table: t, received: 0, rejected: 0, excluded: 0, used: 0 };
      for (const f of ['received', 'rejected', 'excluded', 'used']) totals[k][f] += q[f];
    }
  }
  const tot = Object.values(totals).map(t => ({ ...t, rej_rate: rate(t.rejected, t.received), exc_rate: rate(t.excluded, t.received) }));
  main.append(h('h2', {}, 'Data quality'),
    h('p', { class: 'muted' }, 'Rejected = failed validation when loaded. Excluded = valid but removed by a cleaning rule. ' +
      'For every table: received = rejected + excluded + used. Open any chart’s Expand → Rejected tab to see the rows.'),
    h('h3', {}, 'Per season and table'),
    tableEl([col('year', 'Year'), col('table', 'Table'), col('received', 'Received'), col('rejected', 'Rejected'),
      col('excluded', 'Excluded'), col('used', 'Used'), col('rej_rate', 'Rejection rate'), col('exc_rate', 'Exclusion rate')], tot),
    h('h3', {}, 'Per race (laps table)'),
    tableEl([col('year', 'Year'), col('race', 'Grand Prix'), col('received', 'Laps received'), col('rejected', 'Rejected'),
      col('excluded', 'Excluded'), col('used', 'Used'), col('rej_rate', 'Rejection rate'), col('exc_rate', 'Exclusion rate'),
      col('other', 'Other tables removed')], rows));
}

async function renderStrategy(main) {
  let curves, laps = 57, pit = 22, source;
  const race = await getRace();
  const rd = race && race.charts.find(c => c.id === 'degradation' && c.status === 'ok');
  if (rd) {
    curves = makeCurves(rd.rows); source = `${race.year} ${race.name} (measured in this race)`;
    laps = Math.max(...race.charts.find(c => c.id === 'race_result').rows.map(r => r.laps || 0)) || laps;
    const pr = race.charts.find(c => c.id === 'pit_stops').rows.map(r => r.duration).sort((a, b) => a - b);
    if (pr.length) pit = pr[Math.floor(pr.length / 2)];
  } else {
    const o = await load(S.index.overall_file), pd = o.charts.find(c => c.id === 'p_degradation');
    if (!pd || pd.status !== 'ok') return main.append(needRace('Not enough degradation data to simulate.'));
    curves = makeCurves(pd.rows); source = 'all seasons pooled (select a race to use that race instead)';
    const pt = o.charts.find(c => c.id === 'p_pit_team').rows.map(r => r.median_pit).sort((a, b) => a - b);
    if (pt.length) pit = pt[Math.floor(pt.length / 2)];
  }
  const lapsIn = h('input', { type: 'number', value: Math.round(laps), min: 20, max: 80, style: 'width:80px' });
  const pitIn = h('input', { type: 'number', value: pit.toFixed(1), step: 0.5, style: 'width:80px' });
  const out = h('div');
  const run = () => {
    const res = simulate(curves, +lapsIn.value, +pitIn.value);
    if (!res.length) return out.replaceChildren(needRace('Need at least two dry compounds with usable degradation curves.'));
    const best = res[0].loss;
    out.replaceChildren(tableEl([col('rank', '#'), col('plan', 'Tyres'), col('stops', 'Stop laps'), col('stopsN', 'Stops'),
      col('gap', 'Time lost vs best', 's')],
      res.slice(0, 10).map((r, i) => ({ rank: i + 1, plan: r.tyres.join(' → '), stops: r.stops.join(', '),
        stopsN: r.stops.length, gap: +(r.loss - best).toFixed(2) }))));
  };
  main.append(h('h2', {}, 'Strategy lab'),
    h('p', { class: 'muted' }, `Degradation curves from: ${source}. Time lost = tyre-wear delta summed over every lap + pit-lane time per stop; ` +
      'base pace is the same for every plan, so only the differences matter. Straight-line extrapolation beyond the measured ages. ' +
      'It ignores traffic, safety cars and track evolution — a guide, not a prediction.'),
    h('div', { class: 'toolbar' }, 'Race laps', lapsIn, 'Pit-lane time (s)', pitIn, h('button', { class: 'primary', onclick: run }, 'Simulate')), out);
  run();
}

/* ===================================================== selectors, tabs and start-up */
function renderBar() {
  const sel = $('#selectors'), idx = S.index;
  const races = S.year ? sessionsOf(S.year) : [];
  const drivers = (sessionInfo() || { drivers: [] }).drivers;
  sel.replaceChildren(
    select([['', 'Year…'], ...idx.years.map(y => [y, y])], S.year, v => {
      S.year = v ? +v : null; S.key = null; S.driver = null;
      if (S.year && ['race', 'driver', 'track'].includes(S.tab)) S.key = sessionsOf(S.year)[0]?.session_key ?? null;
      render();
    }),
    select([['', 'Grand Prix…'], ...races.map(s => [s.session_key, s.name])], S.key, v => {
      S.key = v ? +v : null;
      if (S.key && !(sessionInfo().drivers.some(d => d.code === S.driver))) S.driver = null;   // driver not in this race
      if (S.key && ['overall', 'season'].includes(S.tab)) S.tab = 'race';
      render();
    }),
    select([['', 'Driver…'], ...drivers.map(d => [d.code, `${d.code} · ${d.name}`])], S.driver, v => { S.driver = v || null; if (S.driver && ['overall', 'season', 'race'].includes(S.tab)) S.tab = 'driver'; render(); }));
  $('#tabs').replaceChildren(...TABS.map(([id, label]) =>
    h('button', { class: S.tab === id ? 'on' : '', onclick: () => { S.tab = id; render(); } }, label)));
}

async function render() {
  S.charts.forEach(c => c.destroy()); S.charts = [];
  S.timers.forEach(clearInterval); S.timers = []; S.trackState = null; S.marker = null; drawQueue.length = 0;
  if (S.tab === 'driver' && S.driver && !S.key && S.year) S.key = sessionsOf(S.year)[0]?.session_key ?? null;
  writeHash(); renderBar();
  const main = $('#main');
  main.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  const fresh = h('div');
  try {
    await renderTab(fresh);
    main.replaceChildren(fresh);
    flushDraws();
  } catch (e) {
    console.error(e);
    main.replaceChildren(h('div', { class: 'empty' }, `Could not load this view: ${e.message}. `,
      h('button', { onclick: render }, 'Retry')));
  }
}

async function start() {
  const main = $('#main');
  try {
    S.index = await load('index.json');
    if (S.index.schema_version !== 2) throw new Error('data files are from a different version - re-run python pipeline/eda.py');
  } catch (e) {
    main.replaceChildren(h('div', { class: 'empty' }, 'No data found (', e.message, '). Run ',
      h('code', {}, 'python pipeline/demo.py'), ' for sample data, or ', h('code', {}, 'python pipeline/etl.py && python pipeline/eda.py'),
      ' for real data, and serve this folder over HTTP (not file://).'));
    return;
  }
  indexTeamColours();
  readHash();
  if (!S.year && S.index.years.length) S.year = S.index.years[S.index.years.length - 1];
  window.addEventListener('hashchange', () => { readHash(); render(); });
  render();
}
start();
}   // end of browser-only section
