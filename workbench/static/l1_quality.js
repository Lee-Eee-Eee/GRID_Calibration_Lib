/* Saved TB L1 fit diagnostics -> temperature/bias overview. No fitting or quality thresholds. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.L1Quality = api;
})(typeof window === "undefined" ? null : window, function () {
  "use strict";

  const statuses = Object.freeze({
    accepted: { label: "质量通过", color: "#2457a7", symbol: "circle" },
    rejected: { label: "质量拒绝", color: "#b54b37", symbol: "diamond" },
    failed: { label: "拟合失败", color: "#b54b37", symbol: "x" },
    pending_window: { label: "改窗待拟合", color: "#a46c13", symbol: "diamond-open" },
    excluded: { label: "已屏蔽", color: "#a46c13", symbol: "x" },
    not_fitted: { label: "尚未拟合", color: "#929da7", symbol: "circle-open" },
    unknown: { label: "质量未判定", color: "#929da7", symbol: "square-open" },
    model_pending: { label: "拟合方案待定", color: "#929da7", symbol: "square-open" },
  });
  const metrics = Object.freeze({ status: "质量状态", rsquared: "R²", redchi: "约化 χ²" });
  const attentionStatuses = new Set(["rejected", "failed", "pending_window", "not_fitted", "unknown"]);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const escaped = value => String(value).replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const displayNumber = (value, digits = 4) => finite(value) ? value.toFixed(digits) : "—";

  function statusOf(point = {}) {
    const quality = point.fit_quality || {};
    if (point.unsupported_spectral_model || point.fit_status === "model_pending" || quality.status === "model_pending") return "model_pending";
    if (point.error || ["failed", "fit_failed"].includes(point.fit_status) || ["failed", "fit_failed"].includes(quality.status)) return "failed";
    if (point.pending_window) return "pending_window";
    if (point.excluded) return "excluded";
    if (quality.accepted === true) return "accepted";
    if (quality.accepted === false) return "rejected";
    if (point.fit_status === "not_fitted" || !finite(point.center)) return "not_fitted";
    return "unknown";
  }

  const statusLabel = status => statuses[typeof status === "string" ? status : statusOf(status)]?.label || "质量未判定";

  function reasonsOf(point) {
    const quality = point.fit_quality || {};
    const values = [point.unsupported_spectral_model, point.error, quality.reason, ...(Array.isArray(quality.warnings) ? quality.warnings : [])];
    const reasons = values.filter(value => typeof value === "string" && value.trim());
    if (point.pending_window) reasons.unshift("窗口已修改，当前结果尚未按新窗口拟合");
    if (point.excluded) reasons.unshift("该点已屏蔽");
    return [...new Set(reasons)];
  }

  function count(rows) {
    const result = { total: rows.length, plotted: 0, unplaced: 0, attention: 0 };
    for (const status of Object.keys(statuses)) result[status] = 0;
    for (const row of rows) {
      result[row.status] += 1;
      // Exclusion is an independent flag: a failed/edited point stays failed/edited.
      if (row.excluded && row.status !== "excluded") result.excluded += 1;
      result[row.placed ? "plotted" : "unplaced"] += 1;
      if (attentionStatuses.has(row.status)) result.attention += 1;
    }
    return result;
  }

  function hover(row) {
    const quality = row.fit_quality || {};
    return [
      `${escaped(row.stem)} · CH ${row.channel}`,
      `温度 ${displayNumber(row.temp, 3)} °C · 偏压 ${displayNumber(row.bias, 4)} V`,
      `质量：${row.statusLabel}`,
      `R² ${displayNumber(row.rsquared, 5)} · 约化 χ² ${displayNumber(quality.redchi, 4)}`,
      `峰面积信噪比 ${displayNumber(quality.amplitude_snr, 2)} · ΔBIC ${displayNumber(quality.delta_bic_background, 2)}`,
      ...row.reasons.map(reason => escaped(reason)),
      "点击打开能谱复核",
    ].join("<br>");
  }

  function matches(row, query, filter) {
    if (query && !`${row.stem} ${row.temp ?? ""} ${row.bias ?? ""} ${row.statusLabel} ${row.reasons.join(" ")}`.toLocaleLowerCase().includes(query)) return false;
    if (filter === "excluded") return !!row.excluded || row.status === "excluded";
    if (filter === "attention") return attentionStatuses.has(row.status);
    if (filter === "pending") return ["pending_window", "not_fitted", "unknown"].includes(row.status);
    if (filter === "fitted") return row.status === "accepted";
    return filter === "all" || row.status === filter;
  }

  function traceFor(rows, status, metric, scale, showScale, missing = false) {
    const style = statuses[status];
    const numeric = metric !== "status" && !missing;
    return {
      type: "scatter", mode: "markers", name: missing ? `${style.label} · 无${metrics[metric]}` : style.label,
      legendgroup: status, meta: { qualityStatus: status, missingMetric: missing },
      x: rows.map(row => row.temp), y: rows.map(row => row.bias),
      customdata: rows.map(row => row.id), text: rows.map(hover), hovertemplate: "%{text}<extra></extra>",
      marker: {
        size: 10, symbol: style.symbol, opacity: 0.92,
        line: { color: numeric ? "#ffffff" : style.color, width: 1.5 },
        color: numeric ? rows.map(row => row.metricValue) : missing ? "#929da7" : style.color,
        ...(numeric ? { colorscale: scale.colors, cmin: scale.min, cmax: scale.max, showscale: showScale,
          colorbar: { title: { text: metrics[metric], font: { family: "Arial, sans-serif", size: 12 } },
            thickness: 12, len: 0.82, tickfont: { family: "Times New Roman, serif", size: 11 } } } : {}),
      },
    };
  }

  function build(overview = {}, options = {}) {
    const channel = Number.isInteger(options.channel) ? options.channel : 0;
    const metric = Object.hasOwn(metrics, options.metric) ? options.metric : "status";
    const filter = options.filter || "all";
    const query = String(options.query || "").trim().toLocaleLowerCase();
    const allRows = (overview.files || []).map(file => {
      const original = (file.channels || []).find(point => point.channel === channel) || { channel, fit_status: "not_fitted" };
      const status = statusOf(original);
      return {
        ...original, id: original.id || `${overview.payload || ""}:${file.stem}:${channel}`, stem: original.stem || file.stem,
        channel, status, statusLabel: statusLabel(status), reasons: reasonsOf(original),
        placed: finite(original.temp) && finite(original.bias), has_l0: !!file.has_l0,
        metricValue: metric === "rsquared" ? (finite(original.rsquared) ? original.rsquared : null)
          : metric === "redchi" ? (finite(original.fit_quality?.redchi) ? original.fit_quality.redchi : null) : null,
      };
    });
    const rows = allRows.filter(row => matches(row, query, filter));
    const placed = rows.filter(row => row.placed);
    // Keep diagnostic colors comparable when the user narrows status/search.
    const values = allRows.filter(row => row.placed).map(row => row.metricValue).filter(finite);
    const lo = values.length ? Math.min(...values) : 0, hi = values.length ? Math.max(...values) : 1;
    const spread = lo === hi ? Math.max(Math.abs(lo) * 0.01, 1e-6) : 0;
    const colors = [[0, "#b54b37"], [0.5, "#efdfb7"], [1, "#2457a7"]];
    const scale = { min: lo - spread, max: hi + spread, colors: metric === "redchi" ? colors.map(([stop, color]) => [1 - stop, color]).reverse() : colors };
    const traces = [];
    let showScale = true;
    for (const status of Object.keys(statuses)) {
      const group = placed.filter(row => row.status === status);
      if (metric === "status") {
        if (group.length) traces.push(traceFor(group, status, metric, scale, false));
      } else {
        const available = group.filter(row => finite(row.metricValue));
        const missing = group.filter(row => !finite(row.metricValue));
        if (available.length) { traces.push(traceFor(available, status, metric, scale, showScale)); showScale = false; }
        if (missing.length) traces.push(traceFor(missing, status, metric, scale, false, true));
      }
    }
    const selected = rows.find(row => row.id === options.selectedId) || null;
    if (selected?.placed) traces.push({
      type: "scatter", mode: "markers", name: "当前工况", showlegend: false,
      x: [selected.temp], y: [selected.bias], customdata: [selected.id], hoverinfo: "skip",
      marker: { size: 18, color: "#18242e", symbol: "circle-open", line: { color: "#18242e", width: 2 } },
      meta: { selection: true },
    });
    const groups = new Map();
    for (const row of placed) {
      const key = JSON.stringify([row.temp, row.bias]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    return {
      traces, rows, allRows, unplaced: rows.filter(row => !row.placed), selected,
      counts: count(allRows), filteredCounts: count(rows), channel, metric, metricLabel: metrics[metric],
      overlaps: [...groups.values()].filter(group => group.length > 1),
      metricMissing: metric === "status" ? 0 : rows.filter(row => !finite(row.metricValue)).length,
    };
  }

  return Object.freeze({ build, statusOf, statusLabel, reasonsOf, statuses, metrics });
});
