/* Run with: node --test tests/frontend/l1_quality.test.cjs */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function controller() {
  const elements = new Map(), stored = new Map();
  class Element {
    constructor() {
      this.children = []; this.dataset = {}; this.style = {}; this.hidden = false;
      this.disabled = false; this.textContent = ''; this.listeners = {}; this.plotListeners = {};
      this.firstChild = { textContent: '' };
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set value(value) { this._value = value == null ? '' : String(value); }
    get value() { return this._value || ''; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.value = ''; }
    replaceWith() {}
    addEventListener(type, action) { (this.listeners[type] ||= []).push(action); }
    setAttribute() {}
    after() {}
    on(type, action) { this.plotListeners[type] = action; }
    removeAllListeners(type) { delete this.plotListeners[type]; }
    matches() { return false; }
    querySelector(selector) { return element(`child:${this.id}:${selector}`); }
    querySelectorAll() { return []; }
    get options() { return this.children; }
    get rows() { return this.children; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() {}
  }
  function element(id) {
    if (!elements.has(id)) { const item = new Element(); item.id = id; elements.set(id, item); }
    return elements.get(id);
  }
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], querySelector: element, body: new Element() },
    window: { addEventListener() {} },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetch: () => new Promise(() => {}),
    localStorage: { getItem(key) { return stored.get(key) ?? null; }, setItem(key, value) { stored.set(key, String(value)); } },
    setTimeout, clearTimeout, requestAnimationFrame() {}, console, deferred,
  });
  vm.runInContext(readFileSync(path.resolve(__dirname, '../../workbench/static/workbench.js'), 'utf8'), context);
  vm.runInContext(`
    state.run = { id:'quality-run', payload:'10B', dataset:{profile:'10B'}, inventory:{}, config:{}, jobs:[] };
    state.layer = 'TB_L1'; state.channel = 0; state.busy = false; state.loading = false;
    state.loadToken = 4; state.spectrumToken = 0; state.dirty = false;
    renderUnpackWorkspace = () => {}; setBusy = () => {}; renderCurrentStatus = () => {};
    chartMessage = () => {}; messageError = error => { state.testError = error.message; };
    const selected = [];
    selectPoint = async point => { state.point = point; selected.push(point); };
    const plotCalls = [];
    const plotly = {
      async react(chart, traces, layout) { chart.data = traces; chart.layout = layout; plotCalls.push({traces,layout}); },
      purge(chart) { delete chart.data; },
      Plots:{resize() {}}, relayout:async()=>{}, restyle:async()=>{}
    };
    window.Plotly = plotly; globalThis.Plotly = plotly;
    function row(stem, channel, values={}) {
      return { id:'10B:'+stem+':'+channel, stem, channel, temp:channel, bias:28+channel/10,
        fit_status:'fitted', quality_accepted:true, chi2_reduced:1.25,
        rejection_reasons:[], ...values };
    }
    function file(stem, values={}) {
      return {stem, has_l0:true, channels:[row(stem,2),row(stem,0,values),row(stem,3,{fit_status:'model_pending',quality_accepted:null,chi2_reduced:null}),row(stem,1)]};
    }
    state.l1Overview = {files:[
      file('accepted',{temp:0,bias:28.17}),
      file('rejected',{temp:-15.25,bias:26.912,fit_status:'rejected',quality_accepted:false,chi2_reduced:12.5,
        fit_quality:{accepted:false,delta_bic_background:24.375,amplitude_snr:8.125},rejection_reasons:['structured_residual']}),
      file('failed',{fit_status:'failed',error:'fit did not converge'}),
      file('model',{fit_status:'model_pending',quality_accepted:null,chi2_reduced:null}),
      file('unfitted',{fit_status:'not_fitted',quality_accepted:null,chi2_reduced:null}),
      file('unknown',{fit_status:'fitted',quality_accepted:null,chi2_reduced:null}),
      file('no-metric',{chi2_reduced:null}),
      file('no-coordinate',{temp:null})
    ]};
  `, context);
  return { run: source => vm.runInContext(source, context), element };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

test('quality points map each file to the requested physical channel even when rows are unordered', () => {
  const ui = controller();
  const points = plain(ui.run('l1QualityPoints(1)'));
  assert.equal(points.length, 8);
  assert.equal(new Set(points.map(item => item.id)).size, 8);
  for (const point of points) {
    assert.equal(point.channel, 1);
    assert.equal(point.id, `10B:${point.stem}:1`);
    assert.equal(point.temp, 1);
    assert.equal(point.bias, 28.1);
  }
  assert.equal(ui.run('l1QualityPoints(0)[0].temp'), 0);
  assert.equal(ui.run('l1QualityPoints(0)[1].temp'), -15.25);
  assert.equal(ui.run('l1QualityPoints(0)[1].bias'), 26.912);
});

test('acceptance, rejection, failed calculation, pending model and unknown quality remain separate', () => {
  const ui = controller();
  assert.deepEqual(plain(ui.run('l1QualityPoints().map(l1QualityStatus)')), [
    'accepted', 'rejected', 'failed', 'model_pending', 'not_fitted', 'quality_unknown', 'accepted', 'accepted',
  ]);
  const summary = plain(ui.run('l1QualitySummary(l1QualityPoints())'));
  assert.equal(summary.total, 8);
  assert.deepEqual(summary.counts, { accepted:3, rejected:1, failed:1, model_pending:1, not_fitted:1, quality_unknown:1 });
  assert.equal(summary.missingCoordinates, 1);
  assert.equal(summary.missingMetrics, 4);
});

test('missing metrics stay grey and missing coordinates never become zero-valued map points', () => {
  const ui = controller();
  const traces = plain(ui.run('l1QualityTraces(l1QualityPoints())'));
  const plotted = traces.flatMap(trace => trace.customdata || []);
  assert.equal(plotted.length, 7);
  assert.equal(new Set(plotted.map(item => item.id)).size, 7);
  assert.equal(plotted.some(item => item.stem === 'no-coordinate'), false);
  const grey = traces.find(trace => trace.customdata?.some(item => item.stem === 'no-metric'));
  assert.ok(grey);
  assert.equal(grey.marker.coloraxis, undefined);
  assert.equal(typeof grey.marker.color, 'string');
  const coloured = traces.filter(trace => trace.marker?.coloraxis === 'coloraxis');
  assert.ok(coloured.length);
  assert.ok(coloured.every(trace => trace.marker.color.every(Number.isFinite)));
  ui.run(`state.l1Overview.files[6].channels.find(item=>item.channel===0).chi2_reduced=0;`);
  const withZero = plain(ui.run('l1QualityTraces(l1QualityPoints())'));
  const zeroTrace = withZero.find(trace => trace.customdata?.some(item => item.stem === 'no-metric'));
  const zeroIndex = zeroTrace.customdata.findIndex(item => item.stem === 'no-metric');
  assert.equal(zeroTrace.marker.coloraxis, 'coloraxis');
  assert.equal(zeroTrace.marker.color[zeroIndex], 0);
});

test('all plotted points retain measured conditions, identity, state symbols and rejection evidence', () => {
  const ui = controller();
  const points = plain(ui.run('l1QualityPoints()'));
  const lookup = new Map(points.map(point => [point.id, point]));
  const traces = plain(ui.run('l1QualityTraces(l1QualityPoints())'));
  const symbols = { accepted:'circle', rejected:'diamond', failed:'x', model:'square-open', unfitted:'circle-open', unknown:'triangle-up-open', 'no-metric':'circle' };
  for (const trace of traces) {
    for (const [index, identity] of (trace.customdata || []).entries()) {
      const point = lookup.get(identity.id);
      assert.ok(point);
      assert.equal(identity.stem, point.stem);
      assert.equal(identity.channel, point.channel);
      assert.equal(trace.x[index], point.temp);
      assert.equal(trace.y[index], point.bias);
      assert.equal(trace.marker.symbol, symbols[point.stem]);
    }
  }
  const rejected = traces.find(trace => trace.customdata?.some(item => item.stem === 'rejected'));
  assert.match(JSON.stringify(rejected), /structured_residual/);
  assert.match(JSON.stringify(rejected), /12\.5/);
  assert.match(JSON.stringify(rejected), /ΔBIC 24\.375/);
  assert.match(JSON.stringify(rejected), /峰面积 SNR 8\.125/);
});

test('click identity selects the exact file and channel and cannot fall back to a similar stem', async () => {
  const ui = controller();
  const context = ui.run('l1QualityContext()');
  await ui.run(`selectL1QualityPoint({id:'10B:rejected:0',stem:'rejected',channel:0},${JSON.stringify(context)})`);
  assert.equal(ui.run('selected.length'), 1);
  assert.equal(ui.run('state.point.stem'), 'rejected');
  assert.equal(ui.run('state.point.channel'), 0);
  await ui.run(`selectL1QualityPoint({id:'10B:rejected:1',stem:'rejected',channel:0},${JSON.stringify(context)})`);
  await ui.run(`selectL1QualityPoint({id:'10B:accepted:0',stem:'rejected',channel:0},${JSON.stringify(context)})`);
  await ui.run(`selectL1QualityPoint({id:'10B:rejected:0',stem:'rejected-extra',channel:0},${JSON.stringify(context)})`);
  assert.equal(ui.run('selected.length'), 1);
});

test('map clicks captured before channel, run, layer or load changes cannot select a stale point', async () => {
  for (const change of ["state.channel=1", "state.run={...state.run,id:'other-run'}", "state.layer='TB_L2'", '++state.loadToken']) {
    const ui = controller();
    const context = ui.run('l1QualityContext()');
    ui.run(change);
    await ui.run(`selectL1QualityPoint({id:'10B:accepted:0',stem:'accepted',channel:0},${JSON.stringify(context)})`);
    assert.equal(ui.run('selected.length'), 0, change);
  }
});

test('switching channels preserves the selected file and computes independent status counts', async () => {
  const ui = controller();
  ui.run(`state.point=l1QualityPoints(0).find(point=>point.stem==='rejected'); state.surface={points:l1QualityPoints(0)};`);
  await ui.run('selectChannel(1)');
  assert.equal(ui.run('state.channel'), 1);
  assert.equal(ui.run('state.point.stem'), 'rejected');
  assert.equal(ui.run('state.point.id'), '10B:rejected:1');
  assert.equal(ui.run('l1QualitySummary(l1QualityPoints()).counts.accepted'), 8);
  await ui.run('selectChannel(3)');
  assert.equal(ui.run('state.point.stem'), 'rejected');
  assert.equal(ui.run('state.point.channel'), 3);
  assert.equal(ui.run('l1QualitySummary(l1QualityPoints()).counts.model_pending'), 8);
  assert.equal(ui.run('l1QualitySummary(l1QualityPoints()).counts.accepted'), 0);
});

test('a render finishing after a channel switch cannot install a usable stale click handler', async () => {
  const ui = controller();
  ui.run(`
    const entered=deferred(), release=deferred();
    plotly.react=async (chart,traces,layout)=>{chart.data=traces;chart.layout=layout;entered.resolve();await release.promise;};
    const pending=renderL1QualityOverview();
  `);
  await ui.run('entered.promise');
  ui.run('state.channel=1; ++state.loadToken; release.resolve();');
  await ui.run('pending');
  const handler = ui.element('l1-quality-chart').plotListeners.plotly_click;
  if (handler) await handler({ points:[{customdata:{id:'10B:accepted:0',stem:'accepted',channel:0}}] });
  assert.equal(ui.run('selected.length'), 0);
});

test('quality rendering failures from a previous run, layer, channel or load do not reach the current view', async () => {
  for (const change of ["state.run={...state.run,id:'other-run'}", "state.layer='EC_L2'", 'state.channel=1', '++state.loadToken']) {
    const ui = controller();
    ui.run(`
      const entered=deferred(), release=deferred();
      plotly.react=async()=>{entered.resolve();await release.promise;};
      const pending=renderL1QualityOverview().catch(messageError);
    `);
    await ui.run('entered.promise');
    ui.run(`${change}; release.reject(new Error('obsolete quality plot'));`);
    await ui.run('pending');
    assert.equal(ui.run('state.testError'), undefined, change);
  }
});

test('a superseded quality render cannot report its failure over the newer render in the same view', async () => {
  const ui = controller();
  ui.run(`
    const entered=deferred(), release=deferred(), originalReact=plotly.react;
    let renders=0;
    plotly.react=async(...args)=>{if (++renders===1) {entered.resolve();await release.promise;} return originalReact(...args);};
    const oldRender=renderL1QualityOverview().catch(messageError);
  `);
  await ui.run('entered.promise');
  ui.run(`const newRender=renderL1QualityOverview().catch(messageError); release.reject(new Error('superseded quality plot'));`);
  await ui.run('Promise.all([oldRender,newRender])');
  assert.equal(ui.run('state.testError'), undefined);
  assert.equal(ui.run('renders'), 2);
  assert.equal(ui.run('plotCalls.length'), 1);
  assert.equal(typeof ui.element('l1-quality-chart').plotListeners.plotly_click, 'function');
});

test('the current quality render reports its error and a subsequent retry can render normally', async () => {
  const ui = controller();
  ui.run(`
    const entered=deferred(), release=deferred(), originalReact=plotly.react;
    plotly.react=async()=>{entered.resolve();await release.promise;};
    const pending=renderL1QualityOverview().catch(messageError);
  `);
  await ui.run('entered.promise');
  ui.run(`release.reject(new Error('current quality plot failed'));`);
  await ui.run('pending');
  assert.equal(ui.run('state.testError'), 'current quality plot failed');
  ui.run('plotly.react=originalReact;');
  await ui.run('renderL1QualityOverview()');
  assert.equal(ui.run('plotCalls.length'), 1);
  assert.equal(typeof ui.element('l1-quality-chart').plotListeners.plotly_click, 'function');
});

test('a new run hides the previous quality map while its overview loads and stays hidden on failure', async () => {
  const ui = controller();
  await ui.run('renderL1QualityOverview()');
  assert.equal(ui.element('l1-quality-overview').hidden, false);
  assert.equal(ui.run('plotCalls.length'), 1);
  ui.run(`
    const oldClick=$('l1-quality-chart').plotListeners.plotly_click;
    const response=deferred();
    api=()=>response.promise;
    const staleRender=renderL1QualityOverview();
    state.run={...state.run,id:'new-run'};
    const loading=loadSurfaceSafely();
  `);
  assert.equal(ui.run('state.l1Overview'), null);
  assert.equal(ui.element('l1-quality-overview').hidden, true);
  await ui.run('staleRender');
  assert.equal(ui.run('plotCalls.length'), 1, 'queued old-run rendering must be discarded');
  await ui.run("oldClick({points:[{customdata:{id:'10B:accepted:0',stem:'accepted',channel:0}}]})");
  assert.equal(ui.run('selected.length'), 0);
  ui.run("response.reject(new Error('new overview unavailable'));");
  await ui.run('loading');
  assert.equal(ui.run('state.testError'), 'new overview unavailable');
  assert.equal(ui.run('state.l1Overview'), null);
  assert.equal(ui.element('l1-quality-overview').hidden, true);
  ui.run(`$('run-scope').append({textContent:''}); configureLayerPanels();`);
  assert.equal(ui.element('l1-quality-overview').hidden, true, 'panel configuration cannot expose the stale map');
  await ui.run("oldClick({points:[{customdata:{id:'10B:accepted:0',stem:'accepted',channel:0}}]})");
  assert.equal(ui.run('selected.length'), 0);
  assert.equal(ui.run('plotCalls.length'), 1);
});
