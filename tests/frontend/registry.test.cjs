const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../workbench/static/registry.js'), 'utf8');
const selectIds = new Set(['payload-preset-select', 'payload-preset-version', 'dataset-preset', 'dataset-preset-version', 'dataset-select']);

function controller() {
  const elements = new Map();
  class Element {
    constructor(id) { this.id = id; this.options = []; this.dataset = {}; this.listeners = {}; this._value = ''; this.classList = { toggle() {} }; }
    get value() { return this._value; }
    set value(value) { this._value = selectIds.has(this.id) && !this.options.some(option => option.value === value) ? '' : String(value); }
    set selectedIndex(index) { this._value = this.options[index]?.value || ''; }
    append(...options) { for (const option of options) { this.options.push(option); if (this.options.length === 1) this._value = option.value; } }
    replaceChildren() { this.options = []; this._value = ''; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  }
  const element = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  const context = vm.createContext({
    $: element, Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    clear: el => { el.replaceChildren(); return el; },
    state: { run: { id: 'run-10', payload: '10B' }, datasets: [], busy: false },
    acquisitionDraft: { profile: null, busy: false }, timestamp: String,
    api: async () => ({ presets: [] }),
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`payloadRegistry.presets = [
    { id:'p14', payload:'14B', name:'14B 默认实验读取', latest_version:3, versions:[{version:3},{version:1}] },
    { id:'p10', payload:'10B', name:'10B · 波形读取', latest_version:2, versions:[{version:2},{version:1}] }
  ];`);
  const change = (id, value) => { const el = element(id); el.value = value; for (const listener of el.listeners.change || []) listener({ stopPropagation() {} }); };
  return { run, element, change, options: id => element(id).options.map(option => ({ text: option.text, value: option.value })) };
}

test('preset menus show the current payload and a separate entry for other payloads', () => {
  const ui = controller();
  ui.run(`registryFillPresetChoices('payload-preset-select', '10B', 'p10');`);
  assert.deepEqual(ui.options('payload-preset-select').map(option => option.value), ['', 'p10', '__other_payloads__']);
  assert.equal(ui.options('payload-preset-select')[1].text, '波形读取');
  assert.equal(ui.run(`registryPreset('p10').name`), '10B · 波形读取');
});

test('opening other payload presets retains the selected preset and version', () => {
  const ui = controller();
  ui.run(`registryFillPresetChoices('payload-preset-select', '10B', 'p10'); registryFillVersions('payload-preset-version', registryPreset('p10'), 1);`);
  ui.change('payload-preset-select', '__other_payloads__');
  assert.deepEqual(ui.options('payload-preset-select').map(option => option.value), ['', 'p14', 'p10']);
  assert.equal(ui.element('payload-preset-select').value, 'p10');
  assert.equal(ui.element('payload-preset-version').value, '1');
  assert.equal(ui.options('payload-preset-select')[1].text, '14B / 默认实验读取');
});

test('a payload without matching presets never selects a different payload automatically', () => {
  const ui = controller();
  ui.run(`payloadRegistry.presets = payloadRegistry.presets.filter(preset => preset.payload === '14B'); registryFillPresetChoices('dataset-preset', '10B');`);
  assert.deepEqual(ui.options('dataset-preset').map(option => option.value), ['', '__other_payloads__']);
  assert.equal(ui.element('dataset-preset').value, '');
});

test('a single matching preset defaults to its latest version', () => {
  const ui = controller();
  ui.element('new-run-payload').value = '10B';
  ui.run(`registryDatasetPayloadChanged();`);
  assert.equal(ui.element('dataset-preset').value, 'p10');
  assert.equal(ui.element('dataset-preset-version').value, '2');
});

test('renaming the output payload keeps an explicitly selected reading preset and version', () => {
  const ui = controller();
  ui.run(`registryFillPresetChoices('dataset-preset', '10B', 'p10'); registryFillVersions('dataset-preset-version', registryPreset('p10'), 1);`);
  ui.element('new-run-payload').value = 'TEST-10';
  ui.change('new-run-payload', 'TEST-10');
  assert.equal(ui.element('dataset-preset').value, 'p10');
  assert.equal(ui.element('dataset-preset-version').value, '1');
});

test('a saved directory binding retains its exact preset version when a newer one exists', async () => {
  const ui = controller();
  ui.run(`state.datasets = [{ id:'data-10', payload:'10B', acquisition_preset_match: { status:'matched', reference: {id:'p10', payload:'10B', version:1} } }];`);
  ui.element('dataset-select').append({ value: 'data-10' });
  ui.element('new-run-payload').value = '10B';
  ui.run(`api = async () => ({presets:payloadRegistry.presets});`);
  await ui.run(`registryDatasetSelection();`);
  assert.equal(ui.element('dataset-preset').value, 'p10');
  assert.equal(ui.element('dataset-preset-version').value, '1');
});
