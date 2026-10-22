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
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout, clearTimeout, requestAnimationFrame() {}, console, deferred,
  });
  vm.runInContext(readFileSync(path.resolve(__dirname, '../../workbench/static/workbench.js'), 'utf8'), context);
  vm.runInContext(`
    state.run = { id: 'run-a', payload: '14B', dataset: { raw_dir: 'raw-a' }, inventory: {}, config: {}, jobs: [] };
    state.layer = 'TB_L0'; state.channel = 0; state.busy = false; state.loading = false;
    state.loadToken = 0; state.spectrumToken = 0; state.dirty = false;
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
    state.surface = { points: [{ id: 'old', stem: 'old', center: 200, temp: 20, bias: 28 }] };
    const entered = deferred(), release = deferred(); let writes = [];
    Plotly = { react: async chart => { writes.push(chart.id); if (writes.length === 1) { entered.resolve(); await release.promise; } } };
    window.Plotly = Plotly;
    const pending = renderSurface();
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
  await assert.rejects(ui.run('runUnpack()'), /只有历史产物/);
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
  assert.match(ui.run('state.testError'), /扫描并识别/);
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
