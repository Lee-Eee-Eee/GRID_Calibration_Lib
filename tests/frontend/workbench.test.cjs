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

function controller() {
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
    document: { getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], querySelector: element, body: new Element() },
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
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(submitted)')), { layer: 'TB_L1', stem: '101', channel: 0, fit_channels: [0] });
});

test('the main layer action keeps its original layer, file, channel and scope while saving', async () => {
  const ui = controller();
  ui.run(`
    state.layer = 'TB_L1'; state.channel = 0; state.dirty = true;
    state.point = { id: '14B:101:0', stem: '101' }; $('run-scope').value = 'representative';
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

test('L1 residuals use matching histogram bins and finite model counts with Poisson scaling', () => {
  const ui = controller();
  const residual = JSON.parse(ui.run(`JSON.stringify(l1Residual({
    x: [10, 20, 30, 40, 50, 60], y: [9, 0, 4, NaN, 8, 3],
    curve: { x: [10, 20, 30, 40, 45, 50, 60], y: [4, 0, 9, 9, 5, Infinity, 1] }
  }))`));
  assert.deepEqual(residual.x, [10, 20, 30, 60]);
  assert.equal(residual.y.length, 4);
  for (const [index, expected] of [2.5, 0, -5 / 3, 2].entries()) {
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
    { layer: 'TB_L0', stem: '094_observe_50C_290', representative: false },
    { layer: 'TB_L1', stem: '094_observe_50C_290', representative: false },
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
    { layer: 'EC_L2', point_id: 'chosen:2:511', channel: 2, representative: false },
    { layer: 'EC_L2', point_id: 'chosen:2:1275', channel: 2, representative: false },
  ]);
  assert.equal(ui.run('selected.join()'), 'chosen:2:511,chosen:2:1275');
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
