/* Local GRID workbench. Scientific computation lives in the Python processors. */
"use strict";

const $ = (id) => document.getElementById(id);
const layers = [
  { id: "TB_L0", title: "原始数据解析", heading: "TB 原始数据解析", description: "解析事件与遥测，生成按工况组织的标准数据。", input: "原始 .dat / .hk", output: "L0 Parquet · HK metadata", dependency: "原始事件与遥测", process: "原始事件解包、通道与工况关联" },
  { id: "TB_L1", title: "能谱峰拟合", heading: "TB 能谱峰拟合", description: "单高斯峰 + 单背景模型，复核窗口与峰位。", input: "TB L0 · 工况谱", output: "L1 拟合 JSON · 能谱图", dependency: "TB L0 + 当前窗口修订", process: "四种单背景模型择优，输出峰位及误差" },
  { id: "TB_L2", title: "曲面拟合", heading: "TB 温度–偏压曲面", description: "使用各工况峰位和误差，拟合五参数物理响应曲面。", input: "TB L1 · 峰位 / 误差", output: "L2 参数 JSON · 曲面图", dependency: "TB L1 + 当前屏蔽点", process: "按 center_err 加权拟合 G₀、k、V₀、b、c" },
  { id: "EC_L0", title: "原始数据解析", heading: "EC 原始数据解析", description: "解析 X 射线、放射源及背景数据，保存逐脉冲工况。", input: "X 射线 / 源 / 本底原始数据", output: "L0 Parquet · 逐脉冲 T / V", dependency: "原始事件与遥测", process: "读取真实能线数据，关联逐脉冲温度与偏压" },
  { id: "EC_L1", title: "TB 响应校正", heading: "EC 温度–偏压校正", description: "显式记录本次使用的 TB 参数版本与校正依赖。", input: "EC L0 + 固定 TB L2 参数", output: "L1 校正谱 · tb_correction", dependency: "EC L0 + TB L2 版本", process: "校正至参考工况 25 °C / 28.5 V" },
  { id: "EC_L2", title: "能线峰拟合", heading: "EC 能线峰拟合", description: "拟合校正后的 X 射线与放射源能线，生成能量–峰位点。", input: "EC L1 · 校正谱", output: "L2 峰位 JSON · 能线拟合图", dependency: "EC L1 + 源能线配置", process: "单峰拟合、曝光归一化和本底处理" },
  { id: "EC_L3", title: "能量标定", heading: "EC 能量刻度与分辨率", description: "复用现行 E=f(ADC) 全局拟合；低 / 高能段当前使用同一组系数。", input: "EC L2 · 已知能量与峰位", output: "L3 标定 JSON · 分辨率图", dependency: "EC L2 + 对应 TB 版本", process: "现行 E=f(ADC) 全局二次拟合；方向与 wiki 分段规范有差异，详见算法说明" }
];
const state = { bootstrap: null, run: null, layer: "TB_L2", channel: 0, surface: null, spectrum: null, point: null, busy: false, loading: false, dirty: false, openToken: 0, loadToken: 0, spectrumToken: 0, ecToken: 0, pollToken: 0, tab: "artifacts", analysisView: "residual", expandedJobs: new Set() };
const statusNames = { idle: "未运行", pending: "未运行", not_run: "未运行", queued: "排队中", running: "运行中", succeeded: "计算成功", success: "计算成功", completed: "计算成功", failed: "计算失败", stale: "依赖待更新", historical: "仅历史参考" };
const plotConfig = { responsive: true, displaylogo: false, showLink: false, modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines", "sendDataToCloud", "sendChartToCloud"], toImageButtonOptions: { format: "png", scale: 2 } };
const chartFont = { family: '"Segoe UI", "Microsoft YaHei", sans-serif', size: 12, color: "#586572" };
const chartColors = { ink: "#34414d", blue: "#2457a7", fit: "#b54b37", excluded: "#a46c13", muted: "#929da7", grid: "#e3e7eb", border: "#c4ccd4" };
const axisStyle = { tickfont: chartFont, gridcolor: chartColors.grid, linecolor: chartColors.border, showline: true, ticks: "outside", ticklen: 3, tickcolor: chartColors.border, zeroline: false, automargin: true };
const chartHover = { bgcolor: "#ffffff", bordercolor: "#c4ccd4", font: { ...chartFont, color: "#18242e" }, align: "left" };

function node(tag, className, value) { const el = document.createElement(tag); if (className) el.className = className; if (value !== undefined && value !== null) el.textContent = String(value); return el; }
function clear(el) { el.replaceChildren(); return el; }
function valueText(value) { return value === null || value === undefined ? "—" : Array.isArray(value) ? value.join(" · ") : typeof value === "object" ? JSON.stringify(value) : String(value); }
function versionText(value) { return !value ? "历史基线" : typeof value === "object" ? `${value.filename || value.version || "TB"}${value.sha256 ? ` · ${value.sha256.slice(0, 8)}` : ""}` : String(value); }
function number(value, digits = 4) { if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—"; const n = Number(value); return Math.abs(n) > 0 && (Math.abs(n) < .001 || Math.abs(n) >= 1e6) ? n.toExponential(3) : n.toLocaleString("en-US", { maximumFractionDigits: digits }); }
function windowValue(id) { const input = $(id); return Number(input.dataset.edited !== "true" && input.dataset.preciseValue !== undefined ? input.dataset.preciseValue : input.value); }
function displayWindowValue(id, value) { const input = $(id); input.dataset.edited = "false"; if (value !== "" && value !== null && value !== undefined && Number.isFinite(Number(value))) { input.dataset.preciseValue = String(value); input.value = Number(value).toFixed(3); input.title = `原始精度：${value} ADC`; } else { delete input.dataset.preciseValue; input.value = ""; input.title = ""; } }
function timestamp(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }); }
function normalizedStatus(status) { return ["success", "completed"].includes(status) ? "succeeded" : status || "idle"; }
function badge(status, label) { const s = normalizedStatus(status); return node("span", `badge ${["succeeded", "running", "queued", "failed", "stale"].includes(s) ? s : "neutral"}`, label || statusNames[s] || s); }
function notify(message, type = "info") { $("message").textContent = message; $("message").className = `notice ${type}`; $("message").hidden = false; if ($("dismiss-message")) $("dismiss-message").hidden = false; }
function messageError(error) { notify(error.message || String(error), "error"); }
function activeLayer() { return layers.find((item) => item.id === state.layer); }
function runPath(suffix = "") { return `/api/runs/${encodeURIComponent(state.run.id)}${suffix}`; }
function paramsFor(value) { if (!value) return {}; if (Array.isArray(value)) return value[state.channel] || {}; return value[state.channel] && typeof value[state.channel] === "object" ? value[state.channel] : value; }
function pointLabel(point) { return `${point.stem} · ${number(point.temp, 1)} °C / ${number(point.bias, 3)} V${isExcluded(point) ? " · 已屏蔽" : point.used === false ? " · 质量筛除" : ""}`; }
function excludedIds() { return state.run?.config?.excluded || []; }
function isExcluded(point) { return !!point?.excluded || excludedIds().includes(point?.id); }
function isUsed(point) { return !isExcluded(point) && point.used !== false; }
function artifactsForLayer() {
  const jobOrder = new Map((state.run?.jobs || []).map((job, index) => [job.id, index]));
  const generatedOrder = new Map((state.run?.generated?.[state.layer] || []).map((item) => [item.path, jobOrder.get(item.job_id) ?? -1]));
  return (state.run?.artifacts || []).filter((item) => (item.layer || item.stage || "").toUpperCase().replace(" ", "_") === state.layer)
    .sort((a, b) => Number(!!b.computed) - Number(!!a.computed) || (generatedOrder.get(b.path) ?? -1) - (generatedOrder.get(a.path) ?? -1) || String(b.name).localeCompare(String(a.name)));
}

async function api(path, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let response;
  try { response = await fetch(path, init); } catch { throw new Error("本地处理服务连接中断，请确认服务仍在运行，恢复后点击刷新。"); }
  let result;
  try { result = await response.json(); } catch { throw new Error(`服务返回无法解析的响应（HTTP ${response.status}）。`); }
  if (!response.ok) throw new Error(valueText(result.detail || result.error || result.message || `HTTP ${response.status}`));
  return result;
}

function renderNavigation() {
  const nav = clear($("stage-nav"));
  for (const family of ["TB", "EC"]) {
    const group = node("div", "nav-group");
    group.append(node("h2", "nav-group-label", family === "TB" ? "TB · 温度–偏压" : "EC · 能量标定"));
    for (const layer of layers.filter((item) => item.id.startsWith(family))) {
      const button = node("button", `stage-nav-button${state.layer === layer.id ? " active" : ""}`);
      button.type = "button"; button.dataset.layer = layer.id; button.setAttribute("aria-current", state.layer === layer.id ? "step" : "false");
      button.append(node("span", "nav-layer", layer.id.slice(-2)), node("span", "", layer.title));
      const stage = state.run?.stages?.[layer.id];
      const navStatus = stage?.stale ? "stale" : normalizedStatus(stage?.status);
      const statusLabel = statusNames[navStatus] || "未运行";
      button.title = `${layer.id} · ${layer.title} · ${statusLabel}`;
      button.setAttribute("aria-label", `${layer.id} ${layer.title}，状态：${statusLabel}`);
      const dot = node("span", `nav-status ${navStatus}`); dot.title = statusLabel; dot.setAttribute("aria-hidden", "true"); button.append(dot);
      if (state.run) { const shortStatus = { succeeded: "已计算", failed: "失败", stale: "待更新", running: "运行中", queued: "排队中" }; button.append(node("span", "nav-status-text", shortStatus[navStatus] || "未运行")); }
      button.addEventListener("click", () => selectLayer(layer.id)); group.append(button);
    }
    nav.append(group);
  }
}

function renderRunOptions() {
  const select = clear($("run-select")); select.append(new Option("选择已保存试算", ""));
  for (const run of state.bootstrap?.runs || []) select.append(new Option(`${run.payload} · ${run.name || run.id}`, run.id));
  select.value = state.run?.id || "";
}

function renderStage() {
  if (!state.run) return;
  ++state.ecToken;
  const layer = activeLayer(); const stage = state.run.stages?.[layer.id] || {};
  $("stage-number").textContent = layer.id.slice(-2);
  $("stage-title").textContent = layer.heading;
  $("stage-description").textContent = stage.purpose || layer.description;
  $("stage-input").textContent = valueText(stage.input || layer.input);
  $("stage-output").textContent = valueText(stage.output || layer.output);
  $("stage-dependency").textContent = valueText(stage.dependencies || layer.dependency);
  const status = stage.stale && normalizedStatus(stage.status) === "succeeded" ? "stale" : normalizedStatus(stage.status);
  $("stage-status").replaceWith(Object.assign(badge(status), { id: "stage-status" }));
  const validation = stage.validation;
  $("validation-status").replaceWith(Object.assign(badge(validation?.status === "passed" ? "succeeded" : validation?.status === "failed" ? "failed" : "idle", validation?.status === "passed" ? "校验通过" : validation?.status === "failed" ? "校验未通过" : "尚未校验"), { id: "validation-status" }));
  const hasCrcFailure = validation?.files?.some((item) => !item.valid && /crc/i.test(item.details || ""));
  $("validation-detail").textContent = validation ? `${validation.passed ?? 0} / ${validation.total ?? 0} 文件通过${hasCrcFailure ? " · CRC 检查未通过" : ""} · 详见运行记录` : "计算与校验分别记录；历史存在不代表规范通过";
  $("stage-counts").textContent = `历史 ${stage.history_count ?? 0} · 本试算 ${stage.current_count ?? 0}`;
  $("tb-analysis").hidden = !layer.id.startsWith("TB");
  $("stage-analysis").hidden = layer.id.startsWith("TB");
  $("stage-overview-title").textContent = layer.heading;
  $("stage-overview-caption").textContent = "处理依赖与产物预览";
  const flow = clear($("stage-flow"));
  [{ label: "输入", text: layer.input }, { label: "处理", text: layer.process }, { label: "输出", text: layer.output }].forEach((item, index) => {
    if (index) flow.append(node("span", "flow-arrow", "→"));
    const card = node("div", "flow-node"); card.append(node("strong", "", item.label), node("span", "", item.text)); flow.append(card);
  });
  renderDependencies(); renderArtifacts();
  if (["EC_L0", "EC_L1"].includes(layer.id)) {
    loadEcSpectrum().catch((error) => { if (state.layer === layer.id) clear($("artifact-preview")).append(node("p", "", error.message)); });
  } else if (!layer.id.startsWith("TB")) {
    const image = artifactsForLayer().find((item) => /\.(png|jpe?g|svg)$/i.test(item.name || item.path));
    if (image) previewArtifact(image); else clear($("artifact-preview")).append(node("p", "", "本层暂无可预览的图表。可运行本层，或查看下方 JSON / Parquet 产物。"));
  }
}

function renderDependencies() {
  const dependency = state.run.ec_dependency;
  const current = state.run.tb_version;
  let text = dependency ? versionText(dependency.tb_version || dependency.version || dependency.tb_corrfile || dependency) : "尚未生成本次 EC 校正";
  $("ec-version").textContent = `本次 EC L1 绑定：${text}`;
  $("ec-version").title = text;
  $("tb-version").textContent = `版本 ${versionText(current)}`;
  $("tb-version").title = versionText(current);
  const banner = $("dependency-banner");
  banner.hidden = !state.run.dependency_stale;
  banner.textContent = `TB 参数或配置已变化。本次 EC L1 绑定 TB ${text}，请先更新 TB，再从 EC L1 重算。`;
  if (state.layer.startsWith("EC")) {
    banner.hidden = false; banner.className = "dependency-banner";
    banner.textContent = `本次 EC L1 绑定 TB ${text}。${state.run.ec_coverage?.description || "能峰文件可能沿用历史来源。"}${state.run.dependency_stale ? " TB 依赖已变化，请先更新 TB 并从 EC L1 重算。" : ""}`;
  }
  const coverage = state.run.ec_coverage;
  const details = $("ec-coverage-details"); details.hidden = !coverage?.sources?.length;
  const holder = clear($("ec-coverage-list"));
  if (coverage?.sources?.length) {
    const table = node("table"); const head = node("tr");
    ["能峰文件", "来源", "TB 参数 / 版本", "SHA-256"].forEach((label) => head.append(node("th", "", label))); table.append(head);
    for (const source of coverage.sources) {
      const row = node("tr"); const filename = String(source.file).split("/").pop();
      const nameCell = node("td", "", filename); nameCell.title = source.file;
      const origin = node("td"); origin.append(badge(source.current_tb ? "succeeded" : "stale", source.current_tb ? "当前 TB" : source.origin === "computed" ? "旧 TB" : "历史能峰"));
      const versionCell = node("td", "", source.provenance?.tb_version ? versionText(source.provenance.tb_version) : "历史记录未绑定本次 TB");
      const hashCell = node("td", "", source.sha256?.slice(0, 10) || "—"); hashCell.title = source.sha256 || "";
      row.append(nameCell, origin, versionCell, hashCell); table.append(row);
    }
    holder.append(table);
  }
}

function renderRun() {
  const run = state.run;
  $("empty-state").hidden = !!run; $("run-workspace").hidden = !run;
  $("save-run").disabled = !run || state.busy;
  if (!run) { renderNavigation(); return; }
  $("payload").value = run.payload; $("run-identity").textContent = `runs / ${run.id}`;
  $("saved-at").textContent = run.updated_at ? `保存于 ${timestamp(run.updated_at)}` : "试算已持久保存";
  const notes = clear($("scientific-notes"));
  for (const note of run.scientific_notes || []) notes.append(node("p", "", note));
  renderRunOptions(); renderNavigation(); renderStage(); renderJobs(); setBusy(state.busy);
}

function renderArtifacts() {
  const items = artifactsForLayer(); const list = clear($("artifact-list"));
  $("artifact-count").textContent = items.length;
  if (!items.length) { list.append(node("p", "empty-list", "本层暂无产物；运行处理器后会显示真实输出。")); return; }
  for (const item of items) {
    const row = node("div", "artifact-row"); const name = item.name || String(item.path).split(/[\\/]/).pop();
    const extension = name.includes(".") ? name.split(".").pop().toUpperCase() : "FILE";
    row.append(node("span", "file-icon", extension.slice(0, 4)));
    const description = node("div", "file-description");
    if (/\.(png|jpe?g|svg)$/i.test(name)) { const link = node("button", "", name); link.type = "button"; link.addEventListener("click", () => previewArtifact(item)); description.append(link); }
    else { const link = node("a", "", name); link.href = item.url || "#"; link.target = "_blank"; link.rel = "noopener"; description.append(link); }
    description.append(node("small", "", item.path || "")); row.append(description);
    const historical = item.historical === true || item.origin === "history" || ["history", "historical"].includes(item.source) || item.kind === "historical" || item.current === false;
    row.append(badge(historical ? "historical" : "idle", historical ? "历史参考" : item.origin === "seed" || item.seeded ? "基线副本" : "本次产物")); list.append(row);
  }
}

function previewArtifact(item) {
  const preview = clear($("artifact-preview")); const image = document.createElement("img");
  image.src = item.url; image.alt = item.name || item.path || "标定产物图"; image.loading = "lazy";
  image.addEventListener("error", () => { clear(preview).append(node("p", "", "图表未能加载，请从产物链接检查文件。")); });
  preview.append(image);
  if (state.layer.startsWith("TB")) { const link = document.createElement("a"); link.href = item.url; link.target = "_blank"; link.rel = "noopener"; link.click(); }
}

function renderJobs() {
  const jobs = [...(state.run?.jobs || [])].reverse(); const list = clear($("job-list"));
  $("job-count").textContent = jobs.length;
  if (!jobs.length) { list.append(node("p", "empty-list", "尚未运行处理器。历史产物不会标记为本次成功。")); return; }
  for (const job of jobs) {
    const row = node("div", "job-row"); const summary = node("div", "job-summary");
    summary.append(node("strong", "job-layer", job.layer || "任务"), badge(job.status), node("span", "tiny-text", job.stem || job.config?.only?.join(", ") || (job.scope === "all" || job.representative === false ? "全部数据" : "代表样例")), node("time", "job-time", timestamp(job.finished_at || job.started_at || job.created_at)));
    row.append(summary);
    if (job.message || job.result?.message) row.append(node("div", "job-details", valueText(job.message || job.result.message)));
    if (job.error) row.append(node("div", "job-details job-error", valueText(job.error)));
    if (job.validation) row.append(node("div", "job-details", `规范校验：${job.validation.status === "passed" ? "通过" : job.validation.status === "failed" ? "未通过" : "未完成"} · ${job.validation.passed ?? 0} / ${job.validation.total ?? 0} 文件`));
    if (job.result?.warnings?.length) row.append(node("div", "job-details", `处理器提示：${valueText(job.result.warnings)}`));
    const details = node("details", "job-details");
    const content = node("pre", "job-log");
    const checks = job.validation?.files?.map((item) => `${item.valid ? "PASS" : "FAIL"} ${item.file}\n${item.details || ""}`).join("\n") || "";
    content.textContent = [checks, job.log || job.logs || "展开后读取处理器日志…"].filter(Boolean).join("\n\n");
    details.append(node("summary", "", "校验详情与处理器日志"), content);
    details.open = state.expandedJobs.has(job.id);
    details.addEventListener("toggle", async () => {
      if (details.open) {
        state.expandedJobs.add(job.id);
        try { const latest = await api(runPath(`/jobs/${encodeURIComponent(job.id)}`)); content.textContent = [checks, latest.log || "本任务暂无处理器输出。"].filter(Boolean).join("\n\n"); } catch (error) { content.textContent = `${checks}\n日志读取失败：${error.message}`; }
      } else state.expandedJobs.delete(job.id);
    });
    row.append(details);
    list.append(row);
  }
}

function renderParams() {
  const data = state.surface || {}; const current = paramsFor(data.params); const baseline = paramsFor(data.baseline?.params || state.run.baseline_surface?.params);
  const table = node("table"); const head = node("thead"); const hr = node("tr");
  ["参数", "初始基线", "当前试算", "变化"].forEach((text) => hr.append(node("th", "", text))); head.append(hr); table.append(head);
  const body = node("tbody");
  for (const key of ["G0", "k", "V0", "b", "c"]) {
    const row = node("tr"); const old = baseline[key]; const now = current[key]; const diff = Number.isFinite(Number(old)) && Number.isFinite(Number(now)) && Number(old) !== 0 ? 100 * (now - old) / Math.abs(old) : null;
    row.append(node("td", "", key === "G0" ? "G₀" : key === "V0" ? "V₀" : key), node("td", "", number(old, 6)), node("td", "", number(now, 6)), node("td", diff === null ? "" : Math.abs(diff) < 1e-8 ? "" : diff > 0 ? "positive" : "negative", diff === null ? "—" : `${diff > 0 ? "+" : ""}${number(diff, 3)}%`)); body.append(row);
  }
  table.append(body); clear($("parameter-table")).append(table);
  const diag = paramsFor(data.diagnostics); const diagnostics = clear($("fit-diagnostics"));
  const entries = [["参与点", (data.points || []).filter(isUsed).length], ["RMSE", diag.rmse], ["MAPE", diag.mape_percent], ["χ² / dof", diag.chi2_reduced]];
  entries.forEach(([key, value]) => { const item = node("span", "", key); item.append(node("strong", "", `${number(value, 3)}${key === "MAPE" && value !== undefined ? "%" : ""}`)); diagnostics.append(item); });
}

function bindPointClicks(chart) {
  if (typeof chart.removeAllListeners === "function") chart.removeAllListeners("plotly_click");
  chart.on("plotly_click", (event) => {
    const sample = event.points.find((point) => point.customdata);
    if (sample) {
      const id = typeof sample.customdata === "object" ? sample.customdata[0] : sample.customdata;
      const point = state.surface.points.find((p) => p.id === id || p.stem === id);
      if (point) selectPoint(point).catch(messageError);
    }
  });
}

function surfaceViewport() {
  const rect = $("surface-chart").getBoundingClientRect();
  const mode = rect.width < 460 || rect.height < 270 ? "compact" : rect.width < 650 ? "medium" : "wide";
  const eye = mode === "compact" ? { x: 1.95, y: -2.15, z: 1.25 } : mode === "medium" ? { x: 1.65, y: -1.8, z: 1.05 } : { x: 1.35, y: -1.45, z: .95 };
  return { mode, camera: { eye, center: { x: 0, y: 0, z: mode === "wide" ? -.24 : 0 } } };
}

async function renderSurface() {
  if (!window.Plotly) throw new Error("本地 Plotly 资源未加载，请检查 /static/plotly.min.js。");
  const data = state.surface; const points = data.points || []; const traces = [];
  if (data.grid?.gain?.length) traces.push({ type: "surface", x: data.grid.temp, y: data.grid.bias, z: data.grid.gain, colorscale: [[0, "#e2e8f0"], [.5, "#b5c5d7"], [1, "#718eaf"]], opacity: .6, showscale: false, hoverinfo: "skip", contours: { z: { show: false } }, lighting: { ambient: .85, diffuse: .55, roughness: 1, specular: .05 }, name: "当前拟合曲面" });
  const groups = [{ excluded: false, color: chartColors.blue, name: "参与拟合", matches: isUsed }, { excluded: true, color: chartColors.excluded, name: "人工屏蔽", matches: isExcluded }, { excluded: false, color: chartColors.muted, name: "质量筛除", matches: (point) => !isExcluded(point) && point.used === false }];
  for (const group of groups) {
    const values = points.filter(group.matches);
    traces.push({ type: "scatter3d", mode: "markers", x: values.map((p) => p.temp), y: values.map((p) => p.bias), z: values.map((p) => p.center), customdata: values.map((p) => p.id || p.stem), text: values.map((p) => `${p.stem}<br>${group.name}<br>峰位 ${number(p.center, 2)} ADC`), hovertemplate: "%{text}<br>T = %{x:.2f} °C · V = %{y:.3f} V<extra></extra>", marker: { size: 4, color: group.color, line: { color: "#ffffff", width: .8 }, opacity: 1, symbol: group.excluded ? "x" : "circle" }, name: group.name });
  }
  traces.push(selectedPointTrace(true));
  const axis = (text) => ({ title: { text, font: chartFont }, tickfont: chartFont, gridcolor: "#dfe4e9", zeroline: false, linecolor: "#c4ccd4", backgroundcolor: "#fafbfc", showbackground: true, showspikes: false });
  const viewport = surfaceViewport();
  $("surface-chart").dataset.cameraMode = viewport.mode;
  await Plotly.react($("surface-chart"), traces, { margin: { l: 16, r: 16, t: 0, b: 38 }, paper_bgcolor: "#ffffff", font: chartFont, hoverlabel: chartHover, showlegend: false, uirevision: `${state.run.id}-${state.channel}-${viewport.mode}`, scene: { xaxis: axis("T / °C"), yaxis: axis("Bias / V"), zaxis: axis("峰位 / ADC"), camera: viewport.camera, aspectratio: { x: 1.5, y: 1, z: .8 } } }, plotConfig);
  bindPointClicks($("surface-chart"));
  const residualTraces = [];
  for (const group of groups) {
    const values = points.filter(group.matches);
    residualTraces.push({ type: "scatter", mode: "markers", x: values.map((p) => p.temp), y: values.map((p) => residualPercent(p)), customdata: values.map((p) => p.id || p.stem), text: values.map((p) => `${p.stem}<br>${group.name} · ${number(p.bias, 3)} V`), marker: { color: group.color, size: 7, opacity: .95, symbol: group.excluded ? "x" : "circle", line: { color: "#fff", width: .8 } }, hovertemplate: "%{text}<br>温度 %{x:.2f} °C · 残差 %{y:.3f}%<extra></extra>", name: group.name });
  }
  residualTraces.push(selectedPointTrace(false));
  await Plotly.react($("residual-chart"), residualTraces, { margin: { l: 64, r: 22, t: 16, b: 43 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, xaxis: { ...axisStyle, title: { text: "温度 / °C", standoff: 6 } }, yaxis: { ...axisStyle, title: { text: "残差 / %", standoff: 8 }, zeroline: true, zerolinecolor: "#9daab6", zerolinewidth: 1, rangemode: "tozero" }, hovermode: "closest", uirevision: `${state.run.id}-${state.channel}` }, plotConfig);
  bindPointClicks($("residual-chart"));
  const active = points.filter(isUsed); const residuals = active.map(residualPercent).filter(Number.isFinite);
  $("residual-metric").textContent = residuals.length ? `MAPE ${number(residuals.reduce((sum, val) => sum + Math.abs(val), 0) / residuals.length, 2)}%` : "—";
  $("point-count").textContent = `${active.length} / ${points.length} 参与拟合`;
  $("surface-caption").textContent = `${state.run.payload} / CH ${state.channel} · 选择测量点查看能谱`;
  if (data.dirty) $("surface-caption").textContent += " · 配置已变化，曲面待重算";
  renderParams();
}

function selectedPointTrace(threeDimensional) {
  return { type: threeDimensional ? "scatter3d" : "scatter", mode: "markers", x: [], y: [], ...(threeDimensional ? { z: [] } : {}), customdata: [], text: [], meta: "selected-point", marker: { color: threeDimensional ? chartColors.fit : "#ffffff", size: threeDimensional ? 5.5 : 11, symbol: threeDimensional ? "diamond" : "circle", line: { color: threeDimensional ? "#ffffff" : chartColors.fit, width: threeDimensional ? 1.5 : 2.2 } }, hovertemplate: threeDimensional ? "%{text}<br>T = %{x:.2f} °C · V = %{y:.3f} V<br>峰位 %{z:.2f} ADC<extra></extra>" : "%{text}<br>温度 %{x:.2f} °C · 残差 %{y:.3f}%<extra></extra>", name: "当前选中", showlegend: false };
}

async function highlightSelectedPoint() {
  const point = state.point; if (!point || !window.Plotly) return;
  for (const [id, threeDimensional] of [["surface-chart", true], ["residual-chart", false]]) {
    const chart = $(id); const index = chart.data?.findIndex((trace) => trace.meta === "selected-point");
    if (!(index >= 0)) continue;
    const text = `当前选中 · ${point.stem}${isExcluded(point) ? " · 人工屏蔽" : point.used === false ? " · 质量筛除" : ""}`;
    await Plotly.restyle(chart, { x: [[point.temp]], y: [[threeDimensional ? point.bias : residualPercent(point)]], ...(threeDimensional ? { z: [[point.center]] } : {}), customdata: [[point.id || point.stem]], text: [[text]] }, [index]);
  }
}

function residualPercent(point) {
  if (point.residual_percent !== undefined) return Number(point.residual_percent);
  if (point.predicted && point.center !== undefined) return 100 * (point.center - point.predicted) / point.predicted;
  return point.residual !== undefined ? Number(point.residual) : NaN;
}

async function loadSurface() {
  if (!state.run) return;
  const token = ++state.loadToken;
  const data = await api(runPath(`/surface?channel=${state.channel}`));
  if (token !== state.loadToken) return;
  state.surface = data;
  const previousStem = state.point?.stem;
  const select = clear($("point-select"));
  for (const point of data.points || []) select.append(new Option(pointLabel(point), point.id || point.stem));
  await renderSurface();
  const selected = (data.points || []).find((p) => p.stem === previousStem) || (data.points || []).find((p) => !isExcluded(p)) || data.points?.[0];
  if (selected) await selectPoint(selected); else {
    state.point = null; state.spectrum = null;
    $("point-select").append(new Option("本通道暂无可用工况", ""));
    $("fit-spectrum").disabled = true; $("toggle-excluded").disabled = true;
    Plotly.purge($("spectrum-chart")); clear($("spectrum-params"));
  }
}

async function selectPoint(point) {
  if (state.dirty) await saveRevision({}, true);
  const token = ++state.spectrumToken;
  state.point = point; state.spectrum = null; state.dirty = false; $("point-select").value = point.id || point.stem;
  await highlightSelectedPoint();
  displayWindowValue("window-lo", ""); displayWindowValue("window-hi", "");
  $("window-lo").disabled = true; $("window-hi").disabled = true;
  clear($("spectrum-params"));
  if (window.Plotly) Plotly.purge($("spectrum-chart"));
  clear($("spectrum-chart")).append(node("p", "chart-loading", "正在读取对应工况的真实能谱…"));
  $("selected-channel").textContent = `CH ${state.channel}`;
  const condition = clear($("point-condition"));
  for (const [key, value] of [["温度", `${number(point.temp, 2)} °C`], ["偏压", `${number(point.bias, 3)} V`]]) { const item = node("span", "", key); item.append(node("strong", "", value)); condition.append(item); }
  $("window-status").textContent = "读取真实谱…";
  $("fit-spectrum").disabled = true;
  try {
    const spectrum = await api(runPath(`/spectrum?stem=${encodeURIComponent(point.stem)}&channel=${state.channel}`));
    if (token !== state.spectrumToken) return;
    state.spectrum = spectrum;
    const fit = spectrum.fit || {};
    const override = state.run.config?.windows?.[point.stem]?.[String(state.channel)];
    displayWindowValue("window-lo", override?.lo ?? fit.window_lo ?? fit.lo ?? "");
    displayWindowValue("window-hi", override?.hi ?? fit.window_hi ?? fit.hi ?? "");
    $("window-lo").disabled = state.busy; $("window-hi").disabled = state.busy;
    $("window-status").textContent = (state.run.pending_windows || []).includes(point.id) ? "修订已保存 · 待拟合" : override ? "已应用修订窗" : "当前拟合窗";
    const params = clear($("spectrum-params"));
    for (const [label, value] of [["峰位 / ADC", `${number(fit.center, 3)} ± ${number(fit.center_err, 3)}`], ["σ / ADC", number(fit.sigma, 3)], ["分辨率", fit.resolution === undefined ? "—" : `${number(fit.resolution * (fit.resolution > 1 ? 1 : 100), 2)}%`], ["R²", number(fit.rsquared, 5)], ["拟合模型", fit.fit_mode || fit.mode || "—"], ["计数谱来源", spectrum.origin === "history" || spectrum.historical || ["history", "historical"].includes(spectrum.source) ? "历史 L0 数据" : spectrum.source === "run" ? "本次 L0 数据" : "真实 L0 数据"], ["峰拟合来源", spectrum.fit_source === "computed" ? "本次处理器拟合" : spectrum.fit_source === "historical" ? "历史 L1 拟合" : "真实 L1 拟合"]]) {
      const item = node("div", "param-item"); item.append(node("span", "", label), node("strong", "", value)); params.append(item);
    }
    await renderSpectrum(); setBusy(state.busy);
  } catch (error) { if (token !== state.spectrumToken) return; $("window-status").textContent = "读取失败"; clear($("spectrum-chart")).append(node("p", "chart-loading", "此工况能谱读取失败，请刷新重试。")); messageError(error); }
  updateExcludedButton(); renderRunBadge();
}

async function renderSpectrum() {
  const data = state.spectrum; if (!data) return;
  const traces = [{ type: "scatter", mode: "lines", x: data.x || [], y: (data.y || []).map((v) => v > 0 ? v : null), line: { color: chartColors.ink, width: 1.2, shape: "hv" }, name: "真实计数谱", hovertemplate: "ADC %{x:.1f}<br>计数 %{y:.0f}<extra></extra>" }];
  const curve = data.curve || {};
  if (curve.x?.length && curve.y?.length) traces.push({ type: "scatter", mode: "lines", x: curve.x, y: curve.y, line: { color: chartColors.fit, width: 2.1 }, name: "峰 + 背景拟合", hovertemplate: "ADC %{x:.1f}<br>拟合 %{y:.2f}<extra></extra>" });
  if (curve.x?.length && curve.background?.length) traces.push({ type: "scatter", mode: "lines", x: curve.x, y: curve.background.map((v) => v > 0 ? v : null), line: { color: "#89939d", width: 1.3, dash: "dash" }, name: "背景", hoverinfo: "skip" });
  const lo = windowValue("window-lo"); const hi = windowValue("window-hi"); const width = hi - lo;
  const range = width > 0 ? [Math.max(0, lo - width * .6), hi + width * .6] : undefined;
  if (!$("spectrum-chart").data) clear($("spectrum-chart"));
  await Plotly.react($("spectrum-chart"), traces, { margin: { l: 55, r: 16, t: 14, b: 41 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, xaxis: { ...axisStyle, title: { text: "ADC", standoff: 5 }, range }, yaxis: { ...axisStyle, title: { text: "计数 / bin", standoff: 6 }, type: "log", exponentformat: "power" }, shapes: windowShapes(lo, hi), hovermode: "closest", uirevision: `${state.run.id}-${state.channel}-${state.point.stem}` }, plotConfig);
}

async function loadEcSpectrum() {
  const token = ++state.ecToken; const layer = state.layer; const runId = state.run.id; const channel = state.channel;
  clear($("artifact-preview")).append(node("p", "chart-loading", "正在读取真实 EC 脉冲数据…"));
  const data = await api(runPath(`/ec-spectrum?layer=${encodeURIComponent(layer)}&channel=${channel}`));
  if (token !== state.ecToken || state.run.id !== runId || state.layer !== layer) return;
  const preview = clear($("artifact-preview"));
  const heading = node("div", "ec-spectrum-heading");
  heading.append(node("strong", "", `${data.stem} · CH ${channel}`), badge("idle", ["history", "historical"].includes(data.source) ? "历史谱参考" : "本次真实谱"));
  const chart = node("div", "chart ec-spectrum-chart"); chart.id = "ec-spectrum-chart";
  preview.append(heading, chart);
  const displayNote = /Histogram is focused/i.test(data.display_note || "") ? "图中聚焦既有 EC 复核窗口，区间外事件已单独计数。" : data.display_note || "";
  const detail = node("p", "ec-spectrum-detail", `显示区间内 ${number(data.events_in_display ?? data.n_events, 0)} / 全部 ${number(data.n_events, 0)} 事件${data.events_outside_display ? `；${number(data.events_outside_display, 0)} 事件位于显示区间外` : ""}。${displayNote}`);
  preview.append(detail);
  if (data.tb_corrfile) preview.append(node("p", "ec-spectrum-detail", `谱数据记录的 TB 参数：${data.tb_corrfile}`));
  preview.append(node("p", "ec-spectrum-path", data.path || data.source_path || ""));
  await Plotly.react(chart, [{ type: "scatter", mode: "lines", x: data.x || [], y: (data.y || []).map((v) => v > 0 ? v : null), line: { shape: "hv", color: chartColors.ink, width: 1.2 }, hovertemplate: "ADC %{x:.1f}<br>计数 %{y:.0f}<extra></extra>" }], { margin: { l: 65, r: 25, t: 12, b: 48 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, xaxis: { ...axisStyle, title: { text: `${data.field === "amp_corr" ? "校正后 " : ""}ADC`, standoff: 8 }, range: data.display_range }, yaxis: { ...axisStyle, title: { text: "计数 / bin", standoff: 8 }, type: "log", exponentformat: "power" }, hovermode: "closest" }, plotConfig);
}

function windowShapes(lo, hi) { if (!(hi > lo)) return []; return [{ type: "rect", xref: "x", yref: "paper", x0: lo, x1: hi, y0: 0, y1: 1, fillcolor: "rgba(36,87,167,0.045)", line: { width: 0 }, layer: "below" }, ...[lo, hi].map((value) => ({ type: "line", xref: "x", yref: "paper", x0: value, x1: value, y0: 0, y1: 1, line: { color: "#6b84a6", width: 1, dash: "dash" } }))]; }
function renderRunBadge() { const hasResult = state.run?.jobs?.some((job) => normalizedStatus(job.status) === "succeeded"); $("run-state").textContent = state.loading ? "载入中" : !state.run ? "尚未创建试算" : state.busy ? "处理器运行中" : state.dirty ? "窗口未保存" : hasResult ? "修订与结果已保存" : "配置已保存"; $("run-state").className = `badge ${state.busy ? "running" : state.dirty ? "stale" : "neutral"}`; }
function updateExcludedButton() { const excluded = isExcluded(state.point); $("toggle-excluded").textContent = excluded ? "恢复此点" : "屏蔽此点"; $("toggle-excluded").className = `button${excluded ? "" : " danger"}`; $("toggle-excluded").disabled = !state.point || state.busy; }
function setBusy(busy) { state.busy = busy; ["run-layer", "run-pipeline", "refit-tb", "save-run", "new-run", "run-select", "payload"].forEach((id) => { $(id).disabled = busy || (!state.run && !["new-run", "payload"].includes(id)); }); ["fit-spectrum", "window-lo", "window-hi"].forEach((id) => { $(id).disabled = busy || !state.spectrum; }); $("channel").disabled = state.loading; $("point-select").disabled = state.loading; updateExcludedButton(); renderRunBadge(); }

async function resizeCharts() {
  if (!window.Plotly) return;
  for (const id of ["surface-chart", "residual-chart", "spectrum-chart", "ec-spectrum-chart"]) {
    const chart = $(id);
    if (!chart?.data || chart.getBoundingClientRect().width <= 0 || chart.getBoundingClientRect().height <= 0) continue;
    await Plotly.Plots.resize(chart);
    if (id === "surface-chart") {
      const viewport = surfaceViewport();
      if (chart.dataset.cameraMode !== viewport.mode) {
        chart.dataset.cameraMode = viewport.mode;
        await Plotly.relayout(chart, { "scene.camera": viewport.camera, uirevision: `${state.run.id}-${state.channel}-${viewport.mode}` });
      }
    }
  }
}
async function selectLayer(id) { state.layer = id; renderNavigation(); if (state.run) { renderStage(); requestAnimationFrame(resizeCharts); } }

async function refreshRun(reloadCharts = true) {
  if (!state.run) return;
  state.run = await api(runPath());
  renderRun(); if (reloadCharts) await loadSurface();
}

async function openRun(id) {
  if (!id) return;
  const token = ++state.openToken;
  if (state.dirty) await saveRevision({}, true);
  if (token !== state.openToken) return;
  $("message").hidden = true;
  if ($("dismiss-message")) $("dismiss-message").hidden = true;
  ++state.pollToken; ++state.loadToken; ++state.spectrumToken;
  state.loading = true; setBusy(true);
  let running;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (token !== state.openToken) return;
    state.run = run; state.surface = null; state.point = null; state.spectrum = null; state.dirty = false;
    localStorage.setItem("grid-workbench-active-run", id);
    renderRun(); await loadSurface();
    if (token !== state.openToken) return;
    running = (state.run.jobs || []).find((job) => ["queued", "running"].includes(job.status));
  } finally {
    if (token === state.openToken) { state.loading = false; setBusy(false); renderRunOptions(); }
  }
  if (running && token === state.openToken) restorePolling(running).catch(messageError);
}

async function bootstrap() {
  state.loading = true; setBusy(true);
  try {
  state.bootstrap = await api("/api/bootstrap"); renderRunOptions(); renderNavigation();
  $("connection-label").textContent = "本地处理服务已连接";
  const active = state.bootstrap.active_run;
  const ids = (state.bootstrap.runs || []).map((run) => run.id);
  const remembered = localStorage.getItem("grid-workbench-active-run");
  const id = ids.includes(remembered) ? remembered : typeof active === "string" ? active : active?.id || ids[0];
  if (id) await openRun(id); else { state.loading = false; setBusy(false); renderRun(); }
  } catch (error) { state.loading = false; setBusy(false); throw error; }
}

function openCreateDialog(payload = $("payload").value) { $("new-run-payload").value = payload; $("new-run-name").value = ""; $("new-run-dialog").showModal(); }
async function createRun(event) {
  event.preventDefault(); const button = event.submitter; if (button) button.disabled = true;
  try {
    const result = await api("/api/runs", { method: "POST", body: { payload: $("new-run-payload").value, name: $("new-run-name").value.trim() || undefined } });
    $("new-run-dialog").close(); state.bootstrap = await api("/api/bootstrap"); await openRun((result.run || result).id);
    notify("独立试算已创建，真实历史产物作为基线载入。当前尚未运行计算。", "success");
  } catch (error) { messageError(error); } finally { if (button) button.disabled = false; }
}

function windowPatch() {
  if (!state.point || !state.spectrum) return {};
  const lo = windowValue("window-lo"), hi = windowValue("window-hi");
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || !(lo < hi) || lo < 0) throw new Error("请填写有效拟合窗口：下限 ≥ 0，且上限大于下限。");
  return { windows: { [state.point.stem]: { [String(state.channel)]: { lo, hi } } } };
}

async function saveRevision(extra = {}, silent = false) {
  const patch = { ...(state.dirty ? windowPatch() : {}), ...extra, reason: $("revision-note").value.trim() || "工作台人工复核" };
  const result = await api(runPath("/config"), { method: "PATCH", body: patch });
  state.run = result.run || result; state.dirty = false; renderRun();
  $("window-status").textContent = "已保存修订窗";
  if (!silent) notify("修订配置与说明已保存，重开此试算可恢复。尚未执行的拟合需要点击计算按钮。", "success");
}

async function toggleExcluded() {
  const point = state.point; if (!point) return;
  const ids = [...excludedIds()]; const id = point.id || `${point.stem}:${state.channel}`; const index = ids.indexOf(id);
  const excluded = isExcluded(point);
  if (excluded && index >= 0) ids.splice(index, 1); else if (!excluded) ids.push(id);
  await saveRevision({ excluded: ids }, true);
  await loadSurface();
  notify(`${point.stem} · CH ${state.channel} 已${excluded ? "恢复" : "屏蔽"}。点击“重新拟合 TB”计算新的响应曲面。`, "success");
}

function showTab(tab) {
  state.tab = tab; $("artifacts-content").hidden = tab !== "artifacts"; $("jobs-content").hidden = tab !== "jobs";
  for (const key of ["artifacts", "jobs"]) { $("tab-" + key).classList.toggle("active", key === tab); $("tab-" + key).setAttribute("aria-selected", String(key === tab)); }
  if (tab === "jobs" && $("records-drawer")) $("records-drawer").open = true;
}

function showAnalysisView(view) {
  state.analysisView = view;
  for (const key of ["residual", "comparison"]) {
    const button = $("analysis-tab-" + key), panel = $(key + "-panel");
    if (button) { button.classList.toggle("active", key === view); button.setAttribute("aria-selected", String(key === view)); button.tabIndex = key === view ? 0 : -1; }
    if (panel) panel.hidden = key !== view;
  }
  requestAnimationFrame(resizeCharts);
}

async function restorePolling(job) {
  const token = ++state.pollToken; const runId = state.run.id;
  setBusy(true); showTab("jobs"); notify(`${job.layer} 仍在处理器中运行，已恢复任务状态跟踪。`, "info");
  try {
    while (["queued", "running", "pending"].includes(job.status) && token === state.pollToken) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const response = await api(`/api/runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(job.id)}`); job = response.job || response;
      if (token !== state.pollToken) return;
      const index = state.run.jobs.findIndex((item) => item.id === job.id); if (index >= 0) state.run.jobs[index] = job;
      renderJobs();
    }
    if (token !== state.pollToken) return;
    await refreshRun(true);
    notify(normalizedStatus(job.status) === "succeeded" ? `${job.layer} 计算成功，恢复后的任务记录与真实产物已更新。` : `${job.layer} 计算失败：${valueText(job.error || "详见运行记录")}`, normalizedStatus(job.status) === "succeeded" ? "success" : "error");
  } finally { if (token === state.pollToken) setBusy(false); }
}

async function executeJob(layer, extra = {}, manageBusy = true) {
  if (manageBusy) setBusy(true);
  try {
    const payload = { layer, representative: $("run-scope").value !== "full", ...extra };
    let job = await api(runPath("/jobs"), { method: "POST", body: payload }); job = job.job || job;
    state.run.jobs = [...(state.run.jobs || []).filter((item) => item.id !== job.id), job];
    state.run.stages = { ...(state.run.stages || {}), [layer]: { ...(state.run.stages?.[layer] || {}), status: job.status || "queued" } };
    renderJobs(); renderNavigation(); renderStage(); showTab("jobs");
    notify(`${layer} 已提交到真实处理器。运行结果与错误将记录到当前试算。`, "info");
    while (["queued", "running", "pending"].includes(job.status)) {
      await new Promise((resolve) => setTimeout(resolve, 850));
      const response = await api(runPath(`/jobs/${encodeURIComponent(job.id)}`)); job = response.job || response;
      const index = state.run.jobs.findIndex((item) => item.id === job.id); if (index >= 0) state.run.jobs[index] = job;
      renderJobs();
    }
    await refreshRun(true);
    if (normalizedStatus(job.status) !== "succeeded") throw new Error(`${layer} 失败：${valueText(job.error || job.message || "处理器未返回成功状态，请查看运行记录。")}`);
    notify(`${layer} 计算成功。新产物、处理日志与修订已保存到 runs/${state.run.id}/。`, "success");
    return job;
  } finally { if (manageBusy) setBusy(false); }
}

async function fitSpectrum() {
  const stem = state.point.stem; setBusy(true);
  try { await saveRevision(windowPatch(), true); await executeJob("TB_L1", { stem, channel: state.channel }, false); notify("当前工况四通道 L1 拟合已完成。点击“重新拟合 TB”更新曲面，并比较新旧参数。", "success"); } finally { setBusy(false); }
}

async function runPipeline() {
  setBusy(true); $("run-scope").value = "representative";
  try {
    if (state.dirty) await saveRevision({}, true);
    for (const layer of layers) { await selectLayer(layer.id); await executeJob(layer.id, { representative: true }, false); }
    await selectLayer("EC_L3"); notify("代表样例七层闭环已实际运行成功。各层结果、TB 依赖版本与完整记录已保存。", "success");
  } finally { setBusy(false); }
}

function handleError(action) { return async (...args) => { try { await action(...args); } catch (error) { messageError(error); } }; }
$("new-run").addEventListener("click", () => openCreateDialog());
$("create-first-run").addEventListener("click", () => openCreateDialog());
$("close-dialog").addEventListener("click", () => $("new-run-dialog").close());
$("cancel-new-run").addEventListener("click", () => $("new-run-dialog").close());
$("new-run-form").addEventListener("submit", createRun);
$("run-select").addEventListener("change", handleError((event) => openRun(event.target.value)));
$("payload").addEventListener("change", handleError(async (event) => { const payload = event.target.value; const run = (state.bootstrap.runs || []).find((item) => item.payload === payload); if (run) await openRun(run.id); else openCreateDialog(payload); }));
$("channel").addEventListener("change", handleError(async (event) => { if (state.dirty) await saveRevision({}, true); state.channel = Number(event.target.value); state.dirty = false; await loadSurface(); if (["EC_L0", "EC_L1"].includes(state.layer)) await loadEcSpectrum(); }));
$("point-select").addEventListener("change", handleError(async (event) => { const point = state.surface?.points.find((p) => (p.id || p.stem) === event.target.value); if (point) await selectPoint(point); }));
for (const id of ["window-lo", "window-hi"]) $(id).addEventListener("input", () => { $(id).dataset.edited = "true"; $(id).title = ""; state.dirty = true; $("window-status").textContent = "窗口已修改 · 未拟合"; renderRunBadge(); if (window.Plotly && $("spectrum-chart").data) Plotly.relayout($("spectrum-chart"), { shapes: windowShapes(windowValue("window-lo"), windowValue("window-hi")) }); });
$("save-run").addEventListener("click", handleError(() => saveRevision()));
$("fit-spectrum").addEventListener("click", handleError(fitSpectrum));
$("toggle-excluded").addEventListener("click", handleError(toggleExcluded));
$("refit-tb").addEventListener("click", handleError(async () => { if (state.dirty) await saveRevision({}, true); await executeJob("TB_L2"); }));
$("run-layer").addEventListener("click", handleError(async () => { if (state.dirty) await saveRevision({}, true); const extra = state.layer === "TB_L1" && state.point && $("run-scope").value !== "full" ? { stem: state.point.stem, channel: state.channel } : {}; await executeJob(state.layer, extra); }));
$("run-pipeline").addEventListener("click", handleError(runPipeline));
$("refresh-run").addEventListener("click", handleError(() => refreshRun()));
$("tab-artifacts").addEventListener("click", () => showTab("artifacts"));
$("tab-jobs").addEventListener("click", () => showTab("jobs"));
$("dismiss-message")?.addEventListener("click", () => { $("message").hidden = true; $("dismiss-message").hidden = true; });
for (const view of ["residual", "comparison"]) {
  $("analysis-tab-" + view)?.addEventListener("click", () => showAnalysisView(view));
  $("analysis-tab-" + view)?.addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "residual" : event.key === "End" ? "comparison" : state.analysisView === "residual" ? "comparison" : "residual"; showAnalysisView(next); $("analysis-tab-" + next)?.focus(); } });
}
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resizeCharts, 100); });
$("records-drawer")?.addEventListener("toggle", () => requestAnimationFrame(resizeCharts));
window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
bootstrap().catch((error) => { $("connection-label").textContent = "本地服务连接失败"; messageError(error); });
