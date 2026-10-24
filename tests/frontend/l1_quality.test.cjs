const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const quality = require('../../workbench/static/l1_quality.js');

function point(stem, overrides = {}) {
  return { id: `10B:${stem}:0`, stem, channel: 0, temp: 20, bias: 28.5, center: 1200,
    rsquared: 0.99, fit_status: 'fitted', fit_quality: { accepted: true, redchi: 1.1 }, ...overrides };
}
function overview(...points) {
  return { payload: '10B', files: points.map(p => ({ stem: p.stem, has_l0: true, channels: [p] })) };
}
const baseTraces = result => result.traces.filter(trace => !trace.meta.selection);

test('browser and CommonJS entry points expose the same pure API', () => {
  const window = {};
  vm.runInNewContext(readFileSync(path.resolve(__dirname, '../../workbench/static/l1_quality.js'), 'utf8'), { window });
  assert.equal(typeof window.L1Quality.build, 'function');
  assert.equal(window.L1Quality.statusLabel('rejected'), quality.statusLabel('rejected'));
});

test('saved acceptance decides quality without deriving a new R-squared threshold', () => {
  const result = quality.build(overview(point('low', { rsquared: -0.4 }), point('high', { fit_quality: { accepted: false, redchi: 6.3, warnings: ['saved reason'] } })));
  assert.equal(result.counts.accepted, 1);
  assert.equal(result.counts.rejected, 1);
  assert.match(result.traces.find(t => t.meta.qualityStatus === 'rejected').text[0], /saved reason/);
  assert.equal(result.rows[0].status, 'accepted');
});

test('failed attempt, edited window and model pending cannot look accepted from an old curve', () => {
  const result = quality.build(overview(
    point('failed', { fit_status: 'failed' }),
    point('pending', { pending_window: true }),
    point('plastic', { fit_status: 'model_pending', fit_quality: { accepted: false } }),
    point('excluded', { excluded: true }),
    point('legacy', { fit_quality: {} }),
    point('new', { fit_status: 'not_fitted', center: null, fit_quality: {} }),
  ));
  assert.deepEqual(result.rows.map(r => r.status), ['failed', 'pending_window', 'model_pending', 'excluded', 'unknown', 'not_fitted']);
  assert.equal(result.counts.accepted, 0);
  assert.equal(result.counts.attention, 4);
});

test('excluded failed and edited points remain selectable under the independent exclusion flag', () => {
  const data = overview(
    point('accepted'),
    point('failed-excluded', { excluded: true, fit_status: 'failed' }),
    point('pending-excluded', { excluded: true, pending_window: true }),
    point('accepted-excluded', { excluded: true }),
  );
  const result = quality.build(data, { filter: 'excluded' });
  assert.equal(result.counts.total, 4);
  assert.equal(result.counts.excluded, 3);
  assert.equal(result.counts.accepted, 1);
  assert.equal(result.counts.failed, 1);
  assert.equal(result.counts.pending_window, 1);
  assert.equal(result.filteredCounts.total, 3);
  assert.equal(result.filteredCounts.excluded, 3);
  assert.equal(result.filteredCounts.accepted, 0);
  assert.deepEqual(result.rows.map(row => row.status), ['failed', 'pending_window', 'excluded']);
  assert.deepEqual(baseTraces(result).flatMap(trace => trace.customdata).sort(), [
    '10B:accepted-excluded:0', '10B:failed-excluded:0', '10B:pending-excluded:0',
  ]);
  assert.ok(result.rows.every(row => row.reasons.includes('该点已屏蔽')));
  assert.equal(quality.build(data, { filter: 'excluded', query: 'pending' }).rows[0].status, 'pending_window');
});

test('null, strings, NaN and Infinity coordinates are retained for review without being plotted as zero', () => {
  const result = quality.build(overview(point('null', { temp: null }), point('string', { bias: '28.5' }), point('nan', { temp: NaN }), point('inf', { bias: Infinity }), point('zero', { temp: 0, bias: 0 })));
  assert.equal(result.counts.total, 5);
  assert.equal(result.counts.plotted, 1);
  assert.equal(result.unplaced.length, 4);
  assert.deepEqual(result.traces[0].x, [0]);
  assert.deepEqual(result.traces[0].y, [0]);
});

test('missing numerical diagnostics have a distinct trace and never become zero', () => {
  const result = quality.build(overview(point('missing', { rsquared: null }), point('valid', { rsquared: -0.2 })), { metric: 'rsquared' });
  assert.equal(result.metricMissing, 1);
  assert.equal(result.rows[0].metricValue, null);
  const missing = result.traces.find(t => t.meta.missingMetric);
  assert.deepEqual(missing.customdata, ['10B:missing:0']);
  assert.equal(missing.marker.showscale, undefined);
  assert.deepEqual(result.traces.find(t => !t.meta.missingMetric).marker.color, [-0.2]);
});

test('reduced chi-square uses saved redchi and one shared scale across status traces', () => {
  const result = quality.build(overview(point('a'), point('b', { fit_quality: { accepted: false, redchi: 6.3 } })), { metric: 'redchi' });
  assert.equal(result.metricLabel, '约化 χ²');
  assert.deepEqual(baseTraces(result).map(t => t.marker.cmin), [1.1, 1.1]);
  assert.deepEqual(baseTraces(result).map(t => t.marker.cmax), [6.3, 6.3]);
  assert.equal(result.traces.filter(t => t.marker.showscale).length, 1);
});

test('status and search filters keep the full current-channel diagnostic color range', () => {
  const data = overview(point('accepted-low', { fit_quality: { accepted: true, redchi: 0.7 } }),
    point('rejected-middle', { fit_quality: { accepted: false, redchi: 4.1 } }),
    point('accepted-high', { fit_quality: { accepted: true, redchi: 7.2 } }),
    point('unplaced', { temp: null, fit_quality: { accepted: false, redchi: 900 } }));
  const full = quality.build(data, { metric: 'redchi' });
  const filtered = quality.build(data, { metric: 'redchi', filter: 'attention', query: 'rejected-middle' });
  assert.equal(filtered.filteredCounts.total, 1);
  assert.equal(filtered.traces[0].marker.cmin, full.traces[0].marker.cmin);
  assert.equal(filtered.traces[0].marker.cmax, full.traces[0].marker.cmax);
  assert.equal(filtered.traces[0].marker.cmin, 0.7);
  assert.equal(filtered.traces[0].marker.cmax, 7.2);
});

test('equal temperature-bias observations retain distinct stable IDs and exact measured coordinates', () => {
  const result = quality.build(overview(point('a'), point('b')), { selectedId: '10B:b:0' });
  assert.deepEqual(result.traces[0].x, [20, 20]);
  assert.deepEqual(result.traces[0].y, [28.5, 28.5]);
  assert.deepEqual(result.traces[0].customdata, ['10B:a:0', '10B:b:0']);
  assert.equal(result.overlaps.length, 1);
  assert.equal(result.selected.id, '10B:b:0');
  assert.deepEqual(result.traces.at(-1).customdata, ['10B:b:0']);
});

test('channel selection finds channel identity rather than assuming list position', () => {
  const data = overview(point('a'));
  data.files[0].channels = [point('a', { channel: 2, id: '10B:a:2', temp: -10, bias: 27.1 })];
  assert.equal(quality.build(data, { channel: 2 }).rows[0].id, '10B:a:2');
  assert.equal(quality.build(data, { channel: 0 }).rows[0].status, 'not_fitted');
});

test('search and current-channel status filtering leave overall counts intact', () => {
  const data = overview(point('a'), point('b', { fit_quality: { accepted: false } }), point('c', { pending_window: true }));
  const result = quality.build(data, { query: 'b', filter: 'attention' });
  assert.equal(result.counts.total, 3);
  assert.equal(result.filteredCounts.total, 1);
  assert.equal(result.rows[0].stem, 'b');
});

test('a selected point outside filters is not silently drawn as an unfiltered result', () => {
  const result = quality.build(overview(point('a'), point('b', { fit_quality: { accepted: false } })), { selectedId: '10B:a:0', filter: 'rejected' });
  assert.equal(result.selected, null);
  assert.deepEqual(result.traces.flatMap(t => t.customdata), ['10B:b:0']);
});

test('build preserves original inputs, escapes saved text and includes saved rejection diagnostics', () => {
  const data = overview(point('<spectrum>', { fit_quality: { accepted: false, warnings: ['<unsafe>'], redchi: 6.3, amplitude_snr: 4, delta_bic_background: 10 } }));
  const snapshot = JSON.stringify(data);
  const result = quality.build(data);
  assert.equal(JSON.stringify(data), snapshot);
  assert.match(result.traces[0].text[0], /&lt;spectrum&gt;/);
  assert.match(result.traces[0].text[0], /&lt;unsafe&gt;/);
  assert.match(result.traces[0].text[0], /约化 χ² 6.3000/);
});

test('empty data and unknown metric have a finite, empty overview', () => {
  const result = quality.build({}, { metric: 'not-a-metric' });
  assert.equal(result.metric, 'status');
  assert.equal(result.counts.total, 0);
  assert.deepEqual(result.traces, []);
});
