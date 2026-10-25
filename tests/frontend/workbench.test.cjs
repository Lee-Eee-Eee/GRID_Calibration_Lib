/* Run with: node --test tests/frontend/workbench.test.cjs
 * Exercise the shipped UI controller with deferred network/render operations.
 * Browser layout and actual processor operation are verified separately.
 */
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

function controller({ absentSelectors = [] } = {}) {
  const elements = new Map();
  const stored = new Map();
  class Element {
    constructor() {
      this.value = ''; this.children = []; this.dataset = {}; this.style = {};
      this.hidden = false; this.disabled = false; this.textContent = '';
      this.firstChild = { textContent: '' }; this.listeners = {};
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    append(...items) { this.children.push(...items); }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set value(value) { this._value = value == null ? '' : String(value); }
    get value() { return this._value; }
    replaceChildren(...items) { this.children = items; this.value = ''; }
    replaceWith() {}
    addEventListener(type, action) { (this.listeners[type] ||= []).push(action); }
    setAttribute() {}
    after() {}
    on() {}
    removeAllListeners() {}
    matches() { return false; }
    querySelector(selector) { return element(`child:${this.id}:${selector}`); }
    querySelectorAll() { return []; }
    get options() { return this.children; }
    get rows() { return this.children; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { this.focused = true; }
  }
  function element(id) {
    if (!elements.has(id)) { const item = new Element(); item.id = id; elements.set(id, item); }
    return elements.get(id);
  }
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], querySelector: selector => absentSelectors.includes(selector) ? null : element(selector), body: new Element() },
    window: { addEventListener() {} },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetch: () => new Promise(() => {}), // Bootstrap remains pending; every test supplies explicit data.
    localStorage: { getItem(key) { return stored.get(key) ?? null; }, setItem(key, value) { stored.set(key, String(value)); } },
    setTimeout, clearTimeout, requestAnimationFrame() {}, console, deferred,
  });
  vm.runInContext(readFileSync(path.resolve(__dirname, '../../workbench/static/workbench.js'), 'utf8'), context);
  vm.runInContext(`
    state.run = { id: 'run-a', payload: '14B', dataset: { raw_dir: 'raw-a' }, inventory: {}, config: {}, jobs: [] };
    state.layer = 'TB_L0'; state.channel = 0; state.busy = false; state.loading = false;
    state.loadToken = 0; state.spectrumToken = 0; state.dirty = false;
    const productionRenderUnpackWorkspace = renderUnpackWorkspace;
    const productionSetBusy = setBusy;
    renderUnpackWorkspace = () => {}; setBusy = () => {};
    chartMessage = () => {}; messageError = error => { state.testError = error.message; };
  `, context);
  return { run: source => vm.runInContext(source, context), element };
}

test('the controller starts without decorative pipeline help paragraphs', () => {
  assert.doesNotThrow(() => controller({ absentSelectors: ['.pipeline-menu p'] }));
});

test('TB layer switch while rendering cannot select a point from the old layer', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    const rendered = deferred(), release = deferred();
    let selected = [];
    api = async () => ({ points: [{ id: 'old-point', stem: 'old-file' }] });
    renderSurface = async () => { rendered.resolve(); await release.promise; };
    selectPoint = async point => selected.push(point.id);
    const pending = loadSurface();
  `);
  await ui.run('rendered.promise');
  ui.run(`state.layer = 'EC_L0'; ++state.loadToken; release.resolve();`);
  await ui.run('pending');
  assert.equal(ui.run('selected.length'), 0);
});

test('EC layer switch while rendering cannot select an old energy point', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'EC_L2';
    const rendered = deferred(), release = deferred();
    let selected = [];
    api = async () => ({ points: [{ id: 'old-energy', stem: 'old-file', E: 50 }] });
    renderEcOverview = async () => { rendered.resolve(); await release.promise; };
    selectEcPoint = async point => selected.push(point.id);
    const pending = loadEcAnalysis();
  `);
  await ui.run('rendered.promise');
  ui.run(`state.layer = 'TB_L0'; ++state.loadToken; release.resolve();`);
  await ui.run('pending');
  assert.equal(ui.run('selected.length'), 0);
});

test('reversed channel responses retain the latest channel and file', async () => {
  const ui = controller();
  ui.run(`
    const responses = [deferred(), deferred()]; let requests = 0, selected = [];
    api = () => responses[requests++].promise;
    renderSurface = async () => {};
    selectPoint = async point => selected.push(point.id);
    const first = loadSurface();
    state.channel = 3; ++state.loadToken;
    const second = loadSurface();
    responses[1].resolve({ files: [{ stem: 'ch3-file' }] });
  `);
  await ui.run('second');
  ui.run(`responses[0].resolve({ files: [{ stem: 'old-ch0-file' }] });`);
  await ui.run('first');
  assert.equal(ui.run('state.surface.points[0].channel'), 3);
  assert.equal(ui.run('selected.join()'), '14B:ch3-file:3');
});

test('a surface refresh finishing after an explicit file selection preserves the new file', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    const points = [{ id: '14B:094:0', stem: '094' }, { id: '14B:101:0', stem: '101' }];
    state.point = points[0];
    const entered = deferred(), release = deferred();
    api = async path => path.includes('/l1-overview') ? { points } : { x: [0, 100], y: [1, 20], histogram_config: { min: 0, max: 100 } };
    renderSurface = async () => { entered.resolve(); await release.promise; };
    highlightSelectedPoint = async () => {};
    renderSpectrum = async () => {};
    renderCurrentStatus = () => {};
    const pending = loadSurface();
  `);
  await ui.run('entered.promise');
  await ui.run('selectPoint(points[1])');
  ui.run('release.resolve()');
  await ui.run('pending');
  assert.equal(ui.run('state.point.stem'), '101');
  assert.equal(ui.element('point-select').value, '14B:101:0');
  assert.equal(ui.run('savedView().familyStems.TB'), '101');
});

test('an earlier file switch cannot overwrite a later file switch when window saves finish out of order', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.dirty = true;
    state.point = { id: '14B:094:0', stem: '094' };
    const saves = [deferred(), deferred()]; let saveCount = 0;
    saveRevision = async () => { await saves[saveCount++].promise; };
    api = async () => ({ x: [0, 100], y: [1, 20], histogram_config: { min: 0, max: 100 } });
    highlightSelectedPoint = async () => {};
    renderSpectrum = async () => {};
    renderCurrentStatus = () => {};
    const earlier = selectPoint({ id: '14B:101:0', stem: '101' });
    const latest = selectPoint({ id: '14B:102:0', stem: '102' });
    saves[1].resolve();
  `);
  // Let a later selection's save return before the earlier selection's save.
  await Promise.resolve();
  ui.run('saves[0].resolve()');
  await ui.run('Promise.all([earlier, latest])');
  assert.equal(ui.run('state.point.stem'), '102');
  assert.equal(ui.element('point-select').value, '14B:102:0');
  assert.equal(ui.run('savedView().familyStems.TB'), '102');
});

test('fit controls are disabled immediately while a file switch saves the previous window', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.dirty = true;
    state.point = { id: '14B:094:0', stem: '094' };
    state.spectrum = { x: [0, 100], y: [1, 20] };
    const saved = deferred();
    saveRevision = async () => { await saved.promise; };
    setBusy = productionSetBusy;
    api = async () => ({ x: [0, 100], y: [1, 20], histogram_config: { min: 0, max: 100 } });
    highlightSelectedPoint = async () => {};
    renderSpectrum = async () => {}; renderCurrentStatus = () => {};
    const pending = selectPoint({ id: '14B:101:0', stem: '101' });
  `);
  assert.equal(ui.element('fit-spectrum').disabled, true);
  assert.equal(ui.element('auto-fit-spectrum').disabled, true);
  assert.equal(ui.element('window-lo').disabled, true);
  assert.equal(ui.element('window-hi').disabled, true);
  ui.run('saved.resolve()');
  await ui.run('pending');
  assert.equal(ui.run('state.point.stem'), '101');
});

test('fit submission keeps the file and channel whose window was saved', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.channel = 0;
    state.point = { id: '14B:101:0', stem: '101' };
    state.spectrum = { x: [0, 100], y: [1, 20] };
    $('window-lo').value = '20'; $('window-hi').value = '80';
    const saved = deferred(); let submitted, patch;
    saveRevision = async extra => { patch = extra; await saved.promise; };
    executeJob = async (layer, config) => { submitted = { layer, ...config }; };
    notify = () => {};
    const pending = fitSpectrum();
    state.channel = 3; state.point = { id: '14B:102:3', stem: '102' };
    saved.resolve();
  `);
  await ui.run('pending');
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(patch)')), { windows: { '101': { '0': { lo: 20, hi: 80 } } } });
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), { layer: 'TB_L1', stem: '101', channel: 0, fit_channels: [0], automatic_window: false });
});

test('the main layer action keeps its original layer, file, channel and scope while saving', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.channel = 0; state.dirty = true;
    state.point = { id: '14B:101:0', stem: '101' }; $('run-scope').value = 'representative';
    state.spectrum = {x:[0,100],y:[1,20]}; $('window-lo').value='20'; $('window-hi').value='80';
    const saved = deferred(); let submitted, busyStates = [];
    saveRevision = async () => { await saved.promise; };
    setBusy = value => { busyStates.push(value); };
    executeJob = async (layer, config) => { submitted = { layer, ...config }; };
  `);
  const pending = ui.element('run-layer').listeners.click[0]({});
  assert.equal(ui.run('busyStates[0]'), true);
  ui.run(`
    state.layer = 'EC_L0'; state.channel = 3; state.point = { id: 'other:3', stem: 'other' };
    $('run-scope').value = 'full'; saved.resolve();
  `);
  await pending;
  assert.equal(ui.run('submitted.layer'), 'TB_L1');
  assert.equal(ui.run('submitted.stem'), '101');
  assert.equal(ui.run('submitted.channel'), 0);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(busyStates)')), [true, false]);
});

test('file fitting cannot submit the previous trial file after another trial has opened', async () => {
  for (const action of ['fitSpectrum()', 'fitAllL1Channels()']) {
    const ui = controller();
    ui.run(`
      state.layer = 'TB_L1'; state.channel = 0; state.dirty = true;
      state.point = { id: '14B:101:0', stem: '101' };
      state.spectrum = { x: [0, 100], y: [1, 20] };
      $('window-lo').value = '20'; $('window-hi').value = '80';
      const saved = deferred(); let submissions = 0;
      saveRevision = async () => { await saved.promise; };
      executeJob = async () => { submissions++; };
      notify = () => {};
    `);
    ui.run(`const pending = ${action}; state.run = { id:'run-b', payload:'15B' }; saved.resolve();`);
    await assert.rejects(ui.run('pending'), /试算已切换/);
    assert.equal(ui.run('submissions'), 0, action);
  }
});

test('L1 file search and review filters select only matching conditions', () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    state.l1Overview = { files: [
      { stem: '094_observe_50C_290', has_l0: true, status: 'fitted', fitted: 4, channels: [] },
      { stem: '101_observe_50C_285', has_l0: true, status: 'attention', fitted: 3, channels: [] },
      { stem: '102_observe_40C_285', has_l0: true, status: 'attention', fitted: 0, channels: [] },
      { stem: '103_observe_30C_285', has_l0: true, status: 'pending', fitted: 0, channels: [] },
      { stem: 'history_without_l0', has_l0: false, status: 'fitted', fitted: 4, channels: [] }
    ] };
    $('l1-file-filter').value = 'all';
  `);
  assert.equal(ui.run('filteredL1Files().length'), 4);
  ui.element('l1-file-filter').value = 'attention';
  assert.equal(ui.run('filteredL1Files().map(item => item.stem).join()'), '101_observe_50C_285,102_observe_40C_285');
  ui.element('l1-file-search').value = '50c';
  assert.equal(ui.run('filteredL1Files().map(item => item.stem).join()'), '101_observe_50C_285');
  ui.element('l1-file-filter').value = 'fitted';
  assert.equal(ui.run('filteredL1Files().map(item => item.stem).join()'), '094_observe_50C_290');
  ui.element('l1-file-search').value = '';
  ui.element('l1-file-filter').value = 'pending';
  assert.equal(ui.run('filteredL1Files().map(item => item.stem).join()'), '103_observe_30C_285');
});

test('L1 previous and next navigate the filtered file list without wrapping at the boundary', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    state.l1Overview = { files: [
      { stem: '094', has_l0: true, status: 'fitted', channels: [{ id: '14B:094:0', stem: '094', channel: 0 }] },
      { stem: '101', has_l0: true, status: 'attention', channels: [{ id: '14B:101:0', stem: '101', channel: 0 }] },
      { stem: '102', has_l0: true, status: 'attention', channels: [{ id: '14B:102:0', stem: '102', channel: 0 }] }
    ] };
    state.surface = { points: state.l1Overview.files.flatMap(file => file.channels) };
    state.point = state.surface.points[1];
    $('l1-file-filter').value = 'attention';
    let selected = [];
    selectPoint = async point => { state.point = point; selected.push(point.stem); };
  `);
  await ui.run('moveL1File(1)');
  assert.equal(ui.run('state.point.stem'), '102');
  await ui.run('moveL1File(1)');
  assert.equal(ui.run('state.point.stem'), '102');
  await ui.run('moveL1File(-1)');
  assert.equal(ui.run('state.point.stem'), '101');
  await ui.run('moveL1File(-1)');
  assert.equal(ui.run('state.point.stem'), '101');
  assert.equal(ui.run('selected.join()'), '102,101');
});

test('switching an L1 channel keeps the selected condition file', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.channel = 0; state.point = { id: '14B:101:0', stem: '101', channel: 0 };
    renderNavigation = () => {}; renderStage = () => {}; renderSurface = async () => {};
    api = async () => ({ points: [{ id: '14B:094:3', stem: '094', channel: 3 }, { id: '14B:101:3', stem: '101', channel: 3 }] });
    selectPoint = async point => { state.point = point; rememberView(); };
  `);
  await ui.run('selectChannel(3)');
  assert.equal(ui.run('state.point.stem'), '101');
  assert.equal(ui.run('state.point.channel'), 3);
  assert.equal(ui.run('state.channel'), 3);
});

test('a partial L1 job refreshes its usable results and reports successful and failed channels', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    const partial = { id: 'partial-l1', layer: 'TB_L1', status: 'partial',
      result: { summary: { files_total: 2, channels_succeeded: 7, channels_failed: 1 },
        counts: { outputs: 4, processed: 2, files: 2, channels_fitted: 7, channels_failed: 1 } } };
    api = async () => ({ job: partial });
    let refreshed = 0, notices = [];
    refreshRun = async () => { refreshed++; };
    renderJobs = () => {}; renderNavigation = () => {}; renderStage = () => {}; showTab = () => {};
    notify = (message, type) => { notices.push({ message, type }); };
  `);
  const job = await ui.run(`executeJob('TB_L1', { representative: false })`);
  assert.equal(job.status, 'partial');
  assert.equal(ui.run('refreshed'), 1);
  assert.equal(ui.run('state.run.jobs[0].status'), 'partial');
  const completion = JSON.parse(ui.run('JSON.stringify(notices.at(-1))'));
  assert.match(completion.message, /7/);
  assert.match(completion.message, /1/);
  assert.notEqual(completion.type, 'success');
  assert.notEqual(completion.type, 'error');
});

test('L1 progress flags suspect models without describing them as failed fits', () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    renderL1Progress({ status: 'succeeded', config: {}, result: {
      summary: { channels_succeeded: 192, channels_failed: 0 },
      processor: { channels_accepted: 179, channels_rejected: 13 }
    }});
  `);
  assert.match(ui.run('$("l1-progress").textContent'), /192 通道已拟合 · 179 未见异常 · 13 疑似问题/);
  assert.doesNotMatch(ui.run('$("l1-progress").textContent'), /拒绝|计算失败/);
});

test('fit warnings retain a successful completion and EC participation follows explicit usage', () => {
  const ui = controller();
  ui.run(`
    state.spectrum={fit:{fit_quality:{accepted:false,status:'suspect',warning_only:true}}};
    fitCompletionFeedback({status:'succeeded'},false,1);
  `);
  assert.equal(ui.element('fit-result-status').dataset.status, 'attention');
  assert.match(ui.element('fit-result-status').textContent, /拟合完成.*疑似问题.*保留/);
  assert.doesNotMatch(ui.element('fit-result-status').textContent, /失败|拒绝/);
  assert.match(ui.run(`ecPointStatus({id:'a',used:true,quality_reasons:['R² < 0.7']})`), /参与 L3 拟合.*疑似问题/);
  assert.doesNotMatch(ui.run(`ecPointStatus({id:'a',used:true,quality_reasons:['R² < 0.7']})`), /未参与/);
  assert.match(ui.run(`ecPointStatus({id:'a',used:false,draft_used:true,quality_reasons:['large uncertainty']})`), /参与 L3 拟合/);
  assert.equal(ui.run(`isUsed({id:'a',used:false,draft_used:true})`), true);
  assert.equal(ui.run(`ecPointStatus({id:'a',used:true,excluded:true,quality_reasons:['R² < 0.7']})`), '人工屏蔽');
  assert.match(ui.run(`ecPointStatus({id:'a',used:false,quality_reasons:[]})`), /未参与 L3.*无可用参数/);
});

test('L1 residuals use matching histogram bins and finite model counts with Poisson scaling', () => {
  const ui = controller();
  const residual = JSON.parse(ui.run(`JSON.stringify(l1Residual({
    x: [10, 20, 30, 40, 50, 60], y: [9, 0, 4, NaN, 8, 3],
    curve: { x: [10, 20, 30, 40, 45, 50, 60], y: [4, 0, 9, 9, 5, Infinity, 1] }
  }))`));
  assert.deepEqual(residual.x, [10, 20, 30, 60]);
  assert.equal(residual.y.length, 4);
  for (const [index, expected] of [5 / 3, 0, -2.5, 2 / Math.sqrt(3)].entries()) {
    assert.ok(Math.abs(residual.y[index] - expected) < 1e-12);
  }
});

test('an old residual plot cannot overwrite the latest file result caption or metric', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.point = { id: '14B:094:0', stem: '094' };
    const entered = deferred(), release = deferred();
    Plotly = { react: async () => { entered.resolve(); await release.promise; } };
    window.Plotly = Plotly;
    const pending = renderL1Residual({ x: [100, 200], y: [4, 16], curve: { x: [100, 200], y: [4, 9] } }, [100, 200]);
  `);
  await ui.run('entered.promise');
  ui.run(`
    ++state.spectrumToken; state.point = { id: '14B:101:0', stem: '101' };
    $('residual-caption').textContent = 'current file residual';
    $('residual-metric').textContent = 'current file metric';
    release.resolve();
  `);
  await ui.run('pending');
  assert.equal(ui.element('residual-caption').textContent, 'current file residual');
  assert.equal(ui.element('residual-metric').textContent, 'current file metric');
});

test('a stale failed request cannot replace a newer view with an error', async () => {
  const ui = controller();
  ui.run(`
    const response = deferred(); api = () => response.promise;
    const pending = loadSurfaceSafely();
    state.layer = 'EC_L0'; ++state.loadToken;
    response.reject(new Error('old failure'));
  `);
  await ui.run('pending');
  assert.equal(ui.run('state.testError'), undefined);
});

test('a plot finishing after a layer switch cannot paint the old secondary chart or caption', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1';
    state.point = { id: 'old', stem: 'old', center: 200, temp: 20, bias: 28 };
    state.spectrum = { x: [100, 200, 300], y: [1, 20, 1], curve: { x: [100, 200, 300], y: [2, 18, 2] } };
    $('window-lo').value = '100'; $('window-hi').value = '300';
    const entered = deferred(), release = deferred(); let writes = [];
    Plotly = { react: async chart => { writes.push(chart.id); if (writes.length === 1) { entered.resolve(); await release.promise; } } };
    window.Plotly = Plotly;
    const pending = renderSpectrum();
  `);
  await ui.run('entered.promise');
  ui.run(`state.layer = 'EC_L0'; ++state.loadToken; $('surface-caption').textContent = 'current EC view'; release.resolve();`);
  await ui.run('pending');
  assert.equal(ui.run('writes.join()'), 'surface-chart');
  assert.equal(ui.element('surface-caption').textContent, 'current EC view');
});

test('single-file unpack always sends the selected raw stem without representative fallback', async () => {
  for (const layer of ['TB_L0', 'EC_L0']) {
    const ui = controller();
    ui.run(`state.layer = '${layer}'; state.channel = 2; state.point = { id: 'a', stem: 'chosen', event: 'chosen.dat' }; let submitted; executeJob = async (layer, options) => { submitted = { layer, ...options }; };`);
    await ui.run('runUnpack()');
    assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), { layer, stem: 'chosen', channel: 2, representative: false });
  }
});

test('unpack rejects missing selection and history-only input before submitting', async () => {
  const ui = controller();
  ui.run(`let submissions = 0; executeJob = async () => { submissions++; };`);
  await assert.rejects(ui.run('runUnpack()'), /选择输入文件/);
  ui.run(`state.point = { stem: 'history-only' };`);
  await assert.rejects(ui.run('runUnpack()'), /未找到.*原始文件/);
  ui.run(`state.point = { stem: 'chosen', event: 'chosen.dat' }; state.run.dataset.raw_dir = null;`);
  await assert.rejects(ui.run('runUnpack()'), /原始数据目录/);
  assert.equal(ui.run('submissions'), 0);
});

test('a selected file hidden by search cannot be unpacked accidentally', async () => {
  const ui = controller();
  ui.run(`state.point = { id: 'one', stem: 'one', event: 'one.dat' }; let submissions = 0; executeJob = async () => { submissions++; };`);
  ui.element('unpack-search').value = 'different-file';
  await assert.rejects(ui.run('runUnpack()'), /筛选/);
  assert.equal(ui.run('submissions'), 0);
});

test('batch unpack operates on all matched inputs even when no file is selected', async () => {
  const ui = controller();
  ui.run(`state.surface = { points: [{ stem: 'one', event: 'one.dat' }, { stem: 'two', event: 'two.dat' }] }; let submitted; executeJob = async (layer, options) => { submitted = options; };`);
  await ui.run('runUnpack(true)');
  assert.equal(ui.run('submitted.representative'), false);
  assert.equal(ui.run("Object.hasOwn(submitted, 'stem')"), false);
});

test('batch unpack retains its explicit all-input scope while search is active', async () => {
  const ui = controller();
  ui.run(`state.surface = { points: [{ stem: 'one', event: 'one.dat' }, { stem: 'two', event: 'two.dat' }] }; let submitted; executeJob = async (layer, options) => { submitted = options; };`);
  ui.element('unpack-search').value = 'one';
  await ui.run('runUnpack(true)');
  assert.equal(ui.run('unpackRawInputs().length'), 2);
  assert.equal(ui.run("Object.hasOwn(submitted, 'stem')"), false);
});

test('EC batch counts all physical channel files and source observations from the reading configuration', () => {
  const ui = controller();
  ui.run(`
    state.run.inventory.raw = { reading_preview: { rows: [
      { kind: 'tb', stem: 'tb-one', event: 'tb.dat' },
      { kind: 'xray', channel: 0, stem: 'x-ch0', event: 'x0.dat' },
      { kind: 'xray', channel: 1, stem: 'x-ch1', event: 'x1.dat' },
      { kind: 'source', channel: null, stem: 'source', event: 'source.dat' }
    ] } };
    state.layer = 'EC_L0'; state.channel = 0;
    state.ecOverview = { files: [{ stem: 'x-ch0', event: 'x0.dat' }] };
    $('unpack-search').value = 'x-ch0';
  `);
  assert.equal(ui.run('unpackRawInputs().length'), 3);
  ui.run(`state.channel = 1;`);
  assert.equal(ui.run('unpackRawInputs().length'), 3);
  ui.run(`state.layer = 'TB_L0';`);
  assert.equal(ui.run('unpackRawInputs().length'), 1);
});

test('data dialog preserves a draft within a run and switches directories with the active run', () => {
  const ui = controller();
  ui.run(`
    registryDatasetSelection = async () => {}; sourceLibraryRefresh = async () => {};
    state.datasets = [
      { id: 'dataset-a', payload: '14B', raw_dir: 'raw-a', product_dir: 'products-a' },
      { id: 'dataset-b', payload: '15B', raw_dir: 'raw-b', product_dir: 'products-b' }
    ];
    state.run.dataset = state.datasets[0];
    openCreateDialog('14B');
  `);
  assert.equal(ui.element('dataset-raw-dir').value, 'raw-a');
  ui.element('dataset-raw-dir').value = 'draft-a';
  ui.run(`datasetPathsChanged(); openCreateDialog('14B');`);
  assert.equal(ui.element('dataset-raw-dir').value, 'draft-a');
  ui.run(`state.run = { id: 'run-b', payload: '15B', dataset: state.datasets[1] }; openCreateDialog('15B');`);
  assert.equal(ui.element('dataset-raw-dir').value, 'raw-b');
  assert.equal(ui.element('dataset-product-dir').value, 'products-b');
  assert.equal(ui.element('dataset-select').value, 'dataset-b');
  assert.equal(ui.run('state.datasetPathsDirty'), false);
});

test('switching runs restores its saved dataset after a scan narrowed the current choices', () => {
  const ui = controller();
  ui.run(`
    sourceLibraryRefresh = async () => {};
    registryDatasetSelection = async () => {};
    state.datasets = [{ id: 'dataset-a', payload: '14B', raw_dir: 'raw-a' }];
    state.run = { id: 'run-b', payload: '15B', dataset: { id: 'dataset-b', payload: '15B', raw_dir: 'raw-b' } };
    openCreateDialog('15B');
  `);
  assert.equal(ui.element('dataset-raw-dir').value, 'raw-b');
  assert.equal(ui.element('dataset-select').value, 'dataset-b');
});

test('editing directories invalidates the selected dataset and blocks stale creation', async () => {
  const ui = controller();
  ui.element('dataset-select').value = 'old-dataset';
  ui.run(`let submissions = 0; api = async () => { submissions++; }; datasetPathsChanged();`);
  assert.equal(ui.element('dataset-select').value, '');
  assert.equal(ui.element('new-run-form').querySelector('[type="submit"]').disabled, true);
  await ui.run(`createRun({ preventDefault() {}, submitter: $('new-run-form').querySelector('[type="submit"]') })`);
  assert.equal(ui.run('submissions'), 0);
  assert.match(ui.element('dataset-create-error').textContent, /扫描并识别/);
  assert.equal(ui.element('dataset-create-error').hidden, false);
  assert.equal(ui.element('dataset-create-error').focused, true);
  assert.equal(ui.element('new-run-form').querySelector('[type="submit"]').disabled, true);
});

test('editing a directory while scanning discards the old scan response', async () => {
  const ui = controller();
  ui.run(`const response = deferred(); api = () => response.promise; const pending = scanDatasets(); datasetPathsChanged(); response.resolve({ datasets: [{ id: 'old-dataset', payload: '14B' }] });`);
  await ui.run('pending');
  assert.equal(ui.run('state.datasets.length'), 0);
  assert.equal(ui.run('state.datasetPathsDirty'), true);
  assert.equal(ui.element('new-run-form').querySelector('[type="submit"]').disabled, true);
});

test('an obsolete directory scan failure cannot overwrite the newly edited directory state', async () => {
  const ui = controller();
  ui.run(`const response = deferred(); api = () => response.promise; const pending = scanDatasets(); datasetPathsChanged(); response.reject(new Error('old directory offline'));`);
  await ui.run('pending');
  assert.match(ui.element('dataset-scan-state').textContent, /目录已改变/);
  assert.equal(ui.element('new-run-form').querySelector('[type="submit"]').disabled, true);
});

test('payload choices show payload identifiers while retaining distinct custom payloads', () => {
  const ui = controller();
  ui.run(`
    state.run = { id: 'ten', payload: '10B' };
    state.bootstrap = {
      datasets: [
        { id: 'ten-data', payload: '10B', label: '10B（已校验工作区副本）' },
        { id: 'n5-data', payload: '四天格', label: '四天格（最新载荷）', acquisition_preset_match: { reference: { payload: 'N5' } } }
      ],
      runs: [{ id: 'ten', payload: '10B' }, { id: 'n5', payload: 'N5', dataset_id: 'n5-data' }, { id: 'custom', payload: 'MY-42' }]
    };
    renderPayloadOptions();
  `);
  assert.deepEqual(ui.element('payload').options.map(option => option.text), ['10B', 'MY-42', 'N5']);
  assert.equal(ui.element('payload').value, '10B');
});

test('trial choices contain only the selected payload with concise labels and preserved stored names', () => {
  const ui = controller();
  ui.run(`
    state.run = { id: 'ten-b', payload: '10B' };
    state.bootstrap = { runs: [
      { id: 'other', payload: '14B', name: '14B · 标定' },
      { id: 'ten-a', payload: '10B', name: '10B · 10B · 新协议验收（' },
      { id: 'ten-b', payload: '10B', name: '窗口复核 · 10B' }
    ] };
    renderRunOptions();
  `);
  assert.deepEqual(ui.element('run-select').options.map(option => option.text), ['选择试算', '新协议验收', '窗口复核']);
  assert.equal(ui.element('run-select').value, 'ten-b');
  assert.equal(ui.run('state.bootstrap.runs[1].name'), '10B · 10B · 新协议验收（');
});

test('a payload identified by its preset opens the matching dataset before its first trial exists', () => {
  const ui = controller();
  ui.run(`
    sourceLibraryRefresh = async () => {}; registryDatasetSelection = async () => {};
    state.datasets = [
      { id: '14b-data', payload: '14B', raw_dir: 'raw-14b' },
      { id: 'n5-data', payload: '四天格', raw_dir: 'raw-n5', acquisition_preset_match: { reference: { payload: 'N5' } } }
    ];
    state.bootstrap = { datasets: state.datasets, runs: [{ id: '14b', payload: '14B' }] };
    openCreateDialog('N5');
  `);
  assert.equal(ui.element('dataset-select').value, 'n5-data');
  assert.equal(ui.element('dataset-raw-dir').value, 'raw-n5');
  assert.equal(ui.element('new-run-payload').value, 'N5');
});

test('an unmatched payload never silently selects another payload dataset', () => {
  const ui = controller();
  ui.run(`
    sourceLibraryRefresh = async () => {}; registryDatasetSelection = async () => {};
    state.datasets = [{ id: '14b-data', payload: '14B', raw_dir: 'raw-14b' }];
    openCreateDialog('NEW-PAYLOAD');
  `);
  assert.equal(ui.element('dataset-select').value, '');
  assert.equal(ui.element('dataset-raw-dir').value, '');
  assert.equal(ui.element('new-run-payload').value, 'NEW-PAYLOAD');
});

test('duplicate concise trial names remain distinguishable by their creation dates', () => {
  const ui = controller();
  ui.run(`
    state.bootstrap = { runs: [
      { id: 'a', payload: '14B', name: '14B · 复核', created_at: '2026-10-20T08:00:00Z' },
      { id: 'b', payload: '14B', name: '14B · 复核', created_at: '2026-10-21T08:00:00Z' }
    ] };
    renderRunOptions();
  `);
  const labels = ui.element('run-select').options.slice(1).map(option => option.text);
  assert.notEqual(labels[0], labels[1]);
  assert.ok(labels.every(label => label.startsWith('复核 · ')));
});

test('trial creation failure appears inside the dialog and receives focus', async () => {
  const ui = controller();
  ui.element('dataset-select').value = 'ten-data';
  ui.element('new-run-payload').value = '10B';
  ui.run(`
    registryCreateSpec = () => ({});
    api = async () => { throw new Error('请选择10B读取配置'); };
  `);
  await ui.run(`createRun({ preventDefault() {}, submitter: $('new-run-form').querySelector('[type="submit"]') })`);
  assert.equal(ui.element('dataset-create-error').textContent, '请选择10B读取配置');
  assert.equal(ui.element('dataset-create-error').hidden, false);
  assert.equal(ui.element('dataset-create-error').focused, true);
  assert.equal(ui.run('state.testError'), undefined);
});

test('view memory separates runs, pipeline families and selected channels', () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.channel = 2; state.point = { id: 'tb-case:2', stem: 'tb-case' }; rememberView();
    state.run = { id: 'run-b', payload: '15B' }; state.layer = 'EC_L0'; state.channel = 0; state.point = { id: '70keV_ch0', stem: '70keV_ch0' }; rememberView();
    state.channel = 3; state.point = null;
  `);
  assert.equal(ui.run('preferredPoint().stem'), '70keV_ch3');
  ui.run(`state.run = { id: 'run-a', payload: '14B' }; state.layer = 'TB_L2'; state.channel = 2;`);
  assert.equal(ui.run('preferredPoint().id'), 'tb-case:2');
  ui.run(`state.channel = 1;`);
  assert.equal(ui.run('preferredPoint().stem'), 'tb-case');
  ui.run(`state.layer = 'EC_L1';`);
  assert.equal(ui.run('preferredPoint().stem'), undefined);
});

test('reopening each trial restores its layer and channel while new trials start at L0', async () => {
  const ui = controller();
  ui.run(`
    localStorage.setItem('grid-workbench-view:run-b', JSON.stringify({ layer: 'EC_L2', channel: 3 }));
    state.layer = 'TB_L1'; state.channel = 1;
    renderRun = () => {}; loadSurfaceSafely = async () => {};
    api = async path => ({ id: path.split('/').at(-1), payload: '10B', jobs: [] });
  `);
  await ui.run(`openRun('run-b')`);
  assert.equal(ui.run('state.layer'), 'EC_L2');
  assert.equal(ui.run('state.channel'), 3);
  assert.equal(ui.element('channel').value, '3');
  assert.equal(ui.run(`localStorage.getItem('grid-workbench-payload:10B')`), 'run-b');
  await ui.run(`openRun('run-new')`);
  assert.equal(ui.run('state.layer'), 'TB_L0');
  assert.equal(ui.run('state.channel'), 0);
  await ui.run(`openRun('run-b')`);
  assert.equal(ui.run('state.layer'), 'EC_L2');
  assert.equal(ui.run('state.channel'), 3);
});

test('moving from TB unpack to fitting keeps the selected condition file', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L0'; state.point = { id: '14B:last-case:0', stem: 'last-case' };
    renderNavigation = () => {}; renderStage = () => {}; renderSurface = async () => {};
    api = async () => ({ points: [{ id: '14B:first-case:0', stem: 'first-case' }, { id: '14B:last-case:0', stem: 'last-case' }] });
    let selected;
    selectPoint = async point => { selected = point; };
  `);
  await ui.run(`selectLayer('TB_L1')`);
  assert.equal(ui.run('selected.stem'), 'last-case');
});

test('moving from EC unpack to correction keeps the selected source or energy file', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'EC_L0'; state.point = { id: 'src_Cs137_5m', stem: 'src_Cs137_5m' };
    renderNavigation = () => {}; renderStage = () => {};
    api = async () => ({ files: [{ stem: '40keV_ch0' }, { stem: 'src_Cs137_5m' }] });
    let selected;
    selectEcPoint = async point => { selected = point; };
  `);
  await ui.run(`selectLayer('EC_L1')`);
  assert.equal(ui.run('selected.stem'), 'src_Cs137_5m');
});

test('switching pipeline families never records a TB selection as an EC selection', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L0'; state.point = { id: 'tb-case:0', stem: 'tb-case' };
    renderNavigation = () => {}; renderStage = () => {}; loadSurfaceSafely = async () => {};
  `);
  await ui.run(`selectLayer('EC_L0')`);
  assert.equal(ui.run('savedView().familyStems.TB'), 'tb-case');
  assert.equal(ui.run('savedView().familyStems.EC'), undefined);
});

test('current file status ignores a failed fit of another file or pipeline layer', () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L0'; state.point = { id: '94:0', stem: '094_observe' };
    state.run.jobs = [
      { id: 'unpacked', layer: 'TB_L0', status: 'succeeded', config: { only: ['094_observe'] } },
      { id: 'fit-failed', layer: 'TB_L1', status: 'failed', config: { only: ['094_observe'] } },
      { id: 'other-unpack-failed', layer: 'TB_L0', status: 'failed', config: { only: ['108_observe'] } }
    ];
    state.run.stages = { TB_L0: { status: 'failed', stale: true } };
    renderCurrentStatus();
  `);
  assert.equal(ui.run('currentLayerJob().id'), 'unpacked');
  assert.equal(ui.element('stage-status').textContent, '计算成功');
});

test('output path matching cannot treat a different numeric energy as the selected file', () => {
  const ui = controller();
  ui.run(`
    state.layer = 'EC_L0'; state.point = { id: '90keV_ch0', stem: '90keV_ch0' };
    state.run.jobs = [{ id: 'other-energy', layer: 'EC_L0', status: 'succeeded', result: { outputs: ['products/190keV_ch0.l0.parquet'] } }];
  `);
  assert.equal(ui.run('currentLayerJob()'), undefined);
});

test('the unpack result bar follows the selected file instead of the last job for another file', () => {
  const ui = controller();
  ui.run(`
    state.point = { id: '108:0', stem: '108_observe', event: '108.dat', hk: '108.hk' };
    state.surface = { points: [state.point] };
    state.run.jobs = [{ id: 'other', layer: 'TB_L0', status: 'failed', config: { only: ['094_observe'] }, error: 'memory allocation failed' }];
    productionRenderUnpackWorkspace();
  `);
  assert.equal(ui.element('unpack-progress').hidden, true);
  ui.run(`
    state.run.jobs.push({ id: 'selected', layer: 'TB_L0', status: 'succeeded', config: { only: ['108_observe'] } });
    productionRenderUnpackWorkspace();
  `);
  assert.equal(ui.element('unpack-progress').hidden, false);
  assert.match(ui.element('unpack-progress').textContent, /108_observe/);
  assert.doesNotMatch(ui.element('unpack-progress').textContent, /memory allocation/);
});

test('a TB workflow keeps the explicit file through unpack and fit without representative fallback', async () => {
  const ui = controller();
  ui.run(`
    const plan = pipelinePlan('tb', '094_observe_50C_290', '');
    let submitted = [];
    selectLayer = async layer => { state.layer = layer; };
    executeJob = async (layer, spec) => { submitted.push({ layer, ...spec }); };
  `);
  await ui.run(`runPipeline(plan, 'tb')`);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), [
    { layer: 'TB_L0', stem: '094_observe_50C_290', representative: false, channel: 0 },
    { layer: 'TB_L1', stem: '094_observe_50C_290', representative: false, channel: 0, fit_channels: [0] },
  ]);
  assert.throws(() => ui.run(`pipelinePlan('tb', '', '')`), /请选择 TB 文件/);
});

test('a source workflow explicitly schedules its background before the selected source fit', () => {
  const ui = controller();
  ui.run(`
    state.run.inventory = { raw: { reading_preview: { rows: [
      { kind: 'background', stem: 'src_bkg_30m' },
      { kind: 'background', stem: 'src_bkg_30m' },
      { kind: 'source', stem: 'src_Cs137_5m' },
      { kind: 'xray', stem: '40keV_ch0' }
    ] } } };
    const plan = pipelinePlan('ec', '', 'src_Cs137_5m');
  `);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(plan)')), [
    { layer: 'EC_L0', stem: 'src_bkg_30m', representative: false },
    { layer: 'EC_L1', stem: 'src_bkg_30m', representative: false },
    { layer: 'EC_L0', stem: 'src_Cs137_5m', representative: false },
    { layer: 'EC_L1', stem: 'src_Cs137_5m', representative: false },
    { layer: 'EC_L2', stem: 'src_Cs137_5m', representative: false },
  ]);
});

test('EC workflow fitting submits only points from the chosen file and channel', async () => {
  const ui = controller();
  ui.run(`
    state.channel = 2;
    state.ecOverview = { points: [
      { id: 'chosen:2:511', stem: 'src_Na22_5m' },
      { id: 'chosen:2:1275', stem: 'src_Na22_5m' },
      { id: 'other:2:40', stem: '40keV_ch2' }
    ] };
    let submitted = [], selected = [];
    selectLayer = async layer => { state.layer = layer; };
    selectEcPoint = async point => { state.point = point; selected.push(point.id); };
    executeJob = async (layer, spec) => { submitted.push({ layer, ...spec }); };
  `);
  await ui.run(`runPipeline([{layer:'EC_L2',stem:'src_Na22_5m',representative:false}], 'ec')`);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), [
    { layer: 'EC_L2', point_id: 'chosen:2:511', stem: 'src_Na22_5m', channel: 2, representative: false },
    { layer: 'EC_L2', point_id: 'chosen:2:1275', stem: 'src_Na22_5m', channel: 2, representative: false },
  ]);
  assert.equal(ui.run('selected.join()'), 'chosen:2:511,chosen:2:1275,chosen:2:511');
  assert.equal(ui.run('state.point.id'), 'chosen:2:511');
});

test('a missing EC workflow spectrum fails instead of fitting another available spectrum', async () => {
  const ui = controller();
  ui.run(`
    state.ecOverview = { points: [{ id: 'other', stem: '40keV_ch0' }] };
    let submissions = 0;
    selectLayer = async layer => { state.layer = layer; };
    executeJob = async () => { submissions++; };
  `);
  await assert.rejects(ui.run(`runPipeline([{layer:'EC_L2',stem:'90keV_ch0',representative:false}], 'ec')`), /未找到 90keV_ch0/);
  assert.equal(ui.run('submissions'), 0);
});

test('the full workflow processes all inputs and binds its new TB result before EC correction', async () => {
  const ui = controller();
  ui.run(`
    state.run.tb_version = { path: 'previous-tb.json' };
    const plan = pipelinePlan('all', '', '');
    let actions = [];
    selectLayer = async layer => { state.layer = layer; };
    executeJob = async (layer, spec) => {
      actions.push({ layer, ...spec });
      if (layer === 'TB_L2') state.run.tb_version = { path: 'new-tb.json' };
    };
    api = async (path, request) => { actions.push({ dependency: request.body.path }); return { ...state.run, selected_tb: state.run.tb_version }; };
  `);
  await ui.run(`runPipeline(plan, 'all')`);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(actions)')), [
    { layer: 'TB_L0', representative: false },
    { layer: 'TB_L1', representative: false },
    { layer: 'TB_L2', representative: false },
    { layer: 'EC_L0', representative: false },
    { dependency: 'new-tb.json' },
    { layer: 'EC_L1', representative: false },
    { layer: 'EC_L2', representative: false },
    { layer: 'EC_L3', representative: false },
  ]);
});

test('a workflow stops at a failed step and releases its processing state', async () => {
  const ui = controller();
  ui.run(`
    const plan = pipelinePlan('tb', '094_observe', '');
    let submitted = [], busyStates = [];
    setBusy = value => { busyStates.push(value); };
    selectLayer = async layer => { state.layer = layer; };
    executeJob = async layer => { submitted.push(layer); throw new Error('解包失败'); };
  `);
  await assert.rejects(ui.run(`runPipeline(plan, 'tb')`), /解包失败/);
  assert.equal(ui.run('submitted.join()'), 'TB_L0');
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(busyStates)')), [true, false]);
});

test('quality warnings keep the full workflow running through both aggregate fits', async () => {
  const ui = controller();
  ui.run(`
    const plan = pipelinePlan('all', '', '');
    let submitted=[];
    selectLayer=async layer=>{state.layer=layer;};
    executeJob=async layer=>{
      submitted.push(layer);
      if(layer==='TB_L2') state.run.tb_version={path:'warning-inclusive-tb.json'};
      return {layer,status:'succeeded',result:{processor:{channels_suspect:13}},fit_quality:{accepted:false,status:'suspect'}};
    };
    api=async()=>state.run;
  `);
  await ui.run(`runPipeline(plan,'all')`);
  assert.equal(ui.run('submitted.join()'), 'TB_L0,TB_L1,TB_L2,EC_L0,EC_L1,EC_L2,EC_L3');
  assert.equal(ui.element('message').textContent, '流程已完成。');
});

test('a workflow stops after a partial L1 batch so its files can be reviewed before surface fitting', async () => {
  const ui = controller();
  ui.run(`
    const plan = pipelinePlan('all', '', '');
    let submitted = [], busyStates = [];
    setBusy = value => { busyStates.push(value); };
    selectLayer = async layer => { state.layer = layer; };
    executeJob = async layer => {
      submitted.push(layer);
      return { layer, status: layer === 'TB_L1' ? 'partial' : 'succeeded' };
    };
  `);
  await ui.run(`runPipeline(plan, 'all')`);
  assert.equal(ui.run('submitted.join()'), 'TB_L0,TB_L1');
  assert.equal(ui.run('state.layer'), 'TB_L1');
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(busyStates)')), [true, false]);
});

test('workflow capabilities block only unavailable steps while leaving the TB workflow usable', () => {
  const ui = controller();
  ui.run(`
    state.run.stages = { EC_L2: { unavailable_reason: '此载荷的跨照射本底尚未配置' } };
    $('pipeline-mode').value = 'tb'; $('pipeline-tb-file').value = 'T10_B28';
    updateCapabilityControls(); renderPipelinePlan();
  `);
  assert.equal(ui.element('run-pipeline').disabled, false);
  assert.equal(ui.element('start-pipeline').disabled, false);
  assert.equal(ui.element('pipeline-error').hidden, true);
  ui.run(`$('pipeline-mode').value = 'ec'; $('pipeline-ec-file').value = '70keV_ch0'; state.run.selected_tb = { filename: 'tb.json' }; renderPipelinePlan();`);
  assert.equal(ui.element('start-pipeline').disabled, true);
  assert.match(ui.element('pipeline-error').textContent, /跨照射本底/);
  ui.run(`$('pipeline-mode').value = 'all'; renderPipelinePlan();`);
  assert.equal(ui.element('start-pipeline').disabled, true);
});

test('workflow execution selects its requested file so its output is shown instead of an old remembered file', async () => {
  const ui = controller();
  ui.run(`
    state.surface = { points: [{id:'first',stem:'first'}, {id:'chosen',stem:'chosen'}] };
    state.point = state.surface.points[0];
    let visibleAtSubmit = [];
    selectLayer = async layer => { state.layer = layer; };
    selectPoint = async point => { state.point = point; };
    executeJob = async () => { visibleAtSubmit.push(state.point.stem); };
  `);
  await ui.run(`runPipeline(pipelinePlan('tb', 'chosen', ''), 'tb')`);
  assert.equal(ui.run('visibleAtSubmit.join()'), 'chosen,chosen');
});

test('saving XML without opening the lazy metadata tab preserves rules, manifest and source lines', () => {
  const ui = controller();
  ui.run(`
    $('acquisition-panel-metadata').hidden = true;
    const profile = {
      xml: { event: { text: '<event/>' }, hk: { text: '<hk/>' } },
      parser: {
        event_tag: 'event', hk_tag: 'hk', endian: 'MSB',
        fields: { channel: 'channel', utc: ['utc'] },
        transform: Object.fromEntries(Object.keys(acquisitionTransformLabels).map(key => [key, 1])),
        conditions: { mode: 'hk', temp_scale: 1, bias_scale: 1 }
      },
      discovery: {
        mode: 'manifest',
        manifest: [{ event: 'a.dat', hk: 'a.hk', kind: 'source', peak_energies_keV: [59.5], extra: 'preserve' }],
        rules: [{ kind: 'tb', event_regex: 'event', hk_regex: 'hk', metadata: { physical: true } }],
        sources: [{ name: 'Am241', aliases: ['Am'], energies_keV: [59.5], extra: 'preserve' }]
      }
    };
    acquisitionRenderProfile(profile);
    $('event-xml-text').value = '<event changed="true"/>';
    const saved = acquisitionReadProfile();
  `);
  assert.equal(ui.run('acquisitionDraft.metadataRendered'), false);
  assert.equal(ui.element('profile-manifest-rows').rows.length, 0);
  assert.equal(ui.run('JSON.stringify(saved.discovery)'), ui.run('JSON.stringify(profile.discovery)'));
  assert.equal(ui.run('saved.xml.event.text'), '<event changed="true"/>');
});

test('plastic CH3 leaves window editing available while every current-channel Gaussian entry is blocked', async () => {
  for (const payload of ['10B', '11B']) {
    const ui = controller();
    ui.run(`
      state.run.payload='${payload}'; state.layer='TB_L1'; state.channel=3;
      state.point={id:'plastic:3',stem:'plastic',channel:3}; state.spectrum={x:[0,100],y:[2,20]};
      let submissions=0; executeJob=async()=>{submissions++;};
      setBusy=productionSetBusy; setBusy(false);
    `);
    assert.equal(ui.element('fit-spectrum').disabled, true);
    assert.equal(ui.element('auto-fit-spectrum').disabled, true);
    assert.equal(ui.element('run-layer').disabled, true);
    assert.equal(ui.element('window-lo').disabled, false);
    assert.equal(ui.element('window-hi').disabled, false);
    assert.equal(ui.element('spectral-model-note').textContent, '塑料闪烁体 · 康普顿响应，拟合方案待定');
    await assert.rejects(ui.run('fitSpectrum()'), /康普顿响应/);
    await assert.rejects(ui.run('autoFitSpectrum()'), /康普顿响应/);
    assert.equal(ui.run('submissions'), 0);
  }
});

test('plastic spectrum shows counts and draggable bounds without its old Gaussian or peak candidates', async () => {
  const ui = controller();
  ui.run(`
    state.run.payload='10B'; state.layer='TB_L1'; state.channel=3;
    state.point={id:'plastic:3',stem:'plastic',channel:3};
    $('window-lo').value='20'; $('window-hi').value='80';
    state.spectrum={x:[0,50,100],y:[2,20,4],curve:{x:[50],y:[19],background:[3]},fit:{peak_detection:{candidates:[{center:50}]}}};
    let drawn=[];
    globalThis.Plotly=window.Plotly={react:async(chart,traces,layout)=>{chart.data=traces;drawn.push({id:chart.id,traces,layout});},relayout:async()=>{}};
  `);
  await ui.run('renderSpectrum()');
  assert.equal(ui.run('drawn[0].id'), 'surface-chart');
  assert.equal(ui.run('drawn[0].traces.length'), 1);
  assert.equal(ui.run('drawn[0].layout.shapes.length'), 3);
  assert.equal(ui.run(`$('l1-spectrum-legend').dataset.fitAvailable`), 'false');
  assert.equal(ui.element('spectrum-params').children.length, 0);
});

test('renaming an output payload retains the detector response from its preset or dataset', async () => {
  for (const identity of [
    {dataset:{profile:'10B',source_payload:'10B'}},
    {dataset:{source_payload:'11B'}},
    {dataset:{profile:'14B'},acquisition_preset:{payload:'10B'}},
  ]) {
    const ui = controller();
    ui.run(`
      Object.assign(state.run, ${JSON.stringify(identity)}, {payload:'TEST-10'});
      state.layer='TB_L1'; state.channel=3;
      state.point={id:'TEST-10:case:3',stem:'case',channel:3}; state.spectrum={x:[0,100],y:[2,20]};
      let submissions=0; executeJob=async()=>{submissions++;};
      setBusy=productionSetBusy; setBusy(false);
    `);
    assert.equal(ui.run('supportedFitChannels().join()'), '0,1,2');
    assert.equal(ui.element('fit-spectrum').disabled, true);
    assert.equal(ui.element('window-lo').disabled, false);
    await assert.rejects(ui.run('fitSpectrum()'), /康普顿响应/);
    assert.equal(ui.run('submissions'), 0);
  }
  const ui = controller();
  ui.run(`state.run.payload='TEST-10'; state.run.dataset.profile='10B'; state.run.acquisition_preset={payload:'14B'};`);
  assert.equal(ui.run('detectorPayload()'), '14B');
  assert.equal(ui.run('supportedFitChannels().join()'), '0,1,2,3');
});

test('EC manual fitting sends the displayed point and window with automatic selection explicitly disabled', async () => {
  const ui = controller();
  ui.run(`
    state.layer='EC_L2'; state.channel=2; state.point={id:'source:2:59.5',stem:'source',E:59.5};
    state.spectrum={x:[0,100],y:[2,10]}; $('window-lo').value='25'; $('window-hi').value='75';
    let patch,submitted; saveRevision=async extra=>{patch=extra;};
    executeJob=async(layer,config)=>{submitted={layer,...config};}; notify=()=>{};
  `);
  await ui.run('fitSpectrum()');
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(patch)')), {ec_windows:{'source:2:59.5':{lo:25,hi:75}}});
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), {layer:'EC_L2',point_id:'source:2:59.5',channel:2,automatic_window:false});
  assert.match(ui.element('fit-result-status').textContent, /当前窗口拟合完成/);
});

test('fitting uses the displayed EC bin width and drag limits follow the number of fit bins', async () => {
  const ui = controller();
  ui.run(`
    state.layer='TB_L1'; state.point={id:'case:0',stem:'case'};
    state.spectrum={histogram_config:{min:0,max:16384,bin_width:6}};
    $('window-lo').value='100'; $('window-hi').value='200';
  `);
  assert.equal(ui.run('spectrumBinWidth()'), 6);
  assert.equal(ui.run(`boundedWindowValue('window-lo',199)`), 152);
  assert.equal(ui.run(`boundedWindowValue('window-hi',101)`), 148);
  ui.run(`$('window-hi').value='120';`);
  assert.throws(()=>ui.run('windowPatch()'), /至少需要 48 ADC/);
  ui.run(`
    state.layer='EC_L2'; state.channel=1; state.point={id:'tube:1:40',stem:'tube',E:40};
    state.spectrum={bin_width:6,window_bounds:{min:0,max:700,minimum_width:48}};
    $('window-lo').value='200'; $('window-hi').value='350';
    let submitted; saveRevision=async()=>{}; notify=()=>{};
    executeJob=async(layer,config)=>{submitted={layer,...config};return{status:'succeeded'};};
  `);
  await ui.run('fitSpectrum()');
  assert.equal(ui.run('submitted.bin_width'), 6);
  assert.equal(ui.run('submitted.point_id'), 'tube:1:40');
  assert.equal(ui.run('submitted.automatic_window'), false);
  ui.run(`state.spectrum={bin_width:4}; $('window-hi').value='230';`);
  assert.equal(ui.run(`boundedWindowValue('window-lo',229)`), 198);
  ui.run(`state.spectrum.window_bounds={min:10,max:900,minimum_width:40};`);
  assert.equal(ui.run(`boundedWindowValue('window-lo',229)`), 190);
});

test('EC file filtering and previous-next navigation retain the current physical channel', async () => {
  const ui = controller();
  ui.run(`
    state.layer='EC_L2'; state.channel=2; state.ecOverview={points:[
      {id:'a',stem:'tube-50',E:50},{id:'b',stem:'tube-60',E:60},{id:'c',stem:'source-Cs137',E:661.7}
    ]}; state.point=state.ecOverview.points[0];
    $('peak-file-search').value='tube'; let picked=[];
    selectEcPoint=async point=>{state.point=point;picked.push(point.id);};
  `);
  await ui.run('moveEcReviewFile(1)');
  assert.equal(ui.run('state.point.id'), 'b');
  await ui.run('moveEcReviewFile(1)');
  assert.equal(ui.run('picked.join()'), 'b');
  assert.equal(ui.run('state.channel'), 2);
  ui.run(`$('peak-file-search').value='661.7'; renderEcReviewFiles();`);
  assert.equal(ui.element('point-select').options.length, 1);
  assert.equal(ui.element('point-select').options[0].value, 'c');
});

test('EC file switches cannot restore an older file when saves finish in reverse order', async () => {
  const ui = controller();
  ui.run(`
    state.layer='EC_L2'; state.channel=1; state.dirty=true; state.point={id:'old',stem:'old'};
    const firstSave=deferred(), secondSave=deferred(); let saves=0;
    saveRevision=async()=>{await (++saves===1 ? firstSave.promise : secondSave.promise);};
    api=async()=>({x:[0,100],y:[1,2],lo:10,hi:90});
    renderSpectrum=async()=>{}; renderSpectrumProvenance=()=>{}; renderEcOverview=async()=>{}; renderCurrentStatus=()=>{};
    const first=selectEcPoint({id:'first',stem:'first',E:50});
    const second=selectEcPoint({id:'second',stem:'second',E:60});
    secondSave.resolve();
  `);
  await ui.run('second');
  ui.run('firstSave.resolve()');
  await ui.run('first');
  assert.equal(ui.run('state.point.id'), 'second');
});

test('EC spectrum residuals use propagated signal-background errors', () => {
  const ui = controller();
  ui.run(`const actual=l1Residual({x:[1,2],y:[12,-2],yerr:[2,4],curve:{x:[1,2],y:[8,2]}});`);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(actual.y)')), [2,-1]);
});

test('next-layer navigation saves a changed window and opens the layer without running a processor', async () => {
  for (const [layer,next] of [['TB_L1','TB_L2'],['EC_L2','EC_L3']]) {
    const ui = controller();
    ui.run(`
      state.layer='${layer}';state.dirty=true;let actions=[];
      saveRevision=async()=>{actions.push('save');}; selectLayer=async layer=>{actions.push(layer);};
      executeJob=async()=>{actions.push('compute');};
    `);
    await ui.run('reviewNextLayer()');
    assert.equal(ui.run('actions.join()'), `save,${next}`);
  }
});

test('a single-file pipeline keeps its entry channel when another channel is viewed during processing', async () => {
  const ui = controller();
  ui.run(`
    state.channel=2; let submitted=[];
    selectLayer=async layer=>{state.layer=layer;};
    executeJob=async(layer,spec)=>{submitted.push({layer,...spec});state.channel=1;};
  `);
  await ui.run(`runPipeline(pipelinePlan('tb','chosen',''),'tb')`);
  assert.equal(ui.run('submitted[0].channel'), 2);
  assert.equal(ui.run('submitted[1].channel'), 2);
  assert.equal(ui.run('submitted[1].fit_channels.join()'), '2');
});

test('source energy editing retains other channel windows and independent ADC anchors', () => {
  const ui = controller();
  ui.run(`state.channel=1; state.ecPeakDraft={energy_key:'src_Na22',source_name:'Na22',lines:[
    {energy_keV:511,windows:{'0':{lo:100,hi:180},'1':{lo:110,hi:190}},centers:{'0':140,'1':150}},
    {energy_keV:1274.5,windows:{'1':{lo:300,hi:390}},centers:{}}
  ]}; state.ecPeakDraft.lines[0].windows['1'].lo=112;`);
  const body = JSON.parse(ui.run('JSON.stringify(ecPeakSaveBody())'));
  assert.deepEqual(body.lines[0].windows['0'], {lo:100,hi:180});
  assert.deepEqual(body.lines[0].windows['1'], {lo:112,hi:190});
  assert.deepEqual(body.lines[0].centers, {'0':140,'1':150});
  assert.equal(body.lines[1].energy_keV,1274.5);
  ui.run('state.ecPeakDraft.lines[1].energy_keV=511;');
  assert.throws(()=>ui.run('ecPeakSaveBody()'),/不重复/);
});

test('a newly added source line is saved before its unfitted spectrum is selected', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.channel=2;state.ecPeakDirty=true;
    state.ecPeakDraft={energy_key:'src_Test',lines:[{energy_keV:88.25,windows:{},centers:{}}]};
    state.ecOverview={points:[]};let selectedActions=[];
    saveEcPeakConfig=async reload=>{selectedActions.push('save:'+reload);state.ecPeakDirty=false;};
    loadEcAnalysis=async()=>{selectedActions.push('load:'+state.point.id);};`);
  await ui.run('selectEcPeakLine(0)');
  assert.equal(ui.run('selectedActions.join()'),'save:false,load:14B:ec:src_Test:2:88.25');
  assert.equal(ui.run('state.point.center'),undefined);
});

test('source batch fitting keeps the current observation file, channel and displayed binning', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.channel=1;state.point={stem:'src_Na22',energy_key:'src_Na22',E:511};
    state.ecPeakConfig={spectra:[{energy_key:'src_Na22',signal_file:'src_Na22_30m_corr.l1.parquet',lines:[]}]};
    state.spectrum={bin_width:8,point:{signal_file:'src_Na22_10m_corr.l1.parquet'}};let sourceFit;
    executeJob=async(layer,spec)=>{sourceFit={layer,...spec};};`);
  await ui.run('fitEcSource()');
  const spec=JSON.parse(ui.run('JSON.stringify(sourceFit)'));
  assert.equal(spec.energy_key,'src_Na22'); assert.equal(spec.stem,'src_Na22_10m');
  assert.deepEqual(spec.fit_channels,[1]); assert.equal(spec.channel,1); assert.equal(spec.bin_width,8);
  assert.equal(spec.representative,false);
});

test('saved artifact names are visible in the log while previews fetch only when expanded', async () => {
  const ui=controller();
  ui.run(`state.run.jobs=[{id:'job-one',layer:'EC_L2',status:'succeeded',outputs:[{path:'14B/ec/L2/json/source_fit_results.l2.json'}]}];
    let previewRequests=[];api=async path=>{previewRequests.push(path);return {kind:'json',json:{center:150},truncated:true};};
    renderJobs(); const files=$('job-list').children[0].children[1]; const previewDetails=files.children[0];`);
  assert.equal(ui.run('previewDetails.children[0].textContent'),'source_fit_results.l2.json');
  assert.equal(ui.run('previewRequests.length'),0);
  await ui.run(`previewDetails.open=true;previewDetails.listeners.toggle[0]()`);
  assert.match(ui.run('previewRequests[0]'),/artifact-preview\?path=.*job_id=job-one/);
  assert.equal(ui.run('previewDetails.dataset.loaded'),'true');
  assert.match(ui.run('previewDetails.children[1].children[0].textContent'),/150/);
});

test('an unchanged running-job poll does not rebuild records or collapse loaded previews', () => {
  const ui=controller();
  ui.run(`state.run.jobs=[{id:'busy',layer:'EC_L2',status:'running',outputs:[]}];renderJobs();
    const originalRow=$('job-list').children[0];state.run.jobs[0].log='more progress';renderJobs();`);
  assert.equal(ui.run("$('job-list').children[0]===originalRow"),true);
});

test('configuration save records show their JSON filenames beside processor runs', () => {
  const ui=controller();
  ui.run(`state.run.operations=[{id:'config-2',label:'保存能线与窗口',files:[{name:'0002-ec-peaks.json',path:'revisions/0002-ec-peaks.json',url:'/saved.json'}]}];state.run.jobs=[];renderJobs();`);
  assert.equal(ui.run("$('job-list').children[0].children[1].children[0].children[0].textContent"),'0002-ec-peaks.json');
});

test('a source configuration response cannot paint a different run after a delayed load', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';const releaseCatalog=deferred();
    api=async path=>path.includes('ec-peak-config')?releaseCatalog.promise:{points:[]};
    const loading=loadEcAnalysis();state.run={...state.run,id:'run-b'};state.ecPeakConfig={spectra:[{energy_key:'new-run'}]};
    releaseCatalog.resolve({spectra:[{energy_key:'old-run'}]});`);
  await ui.run('loading');
  assert.equal(ui.run('state.ecPeakConfig.spectra[0].energy_key'),'new-run');
});

test('a failed selected spectrum leaves the source catalog available for selecting another spectrum', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';
    const sourceCatalog={spectra:[{energy_key:'src_Na22',source_name:'Na22',signal_file:'src_Na22_corr.l1.parquet',lines:[{energy_keV:511,windows:{},centers:{}}]}]};
    api=async path=>{
      if(path.includes('/ec-peak-config')) return sourceCatalog;
      if(path.includes('/ec-overview')) return {points:[{id:'tube-40',stem:'40keV',E:40}]};
      throw new Error('CRC invalid');
    };
    renderEcOverview=async()=>{};renderCurrentStatus=()=>{};setBusy=productionSetBusy;`);
  await assert.rejects(ui.run('loadEcAnalysis()'),/CRC invalid/);
  assert.equal(ui.element('ec-source-workbench').hidden,false);
  assert.equal(ui.element('ec-spectrum-select').options[1].value,'src_Na22');
  assert.equal(!!ui.element('ec-spectrum-select').disabled,false);
  assert.equal(ui.run('state.selecting'),false);
  ui.run(`notify('CRC invalid','error');
    api=async()=>({x:[0,1000],y:[2,3],lo:100,hi:200});
    renderSpectrum=async()=>{};renderSpectrumProvenance=()=>{};
    state.ecOverview.points.push({id:'14B:ec:src_Na22:0:511',stem:'src_Na22',E:511});`);
  await ui.run(`selectEcSource('src_Na22')`);
  assert.equal(ui.run('state.point.stem'),'src_Na22');
  assert.equal(ui.element('message').hidden,true);
});

test('editing the selected source energy keeps the corresponding row selected after save and releases controls', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.channel=1;state.point={id:'14B:ec:src_Test:1:511',stem:'src_Test',E:511};
    state.ecPeakSelectedIndex=0;state.ecPeakDirty=true;state.ecPeakDraft={energy_key:'src_Test',source_name:'Test',lines:[{energy_keV:512,windows:{},centers:{}}]};
    state.ecPeakConfig={spectra:[state.ecPeakDraft]};state.ecOverview={points:[]};
    api=async()=>({spectra:[{energy_key:'src_Test',source_name:'Test',lines:[{energy_keV:512,windows:{},centers:{},point_ids:{'1':'14B:ec:src_Test:1:512'}}]}]});
    renderEcSourceConfig=()=>{};renderJobs=()=>{};`);
  await ui.run('saveEcPeakConfig(false)');
  assert.equal(ui.run('state.point.id'),'14B:ec:src_Test:1:512');
  assert.equal(ui.run('state.point.fit_status'),'not_fitted');
  assert.equal(ui.run('state.point.center'),null);
  assert.equal(ui.element('channel').disabled,false);
  assert.equal(ui.element('run-select').disabled,false);
  assert.equal(ui.run('state.ecPeakDirty'),false);
});

test('source point identities use the backend canonical energy identity when supplied', () => {
  const ui=controller();
  ui.run(`state.ecPeakConfig={spectra:[{energy_key:'src_Test',lines:[{energy_keV:1234.56789,point_ids:{'2':'14B:ec:src_Test:2:1234.57'}}]}]};`);
  assert.equal(ui.run("ecPeakIdentity('src_Test',1234.56789,2)"),'14B:ec:src_Test:2:1234.57');
});

test('saved source windows remain pending despite an existing fit until that energy is refitted', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';
    const oldFit={id:'14B:ec:src_Na22:0:1274.5',stem:'src_Na22',E:1274.5,center:17000,lo:16600,hi:17800,fit_status:'fitted'};
    state.point=oldFit;state.run.pending_ec_windows=[oldFit.id];
    state.run.config.ec_windows={[oldFit.id]:{lo:6500,hi:7900}};
    state.ecOverview={points:[{id:'14B:ec:src_Na22:0:511',stem:'src_Na22',E:511,center:3000,fit_status:'fitted'},oldFit]};
    state.ecPeakConfig={spectra:[{energy_key:'src_Na22',lines:[{energy_keV:511,windows:{}},{energy_keV:1274.5,windows:{'0':{lo:6500,hi:7900}}}]}]};
    renderEcSourceConfig();
    api=async()=>({x:[0,18000],y:[1,20],lo:16600,hi:17800,fit:oldFit});
    renderSpectrum=async()=>{};renderSpectrumProvenance=()=>{};renderEcOverview=async()=>{};renderCurrentStatus=()=>{};`);
  assert.equal(ui.element('ec-peak-rows').children[0].children[4].textContent,'已拟合');
  assert.equal(ui.element('ec-peak-rows').children[1].children[4].textContent,'待重拟合');
  await ui.run('selectEcPoint(oldFit)');
  assert.equal(Number(ui.element('window-lo').value),6500);
  assert.equal(Number(ui.element('window-hi').value),7900);
  assert.equal(ui.element('window-status').textContent,'待重拟合');
  ui.run('state.run.pending_ec_windows=[];renderEcSourceConfig();');
  assert.equal(ui.element('ec-peak-rows').children[1].children[4].textContent,'已拟合');
});

test('full-source view labels only configured ADC windows on the current physical channel', () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.channel=1;state.ecFullSource=true;state.point={stem:'src_Na22',E:511};
    state.ecPeakConfig={spectra:[{energy_key:'src_Na22',lines:[
      {energy_keV:511,windows:{'0':{lo:10,hi:30},'1':{lo:100,hi:180}}},
      {energy_keV:1274.5,windows:{'1':{lo:300,hi:420}}},
      {energy_keV:88,windows:{'0':{lo:50,hi:80}}}
    ]}]};`);
  const overlay=JSON.parse(ui.run('JSON.stringify(ecSourceWindowOverlays())'));
  assert.equal(overlay.annotations.length,2);
  assert.deepEqual(overlay.annotations.map(item=>item.x),[140,360]);
  assert.equal(overlay.shapes.length,1);
  assert.equal(overlay.shapes[0].x0,300);
  assert.match(overlay.annotations[1].text,/1,?274\.5/);
});

test('source workflow maps grouped peak identities to the selected observation and channel', async () => {
  const ui=controller();
  ui.run(`state.channel=1;state.ecOverview={points:[
    {id:'co-1173',stem:'src_Co60',channel:1,signal_file:'src_Co60_40m_corr.l1.parquet'},
    {id:'co-1332',stem:'src_Co60',channel:1,l1_stem:'src_Co60_40m'},
    {id:'other-exposure',stem:'src_Co60',channel:1,signal_file:'src_Co60_10m_corr.l1.parquet'},
    {id:'other-channel',stem:'src_Co60',channel:2,signal_file:'src_Co60_40m_corr.l1.parquet'}
  ]};let submitted=[];
  selectLayer=async layer=>{state.layer=layer;};selectEcPoint=async point=>{state.point=point;};
  executeJob=async(layer,spec)=>{submitted.push({layer,...spec});};`);
  await ui.run(`runPipeline([{layer:'EC_L2',stem:'src_Co60_40m',representative:false}],'ec')`);
  const submitted=JSON.parse(ui.run('JSON.stringify(submitted)'));
  assert.deepEqual(submitted.map(job=>job.point_id),['co-1173','co-1332']);
  assert.ok(submitted.every(job=>job.stem==='src_Co60_40m' && job.channel===1));
});

test('opening a source workflow selects its real filename instead of the first listed source', () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={stem:'src_Co60',signal_file:'src_Co60_40m_corr.l1.parquet'};
  state.run.inventory={raw:{reading_preview:{rows:[
    {kind:'source',stem:'src_Cs137_25m'}, {kind:'source',stem:'src_Co60_40m'}
  ]}}};renderPipelinePlan=()=>{};openPipelineDialog();`);
  assert.equal(ui.element('pipeline-ec-file').value,'src_Co60_40m');
});

test('CRC recovery rebuilds the selected source and background without fitting historical windows', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.channel=1;
  const failedPoint={id:'14B:ec:src_Cs137:1:661.7',stem:'src_Cs137',E:661.7,signal_file:'src_Cs137_25m_corr.l1.parquet'};
  state.run.inventory={raw:{reading_preview:{rows:[
    {kind:'background',stem:'src_bkg_30m'}, {kind:'source',stem:'src_Cs137_25m'}
  ]}}};state.ecOverview={points:[failedPoint]};
  $('spectrum-params').append(node('div','param-item','old peak 17000'));
  $('window-lo').value=16600;$('window-hi').value=17800;$('residual-metric').textContent='RMS 99';
  chartMessage=(id,text)=>{$(id).replaceChildren(node('p','chart-loading',text));};
  api=async()=>{throw new Error('全部事件 CRC 失败');};renderCurrentStatus=()=>{};
  renderEcSourceConfig=()=>{};setBusy=busy=>{state.busy=busy;};`);
  await ui.run('selectEcPoint(failedPoint)');
  assert.equal(ui.element('spectrum-rebuild').textContent,'重建此源');
  assert.equal(ui.run('state.selecting'),false);
  assert.equal(ui.run('state.testError'),undefined);
  assert.equal(ui.element('spectrum-params').children.length,0);
  assert.equal(ui.element('window-lo').value,'');
  assert.equal(ui.element('window-hi').value,'');
  assert.equal(ui.element('window-lo').disabled,true);
  assert.equal(ui.element('window-status').textContent,'CRC 异常');
  assert.equal(ui.element('residual-metric').textContent,'');
  assert.equal(ui.element('residual-chart').children[0].textContent,'');
  ui.run(`let recovered=[];executeJob=async(layer,spec)=>{recovered.push({layer,...spec});return {status:'succeeded'};};
  loadSurfaceSafely=async()=>{};`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(recovered)')), [
    {layer:'EC_L0',stem:'src_bkg_30m',representative:false,channel:1},
    {layer:'EC_L1',stem:'src_bkg_30m',representative:false,channel:1},
    {layer:'EC_L0',stem:'src_Cs137_25m',representative:false,channel:1},
    {layer:'EC_L1',stem:'src_Cs137_25m',representative:false,channel:1}
  ]);
  assert.equal(ui.run('state.point.id'),'14B:ec:src_Cs137:1:661.7');
  assert.equal(ui.run('state.layer'),'EC_L2');
});

test('TB CRC recovery exposes an unpack action for its selected file', async () => {
  const ui=controller();
  ui.run(`state.layer='TB_L0';state.run.inventory={raw:{tb_samples:[{stem:'10C-270-141'}]}};
  const failedPoint={id:'15B:10C-270-141:0',stem:'10C-270-141'};
  api=async()=>{throw new Error('全部事件 CRC 失败');};renderCurrentStatus=()=>{};
  renderL1Workspace=()=>{};highlightSelectedPoint=async()=>{};
  setBusy=busy=>{state.busy=busy;};`);
  await ui.run('selectPoint(failedPoint)');
  assert.equal(ui.element('spectrum-rebuild').textContent,'重新解包');
  assert.equal(ui.run('state.testError'),undefined);
  ui.run(`let unpacked=[];executeJob=async(layer,spec)=>{unpacked.push({layer,...spec});return {status:'succeeded'};};
  loadSurfaceSafely=async()=>{};`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(unpacked)')), [{layer:'TB_L0',stem:'10C-270-141',representative:false,channel:0}]);
});

test('a stale CRC action cannot start jobs for a different run, point or channel', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={id:'old-point',stem:'src_Cs137',signal_file:'src_Cs137_25m_corr.l1.parquet'};
  state.run.inventory={raw:{ec_samples:[{stem:'src_Cs137_25m'}]}};
  renderSpectrumRecovery(new Error('CRC invalid'));let submissions=0;executeJob=async()=>{submissions++;};
  state.channel=2;`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.equal(ui.run('submissions'),0);
  ui.run(`state.channel=0;state.point={id:'another-point',stem:'src_Cs137'};`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.equal(ui.run('submissions'),0);
  ui.run(`state.point={id:'old-point',stem:'src_Cs137'};state.run.id='another-run';`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.equal(ui.run('submissions'),0);
});

test('source recovery stops before the next calculation if its channel changes during a job', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={id:'chosen',stem:'src_Cs137',signal_file:'src_Cs137_25m_corr.l1.parquet'};
  state.run.inventory={raw:{reading_preview:{rows:[{kind:'background',stem:'src_bkg_30m'},{kind:'source',stem:'src_Cs137_25m'}]}}};
  renderSpectrumRecovery(new Error('CRC invalid'));let submissions=[];const release=deferred(),started=deferred();
  executeJob=async(layer,spec)=>{submissions.push({layer,...spec});started.resolve();return release.promise;};
  setBusy=busy=>{state.busy=busy;};`);
  const recovery=ui.element('spectrum-rebuild').listeners.click[0]();
  await ui.run('started.promise');
  ui.run(`state.channel=3;release.resolve({status:'succeeded'});`);
  await recovery;
  assert.equal(ui.run('submissions.length'),1);
  assert.equal(ui.run('state.busy'),false);
});

test('CRC recovery is absent when original data is unavailable or the error is unrelated', () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={id:'chosen',stem:'src_Cs137',signal_file:'src_Cs137_25m_corr.l1.parquet'};`);
  assert.equal(ui.run(`renderSpectrumRecovery(new Error('CRC invalid'))`),false);
  ui.run(`state.run.inventory={raw:{ec_samples:[{stem:'src_Cs137_25m'}]}};`);
  assert.equal(ui.run(`renderSpectrumRecovery(new Error('fit is unavailable'))`),false);
});

test('a failed recovery stops its plan and leaves the selected recovery action available for retry', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={id:'chosen',stem:'src_Cs137',signal_file:'src_Cs137_25m_corr.l1.parquet'};
  state.run.inventory={raw:{reading_preview:{rows:[{kind:'background',stem:'src_bkg_30m'},{kind:'source',stem:'src_Cs137_25m'}]}}};
  renderSpectrumRecovery(new Error('CRC invalid'));let submissions=0;
  executeJob=async()=>{submissions++;throw new Error('read configuration needs review');};
  setBusy=busy=>{state.busy=busy;};`);
  await ui.element('spectrum-rebuild').listeners.click[0]();
  assert.equal(ui.run('submissions'),1);
  assert.equal(ui.run('state.testError'),'read configuration needs review');
  assert.equal(ui.element('spectrum-rebuild').disabled,false);
  assert.equal(ui.run('state.busy'),false);
});

test('focused spectrum scaling ignores offscreen noise and includes the visible curve and negative rate', () => {
  const ui=controller();
  ui.run(`const focusedTraces=[
    {x:[100,7100,7300,7600],y:[1500,-.002,.02,0]},
    {x:[100,7100,7300,7600],y:[2000,0,.025,.001]},
    {visible:'legendonly',x:[7300],y:[3000]}
  ];const originalFocused=JSON.stringify(focusedTraces);`);
  const range=JSON.parse(ui.run('JSON.stringify(visibleSpectrumYRange(focusedTraces,[7000,7800]))'));
  assert.ok(range[0]<-.002);
  assert.ok(range[1]>.025 && range[1]<.04);
  assert.equal(ui.run('JSON.stringify(focusedTraces)===originalFocused'),true);
});

test('focused spectrum scaling accounts for displayed asymmetric error bars only', () => {
  const ui=controller();
  ui.run(`const errors=[
    {x:[100,7200],y:[999,.02],error_y:{array:[500,.03],arrayminus:[500,.04],symmetric:false}},
    {x:[7200],y:[.01],error_y:{visible:false,array:[900]}}
  ];`);
  const range=JSON.parse(ui.run('JSON.stringify(visibleSpectrumYRange(errors,[7000,7500]))'));
  assert.ok(range[0]<-.02);
  assert.ok(range[1]>.05 && range[1]<.07);
});

test('focused positive spectra use Plotly logarithmic coordinates and ignore invalid values', () => {
  const ui=controller();
  ui.run(`const positive=[{x:[0,10,20,30,40],y:[1e9,10,100,0,null]}];`);
  const range=JSON.parse(ui.run('JSON.stringify(visibleSpectrumYRange(positive,[5,45],{logarithmic:true}))'));
  assert.ok(Math.abs(range[0]-.92)<1e-12);
  assert.ok(Math.abs(range[1]-2.12)<1e-12);
  assert.equal(ui.run('visibleSpectrumYRange(positive,[20,20])'),undefined);
  assert.equal(ui.run('visibleSpectrumYRange([{x:[10],y:[null]}],[0,20])'),undefined);
});

test('EC focused rendering preserves signed data while full-source view restores all-data autorange', async () => {
  const ui=controller();
  ui.run(`state.layer='EC_L2';state.point={id:'co60:0:1332.5',stem:'src_Co60',E:1332.5};
    state.spectrum={x:[100,7100,7300,7600],y:[1000,-.002,.02,0],bin_width:4,
      curve:{x:[7100,7300,7600],y:[0,.025,.001],background:[-.001,0,.001]}};
    state.ecPeakConfig={spectra:[{energy_key:'src_Co60',lines:[]}]};
    $('window-lo').value=7000;$('window-hi').value=7700;
    let spectrumPlots=[];Plotly={react:async(chart,traces,layout)=>{spectrumPlots.push({traces,layout});},relayout:async()=>{}};
    renderL1Residual=async()=>{};renderPeakDiagnostics=()=>{};installWindowHandles=()=>{};bindL1Axes=()=>{};updateSpectralModelControls=()=>{};`);
  await ui.run('renderSpectrum()');
  const focused=JSON.parse(ui.run('JSON.stringify(spectrumPlots[0])'));
  assert.deepEqual(focused.traces[0].y,[1000,-.002,.02,0]);
  assert.deepEqual(focused.traces[2].y,[-.001,0,.001]);
  assert.equal(focused.layout.yaxis.type,'linear');
  assert.equal(focused.layout.yaxis.autorange,false);
  assert.ok(focused.layout.yaxis.range[1]<.04);
  ui.run('state.ecFullSource=true;');
  await ui.run('renderSpectrum()');
  const full=JSON.parse(ui.run('JSON.stringify(spectrumPlots[1])'));
  assert.equal(full.layout.yaxis.autorange,true);
  assert.equal(full.layout.yaxis.range,undefined);
  assert.deepEqual(full.traces[0].y,focused.traces[0].y);
});
