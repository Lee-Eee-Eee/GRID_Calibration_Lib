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
const state = { bootstrap: null, run: null, layer: "TB_L0", channel: 0, surface: null, spectrum: null, point: null, ecOverview: null, tbVersions: [], datasets: [], surfaceMode: "response", busy: false, loading: false, dirty: false, openToken: 0, loadToken: 0, spectrumToken: 0, ecToken: 0, pollToken: 0, tab: "artifacts", analysisView: "residual", expandedJobs: new Set() };
const statusNames = { idle: "未运行", pending: "未运行", not_run: "未运行", queued: "排队中", running: "运行中", succeeded: "计算成功", success: "计算成功", completed: "计算成功", partial: "部分完成", failed: "计算失败", stale: "依赖待更新", historical: "仅历史参考" };
const plotConfig = { responsive: true, displaylogo: false, showLink: false, modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines", "sendDataToCloud", "sendChartToCloud"], toImageButtonOptions: { format: "png", scale: 2 } };
const chartFont = { family: 'Arial, "Microsoft YaHei", sans-serif', size: 12, color: "#586572" };
const numericFont = { ...chartFont, family: '"Times New Roman", serif' };
const chartColors = { ink: "#34414d", blue: "#2457a7", fit: "#b54b37", excluded: "#a46c13", muted: "#929da7", grid: "#e3e7eb", border: "#c4ccd4" };
const axisStyle = { tickfont: numericFont, gridcolor: chartColors.grid, linecolor: chartColors.border, showline: true, ticks: "outside", ticklen: 3, tickcolor: chartColors.border, zeroline: false, automargin: true };
const chartHover = { bgcolor: "#ffffff", bordercolor: "#c4ccd4", font: { ...chartFont, color: "#18242e" }, align: "left" };

function node(tag, className, value) { const el = document.createElement(tag); if (className) el.className = className; if (value !== undefined && value !== null) el.textContent = String(value); return el; }
function clear(el) { el.replaceChildren(); return el; }
function plotTarget(chart) { for (const child of [...chart.children]) if (child.matches("p.chart-loading, table, .window-handle")) child.remove(); return chart; }
function isPeakReviewLayer(layer = state.layer) { return ["TB_L1", "EC_L2"].includes(layer); }
function detectorPayload() { return state.run?.acquisition_preset?.payload || state.run?.dataset?.profile || state.run?.dataset?.source_payload || state.run?.payload || ""; }
function hasPlasticChannel() { return ["10B", "11B"].includes(String(detectorPayload()).toUpperCase()); }
function supportedFitChannels() { return hasPlasticChannel() ? [0, 1, 2] : [0, 1, 2, 3]; }
function unsupportedSpectralModel(channel = state.channel, point = state.point, spectrum = state.spectrum) {
  if (!["TB_L1", "TB_L2", "EC_L2", "EC_L3"].includes(state.layer)) return "";
  if (hasPlasticChannel() && channel === 3) return "塑料闪烁体 · 康普顿响应，拟合方案待定";
  const value = spectrum?.unsupported_spectral_model || point?.unsupported_spectral_model;
  return typeof value === "string" ? value : value?.reason || "";
}
function spectrumChart() { return $(isPeakReviewLayer() ? "surface-chart" : "spectrum-chart"); }
function spectrumBinWidth(data = state.spectrum) {
  const value = data?.histogram_config?.bin_width ?? data?.bin_width ?? data?.spectrum?.bin_width;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}
function ecBinningSpec(data = state.spectrum) { const width = spectrumBinWidth(data); return width ? { bin_width: width } : {}; }
function spectrumWindowBounds() {
  const data = state.spectrum || {}, histogram = data.histogram_config || {}, bounds = data.window_bounds || {}, width = spectrumBinWidth(data);
  return { min: bounds.min ?? (state.layer.startsWith("EC") ? 0 : histogram.min ?? 0),
    max: bounds.max ?? (state.layer.startsWith("EC") ? 65536 : histogram.max ?? 1200),
    minimum_width: bounds.minimum_width ?? (width ? 8 * width : 8) };
}
function fitFeedback(text = "", status = "neutral") { const holder = $("fit-result-status"); holder.textContent = text; holder.dataset.status = status; holder.hidden = !text; }
function hasQualityWarning(point) {
  const quality = point?.fit_quality || point?.fit?.fit_quality || {};
  if (["model_pending", "fit_failed", "not_fitted"].includes(quality.status) || ["model_pending", "failed", "not_fitted", "not_parsed"].includes(point?.fit_status) || point?.unsupported_spectral_model) return false;
  return point?.suspect === true || quality.suspect === true || quality.quality_pass === false || quality.accepted === false || ["suspect", "rejected"].includes(point?.fit_status);
}
function fitCompletionFeedback(job, automatic, channel) {
  if (job?.status === "partial") { fitFeedback("部分通道计算失败 · 可用结果已保存", "attention"); return; }
  const suspect = hasQualityWarning(state.spectrum?.fit);
  fitFeedback(suspect ? "拟合完成 · 疑似问题，结果已保留" : `${automatic ? "自动选窗" : "当前窗口拟合"}完成 · CH ${channel}`, suspect ? "attention" : "success");
}
function updateSpectralModelControls() {
  const reason = unsupportedSpectralModel(), holder = $("spectral-model-note");
  holder.hidden = !reason; holder.textContent = reason;
  for (const id of ["fit-spectrum", "auto-fit-spectrum", "refit-tb", "run-layer"]) {
    if ($(id).dataset.unsupportedReason && $(id).title === $(id).dataset.unsupportedReason) $(id).title = "";
    delete $(id).dataset.unsupportedReason;
  }
  if (reason) {
    for (const id of ["fit-spectrum", "auto-fit-spectrum", "refit-tb"]) { $(id).disabled = true; $(id).title = reason; $(id).dataset.unsupportedReason=reason; }
    if (isPeakReviewLayer() && $("run-scope").value !== "full") { $("run-layer").disabled = true; $("run-layer").title = reason; $("run-layer").dataset.unsupportedReason=reason; }
    $("peak-diagnostics").hidden = true;
    clear($("spectrum-params"));
  }
  $("l1-fit-all-channels").textContent = hasPlasticChannel() ? "拟合此文件 · CH 0–2" : "拟合此文件 · 四通道";
  $("fit-next-step").hidden = !isPeakReviewLayer();
  $("review-next-layer").textContent = state.layer === "TB_L1" ? "TB L2 · 曲面拟合 →" : "EC L3 · 能量刻度 →";
}
function chartMessage(id, text) { const chart = $(id); if (window.Plotly && chart.data) Plotly.purge(chart); clear(chart).append(node("p", "chart-loading", text)); }
function valueText(value) { return value === null || value === undefined ? "—" : Array.isArray(value) ? value.join(" · ") : typeof value === "object" ? JSON.stringify(value) : String(value); }
function versionText(value) { return !value ? "历史基线" : typeof value === "object" ? `${value.filename || value.version || "TB"}${value.sha256 ? ` · ${value.sha256.slice(0, 8)}` : ""}` : String(value); }
function number(value, digits = 4) { if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—"; const n = Number(value); return Math.abs(n) > 0 && (Math.abs(n) < .001 || Math.abs(n) >= 1e6) ? n.toExponential(3) : n.toLocaleString("en-US", { maximumFractionDigits: digits }); }
function windowValue(id) { const input = $(id); return Number(input.dataset.edited !== "true" && input.dataset.preciseValue !== undefined ? input.dataset.preciseValue : input.value); }
function displayWindowValue(id, value) { const input = $(id); input.dataset.edited = "false"; if (value !== "" && value !== null && value !== undefined && Number.isFinite(Number(value))) { input.dataset.preciseValue = String(value); input.value = Number(value).toFixed(3); input.title = `原始精度：${value} ADC`; } else { delete input.dataset.preciseValue; input.value = ""; input.title = ""; } }
function timestamp(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }); }
function normalizedStatus(status) { return ["success", "completed"].includes(status) ? "succeeded" : status || "idle"; }
function badge(status, label) { const s = normalizedStatus(status), tone = s === "partial" ? "stale" : s; return node("span", `badge ${["succeeded", "running", "queued", "failed", "stale"].includes(tone) ? tone : "neutral"}`, label || statusNames[s] || s); }
function notify(message, type = "info") { $("message").textContent = message; $("message").className = `notice ${type}`; $("message").hidden = false; if ($("dismiss-message")) $("dismiss-message").hidden = false; }
function messageError(error) { notify(error.message || String(error), "error"); }
function fitAttemptReason(error) { try { const values=JSON.parse(error); if(Array.isArray(values)) return values.map(item=>typeof item==="string"?item:item.error||item.message).filter(Boolean).join(" · "); } catch {} return String(error||"未知原因"); }
function jobErrorText(error) {
  let values; try { values = JSON.parse(error); } catch { values = [error]; }
  if (!Array.isArray(values)) values = [values];
  const messages = values.map(item => typeof item === "string" ? item : item?.error || item?.message || "").filter(text => text && !/^Processor produced no scientific output/.test(text)).map(text => {
    if (/No resolved photopeak/.test(text)) return "当前窗口内未找到可用峰，请调整窗口或噪声上界。";
    if (/unresolved higher-ADC competitor/.test(text)) return "低 ADC 噪声峰与光电峰尚未分清，请把窗口移到目标峰，或设置噪声上界。";
    if (/Unable to allocate|MemoryError/.test(text)) return "解包内存不足。";
    return text;
  });
  return [...new Set(messages)].slice(0, 3).join("；") || String(error || "计算失败");
}
function activeLayer() { return layers.find((item) => item.id === state.layer); }
function viewContext() { return `${state.run?.id}:${state.layer}:${state.channel}`; }
function plotContext() { return `${viewContext()}:${state.loadToken}:${state.spectrumToken}`; }
function isUnpackLayer() { return ["TB_L0", "EC_L0"].includes(state.layer); }
function unpackInputs() { return (state.layer === "TB_L0" ? state.surface?.points : state.ecOverview?.files) || []; }
function unpackRawInputs() {
  const rows = state.run?.inventory?.raw?.reading_preview?.rows;
  if (Array.isArray(rows)) return rows.filter(item => state.layer === "TB_L0" ? item.kind === "tb" : item.kind !== "tb");
  return unpackInputs().filter(item => item.event);
}
function unpackDisabledReason(all = false) {
  if (!state.run) return "请先选择数据并创建试算";
  if (state.loading) return "正在载入试算";
  if (state.busy) return "处理器正在运行";
  if (!state.run.dataset?.raw_dir && !state.run.raw_dir && !unpackRawInputs().length) return "当前仅载入产品，请选择原始数据目录后创建试算";
  if (all) return unpackRawInputs().length ? "" : "当前层未发现匹配的原始文件，请检查数据目录与文件规则";
  if (!state.point) return "请先选择输入文件";
  if ($("unpack-search").value.trim() && !state.point.stem.toLowerCase().includes($("unpack-search").value.trim().toLowerCase())) return "请从筛选结果中选择输入文件";
  if (!state.point.event) return "未找到此项的原始文件，请检查目录和文件规则";
  return "";
}
function renderUnpackWorkspace() {
  if (!state.run || !isUnpackLayer()) return;
  const dataset = state.run.dataset || {}, profile = state.run.acquisition, preset = state.run.acquisition_preset;
  $("unpack-source").textContent = dataset.raw_dir || state.run.raw_dir || (unpackRawInputs().length ? state.bootstrap?.defaults?.raw_dir || "工作区默认数据目录" : "仅载入产品 · 尚未选择原始数据");
  $("unpack-source").title = $("unpack-source").textContent;
  const sourceParts = $("unpack-source").textContent.split(/[\\/]/).filter(Boolean);
  if (sourceParts.length > 3) $("unpack-source").textContent = `${sourceParts[0]} / … / ${sourceParts.slice(-2).join(" / ")}`;
  $("unpack-profile").textContent = `${state.run.payload} · ${preset?.version ? `v${preset.version}` : "默认配置"}`;
  $("unpack-profile").title = `${preset?.name || "默认读取配置"}\n${profile?.sha256 || ""}`;
  $("unpack-output").textContent = `输出：runs/${state.run.id}/products/${state.run.payload}/${state.layer.startsWith("TB") ? "tb" : "ec"}/L0/`;
  const filter = $("unpack-search").value.trim().toLowerCase(), select = clear($("unpack-file"));
  const files = unpackInputs().filter(item => !filter || item.stem.toLowerCase().includes(filter));
  if (!files.some(item => (item.id || item.stem) === state.point?.id)) select.append(new Option(files.length ? "选择输入文件" : "没有匹配文件", ""));
  for (const item of files) select.append(new Option(item.stem, item.id || item.stem));
  select.value = files.some(item => (item.id || item.stem) === state.point?.id) ? state.point.id : "";
  const holder = clear($("unpack-files"));
  for (const [label, value] of [["Event", state.point?.event], ["HK", state.point?.hk]]) { const row = node("span"); row.title = value || ""; row.append(node("b", "", label), node("span", "", value ? value.split(/[\\/]/).pop() : state.point ? "未匹配原始文件" : "选择文件后显示配对文件")); holder.append(row); }
  const reason = unpackDisabledReason();
  $("unpack-action-hint").textContent = reason || `将解包 ${state.point.stem} · 输出包含全部通道`;
  updateUnpackControls();
  const job = currentLayerJob();
  if (job) renderUnpackProgress(job);
  else $("unpack-progress").hidden = true;
}
function updateUnpackControls() {
  for (const [id, all] of [["unpack-selected", false], ["unpack-all", true]]) { const reason = unpackDisabledReason(all); $(id).disabled = !!reason; $(id).title = reason || (all ? `解包本层全部 ${unpackRawInputs().length} 个文件` : `仅解包 ${state.point?.stem}`); }
  $("unpack-file").disabled = state.loading || state.busy;
  $("unpack-settings").disabled = !state.run || state.busy;
  $("unpack-data").disabled = state.busy;
  if (isUnpackLayer()) $("unpack-action-hint").textContent = unpackDisabledReason() || `将解包 ${state.point.stem} · 输出包含全部通道`;
}
function renderUnpackProgress(job) {
  if (!isUnpackLayer() || job.layer !== state.layer) return;
  const holder = $("unpack-progress"), status = normalizedStatus(job.status);
  holder.hidden = false; holder.dataset.status = status;
  holder.textContent = `${statusNames[status] || status} · ${job.config?.only?.join(", ") || job.stem || job.request?.stem || "本层解包任务"}${job.error ? ` · ${jobErrorText(job.error)}` : status === "succeeded" ? " · 新产物与日志已保存" : " · 可在运行记录中查看处理器日志"}`;
}
async function runUnpack(all = false) {
  const reason = unpackDisabledReason(all); if (reason) throw new Error(reason);
  $("run-scope").value = all ? "full" : "representative";
  await executeJob(state.layer, all ? { representative: false } : { stem: state.point.stem, channel: state.channel, representative: false });
}
function runPath(suffix = "") { return `/api/runs/${encodeURIComponent(state.run.id)}${suffix}`; }
function paramsFor(value) { if (!value) return {}; if (Array.isArray(value)) return value[state.channel] || {}; return value[state.channel] && typeof value[state.channel] === "object" ? value[state.channel] : value; }
function pointLabel(point) { return `${point.stem} · ${number(point.temp, 1)} °C / ${number(point.bias, 3)} V${isExcluded(point) ? " · 已屏蔽" : point.fit_status === "not_parsed" ? " · 待解包" : point.fit_status === "not_fitted" ? " · 待拟合" : hasQualityWarning(point) ? " · 疑似问题" : pointUsage(point) === false ? " · 无可用参数" : ""}`; }
function excludedIds() { return state.run?.config?.[state.layer.startsWith("EC") ? "ec_excluded" : "excluded"] || []; }
function isExcluded(point) { return !!point?.excluded || excludedIds().includes(point?.id); }
function pointUsage(point) { return typeof point?.draft_used === "boolean" ? point.draft_used : point?.used; }
function isUsed(point) { return !isExcluded(point) && pointUsage(point) !== false; }
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
      const navStatus = stage?.stale && normalizedStatus(stage?.status) === "succeeded" ? "stale" : normalizedStatus(stage?.status);
      const coverage = stage?.acquisition_coverage, partial = coverage?.completed > 0 && coverage.completed < coverage.required && !["running","queued","failed"].includes(navStatus);
      const statusLabel = partial ? `已处理 ${coverage.completed} / ${coverage.required}` : statusNames[navStatus] || "未运行";
      button.title = `${layer.id} · ${layer.title} · ${statusLabel}`;
      button.setAttribute("aria-label", `${layer.id} ${layer.title}，状态：${statusLabel}`);
      const dot = node("span", `nav-status ${partial ? "succeeded" : navStatus}`); dot.title = statusLabel; dot.setAttribute("aria-hidden", "true"); button.append(dot);
      if (state.run) { const shortStatus = { succeeded: "已计算", partial: "部分完成", failed: "失败", stale: "待更新", running: "运行中", queued: "排队中" }; button.append(node("span", "nav-status-text", partial ? `${coverage.completed}/${coverage.required}` : shortStatus[navStatus] || "未运行")); }
      button.addEventListener("click", handleError(() => selectLayer(layer.id))); group.append(button);
    }
    nav.append(group);
  }
}

function cleanRunName(name, payload) {
  let text = String(name || "").trim(), previous;
  do { previous = text; if (text.startsWith(payload)) text = text.slice(payload.length).replace(/^[\s·:：—\-]+/, ""); } while (text !== previous);
  if (text.endsWith(payload)) text = text.slice(0, -payload.length).replace(/[\s·:：—\-]+$/, "");
  return text.replace(/[（(\[【]+\s*$/, "").trim();
}
function shortDate(value) { return value ? new Date(value).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Shanghai"}) : "未命名试算"; }
function renderRunOptions() {
  const payload = state.run?.payload || $("payload").value;
  const runs = (state.bootstrap?.runs || []).filter(run => run.payload === payload);
  const select = clear($("run-select")); select.append(new Option(runs.length ? "选择试算" : "暂无试算", ""));
  const names = runs.map(run => cleanRunName(run.name, run.payload) || shortDate(run.created_at));
  runs.forEach((run, index) => { const option = new Option(`${names[index]}${names.filter(name => name === names[index]).length > 1 ? ` · ${shortDate(run.created_at)}` : ""}`, run.id); option.title = run.name || run.id; select.append(option); });
  select.value = state.run?.id || "";
}
function savedView(runId = state.run?.id) {
  try { return JSON.parse(localStorage.getItem(`grid-workbench-view:${runId}`) || "{}"); } catch { return {}; }
}
function rememberView() {
  if (!state.run) return;
  const view = savedView(), family = state.layer.slice(0,2);
  view.layer = state.layer; view.channel = state.channel; view.points ||= {}; view.familyStems ||= {};
  if (state.point) { view.points[`${family}:${state.channel}`] = {id:state.point.id,stem:state.point.stem}; view.familyStems[family] = state.point.stem; }
  localStorage.setItem(`grid-workbench-view:${state.run.id}`, JSON.stringify(view));
}
function preferredPoint() {
  const view = savedView(), family = state.layer.slice(0,2);
  return view.points?.[`${family}:${state.channel}`] || {stem:family === "EC" ? view.familyStems?.EC?.replace(/_ch\d$/, `_ch${state.channel}`) : view.familyStems?.TB};
}
function currentLayerJob() {
  const jobs = [...(state.run?.jobs || [])].reverse().filter(job => job.layer === state.layer && (!job.config?.fit_channels || job.config.fit_channels.includes(state.channel)));
  if (!state.point || ["TB_L2","EC_L3"].includes(state.layer)) return jobs[0];
  return jobs.find(job => job.config?.only?.includes(state.point.stem) || job.config?.ec_point_id === state.point.id || job.scope === "all" || job.result?.outputs?.some(path => { const name = String(path).split(/[\\/]/).pop(), stem = state.point.stem; return name.startsWith(`${stem}.`) || name.startsWith(`${stem}_corr.`) || name.startsWith(`${stem}_fit_results.`); }));
}
function datasetMatchesPayload(dataset, payload) { return dataset.payload === payload || dataset.acquisition_preset_match?.reference?.payload === payload || (state.bootstrap?.runs || []).some(run => run.dataset_id === dataset.id && run.payload === payload); }
function renderCurrentStatus() {
  if (!state.run) return;
  if (unsupportedSpectralModel()) {
    $("stage-status").replaceWith(Object.assign(badge("idle", "拟合方案待定"), {id: "stage-status"}));
    return;
  }
  const job = currentLayerJob(), stage = state.run.stages?.[state.layer] || {};
  let status = job?.status || "idle", label;
  if (state.layer === "TB_L1" && state.point) {
    const item = state.l1Overview?.files?.find(file => file.stem === state.point.stem)?.channels?.[state.channel];
    if (item && !["queued","running"].includes(status)) {
      status = item.fit_status === "fitted" ? "succeeded" : ["failed","suspect","rejected"].includes(item.fit_status) ? "stale" : "idle";
      label = item.pending_window ? "窗口待拟合" : item.fit_status === "failed" ? "当前通道计算失败" : hasQualityWarning(item) ? "已拟合 · 疑似问题" : item.fit_status === "fitted" ? "已拟合" : "待拟合";
    }
  }
  if (!job && state.point) label = state.point.fit_status === "not_parsed" ? "待解包" : state.point.fit_status === "not_fitted" ? "待拟合" : isUnpackLayer() || state.layer === "EC_L1" ? "已有谱" : "已有拟合";
  else if (!job && stage.history_count) label = "已有结果";
  $("stage-status").replaceWith(Object.assign(badge(status,label), {id:"stage-status"}));
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
  $("validation-detail").textContent = validation ? `${validation.passed ?? 0} / ${validation.total ?? 0} 文件通过${hasCrcFailure ? " · CRC 检查未通过" : ""}` : "";
  $("stage-counts").textContent = `已有文件 ${stage.history_count ?? 0} · 新输出 ${stage.current_count ?? 0}`;
  const acquisition = state.run.acquisition, acquisitionCoverage = stage.acquisition_coverage, acquisitionPending = (state.run.acquisition_pending_layers || []).includes(layer.id), acquisitionIndicator = $("acquisition-indicator");
  acquisitionIndicator.hidden = !acquisition;
  acquisitionIndicator.className = `badge ${acquisitionPending ? "stale" : acquisitionCoverage?.current ? "succeeded" : "neutral"}`;
  acquisitionIndicator.textContent = acquisition ? `读取 v${acquisition.revision}${acquisitionCoverage ? ` · 已更新 ${acquisitionCoverage.completed} / ${acquisitionCoverage.required}` : ""}${acquisitionPending ? " · 待更新" : ""}` : "";
  acquisitionIndicator.title = acquisition ? `读取配置 SHA-256 ${acquisition.sha256 || "未知"}${acquisitionCoverage?.missing?.length ? `\n待更新输入：${acquisitionCoverage.missing.join(", ")}` : ""}` : "";
  $("acquisition-settings").title = acquisition ? `读取配置 v${acquisition.revision} · ${(acquisition.sha256 || "").slice(0, 10)}` : "选择或修改本载荷的 XML、文件规则与物理标签读取方式";
  $("tb-analysis").hidden = false;
  $("stage-analysis").hidden = true;
  configureLayerPanels(); renderCurrentStatus(); renderUnpackWorkspace();
  $("stage-overview-title").textContent = layer.heading;
  $("stage-overview-caption").textContent = "处理依赖与产物预览";
  const flow = clear($("stage-flow"));
  [{ label: "输入", text: layer.input }, { label: "处理", text: layer.process }, { label: "输出", text: layer.output }].forEach((item, index) => {
    if (index) flow.append(node("span", "flow-arrow", "→"));
    const card = node("div", "flow-node"); card.append(node("strong", "", item.label), node("span", "", item.text)); flow.append(card);
  });
  renderDependencies(); renderArtifacts();
}

function renderDependencies() {
  const dependency = state.run.ec_dependency;
  const current = state.run.tb_version;
  let text = dependency ? versionText(dependency.tb_version || dependency.version || dependency.tb_corrfile || dependency) : "尚未生成本次 EC 校正";
  $("ec-version").textContent = `本次 EC L1 绑定：${text}`;
  $("ec-version").title = text;
  const ecLayer = state.layer.startsWith("EC"), displayedTb = ecLayer ? state.run.selected_tb : current;
  const displayedTbLabel = ecLayer ? `TB 参数：${displayedTb ? versionText(displayedTb) : "未选择"}` : `版本 ${versionText(current)}`;
  $("tb-version").textContent = displayedTbLabel;
  $("tb-version").title = displayedTbLabel;
  const banner = $("dependency-banner");
  banner.hidden = !state.run.dependency_stale;
  banner.textContent = `本次 EC 产物与选定 TB 参数或当前配置不一致，需要从 EC L1 重新校正。`;
  banner.hidden = !ecLayer || state.layer === "EC_L0" || !state.run.dependency_stale;
  banner.textContent = "TB 参数或输入已更新，运行 EC L1 更新校正谱。";
  const unavailable = state.run.stages?.[state.layer]?.unavailable_reason;
  if (unavailable) { banner.hidden = false; banner.textContent = unavailable; }
  else if (state.layer === "EC_L0") banner.hidden = true;
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
  renderPayloadOptions(); $("payload").value = run.payload; $("run-identity").textContent = cleanRunName(run.name, run.payload) || "未命名试算"; $("run-identity").title = `runs/${run.id}`;
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
  const link = document.createElement("a"); link.href = item.url; link.target = "_blank"; link.rel = "noopener"; link.click();
}

function renderJobs() {
  const jobs = [...(state.run?.jobs || [])].reverse(); const list = clear($("job-list"));
  $("job-count").textContent = jobs.length;
  if (!jobs.length) { list.append(node("p", "empty-list", "暂无运行记录。")); return; }
  for (const job of jobs) {
    const row = node("div", "job-row"); const summary = node("div", "job-summary");
    const pointId = job.config?.ec_point_id || job.point_id, pointParts = pointId ? String(pointId).split(":") : [];
    const selectedPeak = pointId ? Number.isFinite(Number(pointParts.at(-1))) ? `${number(pointParts.at(-1), 3)} keV · CH ${pointParts.at(-2)} · ${pointId}` : String(pointId) : "";
    const only = Array.isArray(job.config?.only) ? job.config.only.join(", ") : job.config?.only;
    const scope = selectedPeak || (job.stem || only ? `当前文件 · ${job.stem || only}` : job.scope === "all" || job.representative === false ? "全部数据" : job.scope === "selected" ? "所选输入" : "代表样例");
    summary.append(node("strong", "job-layer", job.layer || "任务"), badge(job.status), node("span", "tiny-text", scope), node("time", "job-time", timestamp(job.finished_at || job.started_at || job.created_at)));
    row.append(summary);
    if (job.message || job.result?.message) row.append(node("div", "job-details", valueText(job.message || job.result.message)));
    if (job.error) row.append(node("div", "job-details job-error", jobErrorText(job.error)));
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

async function renderTBSurface() {
  const context = plotContext();
  if (!window.Plotly) throw new Error("本地 Plotly 资源未加载，请检查 /static/plotly.min.js。");
  $("residual-chart").classList.remove("metadata-chart");
  const data = state.surface; const points = data.points || []; const traces = [];
  const residualMode = state.surfaceMode === "residual";
  const residualMax = Math.max(.1, ...points.map((p) => Math.abs(residualPercent(p))).filter(Number.isFinite));
  if (data.grid?.gain?.length && !residualMode) traces.push({ type: "surface", x: data.grid.temp, y: data.grid.bias, z: data.grid.gain, colorscale: [[0, "#e2e8f0"], [.5, "#b5c5d7"], [1, "#718eaf"]], opacity: .46, showscale: false, hoverinfo: "skip", contours: { z: { show: false } }, lighting: { ambient: .85, diffuse: .55, roughness: 1, specular: .05 }, name: "当前拟合曲面" });
  if (points.length) {
    const xx = [], yy = [], zz = [];
    for (const p of points) { if (!Number.isFinite(residualPercent(p))) continue; const prediction = p.predicted ?? p.center / (1 + residualPercent(p) / 100); xx.push(p.temp, p.temp, null); yy.push(p.bias, p.bias, null); zz.push(residualMode ? 0 : prediction, residualMode ? residualPercent(p) : p.center, null); }
    traces.push({ type: "scatter3d", mode: "lines", x: xx, y: yy, z: zz, line: { color: "#8192a5", width: 3 }, hoverinfo: "skip", name: "测量与曲面之间的残差" });
  }
  const groups = [{ excluded: false, color: chartColors.blue, name: "参与拟合", matches: isUsed }, { excluded: true, color: chartColors.excluded, name: "人工屏蔽", matches: isExcluded }, { excluded: false, color: chartColors.muted, name: "无可用参数", matches: (point) => !isExcluded(point) && pointUsage(point) === false }];
  for (const group of groups) {
    const values = points.filter(group.matches);
    traces.push({ type: "scatter3d", mode: "markers", x: values.map((p) => p.temp), y: values.map((p) => p.bias), z: values.map((p) => residualMode ? residualPercent(p) : p.center), customdata: values.map((p) => p.id || p.stem), text: values.map((p) => `${p.stem}<br>${group.name}<br>峰位 ${number(p.center, 2)} ADC · 残差 ${number(residualPercent(p), 3)}%`), hovertemplate: "%{text}<br>T = %{x:.2f} °C · V = %{y:.3f} V<extra></extra>", marker: { size: 4.5, color: group.excluded || group.color === chartColors.muted ? group.color : values.map(residualPercent), colorscale: [[0,"#2b65ac"],[.5,"#f1f3f5"],[1,"#b54b37"]], cmin: -residualMax, cmax: residualMax, showscale: group.name === "参与拟合", colorbar: { title: { text: "残差 / %", font: chartFont }, thickness: 9, len: .65, x: 1, tickfont: { ...numericFont, size: 11 } }, line: { color: "#64778a", width: .8 }, opacity: 1, symbol: group.excluded ? "x" : "circle" }, name: group.name });
  }
  traces.push(selectedPointTrace(true));
  const axis = (text) => ({ title: { text, font: chartFont }, tickfont: numericFont, gridcolor: "#dfe4e9", zeroline: false, linecolor: "#c4ccd4", backgroundcolor: "#fafbfc", showbackground: true, showspikes: false });
  const viewport = surfaceViewport();
  $("surface-chart").dataset.cameraMode = viewport.mode;
  await Plotly.react(plotTarget($("surface-chart")), traces, { margin: { l: 16, r: 65, t: 0, b: 25 }, paper_bgcolor: "#ffffff", font: chartFont, hoverlabel: chartHover, showlegend: false, uirevision: `${state.run.id}-${state.channel}-${viewport.mode}-${state.surfaceMode}`, scene: { xaxis: axis("T / °C"), yaxis: axis("Bias / V"), zaxis: axis(residualMode ? "残差 / %" : "峰位 / ADC"), camera: viewport.camera, aspectratio: { x: 1.5, y: 1, z: .8 } } }, plotConfig);
  if (context !== plotContext()) return;
  bindPointClicks($("surface-chart"));
  const residualTraces = [];
  for (const group of groups) {
    const values = points.filter(group.matches);
    residualTraces.push({ type: "scatter", mode: "markers", x: values.map((p) => p.temp), y: values.map((p) => residualPercent(p)), customdata: values.map((p) => p.id || p.stem), text: values.map((p) => `${p.stem}<br>${group.name} · ${number(p.bias, 3)} V`), marker: { color: group.color, size: 7, opacity: .95, symbol: group.excluded ? "x" : "circle", line: { color: "#fff", width: .8 } }, hovertemplate: "%{text}<br>温度 %{x:.2f} °C · 残差 %{y:.3f}%<extra></extra>", name: group.name });
  }
  residualTraces.push(selectedPointTrace(false));
  await Plotly.react(plotTarget($("residual-chart")), residualTraces, { margin: { l: 64, r: 22, t: 16, b: 43 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, xaxis: { ...axisStyle, title: { text: "温度 / °C", standoff: 6 } }, yaxis: { ...axisStyle, title: { text: "残差 / %", standoff: 8 }, zeroline: true, zerolinecolor: "#9daab6", zerolinewidth: 1, rangemode: "tozero" }, hovermode: "closest", uirevision: `${state.run.id}-${state.channel}` }, plotConfig);
  if (context !== plotContext()) return;
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
  const context = plotContext();
  const point = state.point; if (!point || !window.Plotly) return;
  for (const [id, threeDimensional] of [["surface-chart", true], ["residual-chart", false]]) {
    const chart = $(id); const index = chart.data?.findIndex((trace) => trace.meta === "selected-point");
    if (!(index >= 0)) continue;
    const text = `当前选中 · ${point.stem}${isExcluded(point) ? " · 人工屏蔽" : hasQualityWarning(point) ? " · 疑似问题" : pointUsage(point) === false ? " · 无可用参数" : ""}`;
    await Plotly.restyle(chart, { x: [[point.temp]], y: [[threeDimensional ? point.bias : residualPercent(point)]], ...(threeDimensional ? { z: [[state.surfaceMode === "residual" ? residualPercent(point) : point.center]] } : {}), customdata: [[point.id || point.stem]], text: [[text]] }, [index]);
  if (context !== plotContext()) return;
  }
}

function residualPercent(point) {
  if (point.residual_percent !== undefined) return Number(point.residual_percent);
  if (point.predicted && point.center !== undefined) return 100 * (point.center - point.predicted) / point.predicted;
  return point.residual !== undefined ? Number(point.residual) : NaN;
}

function filteredL1Files() {
  const search = $("l1-file-search").value.trim().toLowerCase(), filter = $("l1-file-filter").value || "all";
  return (state.l1Overview?.files || []).filter(file => file.has_l0 && (!search || `${file.stem} ${file.channels[state.channel]?.temp ?? ""} ${file.channels[state.channel]?.bias ?? ""}`.toLowerCase().includes(search)) && (filter === "all" || file.status === filter));
}

const l1QualityStates = {
  accepted: { label: "未见异常", symbol: "circle", glyph: "●" },
  suspect: { label: "疑似问题", symbol: "diamond", glyph: "◆" },
  failed: { label: "计算失败", symbol: "x", glyph: "×" },
  model_pending: { label: "模型待定", symbol: "square-open", glyph: "□" },
  not_fitted: { label: "未拟合", symbol: "circle-open", glyph: "○" },
  quality_unknown: { label: "质量未知", symbol: "triangle-up-open", glyph: "△" }
};
function l1QualityNumber(value) { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function l1QualityContext() { return `${state.run?.id}:${state.layer}:${state.channel}:${state.loadToken}`; }
function l1QualityPoints(channel = state.channel) {
  return (state.l1Overview?.files || []).map(file => {
    const point = file.channels?.find(item => item.channel === channel) || {};
    return { ...point, stem: file.stem, channel, id: point.id || `${state.run?.payload}:${file.stem}:${channel}`, has_l0: file.has_l0 };
  });
}
function l1QualityStatus(point) {
  if (point.fit_status === "model_pending" || point.unsupported_spectral_model) return "model_pending";
  if (point.fit_status === "failed") return "failed";
  if (["not_fitted", "not_parsed"].includes(point.fit_status)) return "not_fitted";
  const accepted = Object.hasOwn(point, "quality_accepted") ? point.quality_accepted : point.fit_quality?.accepted;
  if (hasQualityWarning(point) || accepted === false) return "suspect";
  if (accepted === true) return "accepted";
  return point.fit_status === "fitted" || l1QualityNumber(point.center) !== null ? "quality_unknown" : "not_fitted";
}
function l1QualitySummary(points) {
  const counts = Object.fromEntries(Object.keys(l1QualityStates).map(key => [key, 0]));
  for (const point of points) counts[l1QualityStatus(point)]++;
  return { counts, total: points.length,
    missingCoordinates: points.filter(point => l1QualityNumber(point.temp) === null || l1QualityNumber(point.bias) === null).length,
    missingMetrics: points.filter(point => l1QualityNumber(point.chi2_reduced) === null).length };
}
function l1QualityHover(point) {
  const escape = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const reason = point.error || point.unsupported_spectral_model || (point.warning_reasons || point.rejection_reasons || point.fit_quality?.warnings || []).join("；");
  const previous = l1QualityStatus(point) === "failed" ? "上次拟合 " : "";
  return [escape(point.stem), `CH ${point.channel} · ${l1QualityStates[l1QualityStatus(point)].label}`,
    `实测 T ${number(point.temp, 3)} °C · Bias ${number(point.bias, 4)} V`,
    `约化 χ² ${number(point.chi2_reduced, 4)} · ${previous}R² ${number(point.rsquared, 5)}`,
    `${previous}ΔBIC ${number(point.fit_quality?.delta_bic_background, 3)} · 峰面积 SNR ${number(point.fit_quality?.amplitude_snr, 3)}`,
    `${previous}窗口 ${number(point.window_lo, 3)}–${number(point.window_hi, 3)} ADC`,
    `${l1QualityStatus(point) === "failed" ? "失败原因" : l1QualityStatus(point) === "model_pending" ? "模型" : "问题提示"}：${escape(reason || "—")}`,
    ...(point.pending_window ? ["窗口待重拟合"] : []), ...(point.excluded ? ["已人工屏蔽"] : [])].join("<br>");
}
function l1QualityTraces(points) {
  const traces = [];
  for (const [status, style] of Object.entries(l1QualityStates)) for (const colored of [true, false]) {
    const values = points.filter(point => l1QualityStatus(point) === status &&
      l1QualityNumber(point.temp) !== null && l1QualityNumber(point.bias) !== null &&
      (l1QualityNumber(point.chi2_reduced) !== null) === colored);
    if (!values.length) continue;
    traces.push({ type: "scatter", mode: "markers", name: style.label, meta: { quality_status: status, metric_missing: !colored },
      x: values.map(point => point.temp), y: values.map(point => point.bias),
      customdata: values.map(({ id, stem, channel }) => ({ id, stem, channel })), text: values.map(l1QualityHover),
      hovertemplate: "%{text}<extra></extra>", marker: { symbol: style.symbol,
        size: values.map(point => point.id === state.point?.id ? 14 : 10),
        color: colored ? values.map(point => point.chi2_reduced) : "#8a949e",
        ...(colored ? { coloraxis: "coloraxis" } : {}),
        line: { color: values.map(point => point.id === state.point?.id ? "#172a3e" : "#64778a"),
          width: values.map(point => point.id === state.point?.id ? 2.4 : .8) } }, showlegend: false });
  }
  return traces;
}
async function selectL1QualityPoint(identity, context) {
  if (context !== l1QualityContext() || state.layer !== "TB_L1" || identity?.channel !== state.channel) return;
  const point = l1QualityPoints().find(item => item.id === identity.id && item.stem === identity.stem && item.channel === identity.channel);
  if (point) await selectPoint(point);
}
async function renderL1QualityOverview() {
  const panel = $("l1-quality-overview");
  if (!panel || state.layer !== "TB_L1" || !state.run || !state.l1Overview) return;
  panel.hidden = false;
  const context = l1QualityContext(), token = state.l1QualityRenderToken = (state.l1QualityRenderToken || 0) + 1;
  const points = l1QualityPoints(), summary = l1QualitySummary(points), counts = clear($("l1-quality-counts"));
  $("l1-quality-channel").textContent = `CH ${state.channel}`;
  for (const [status, style] of Object.entries(l1QualityStates)) {
    const item = node("span", `l1-quality-count status-${status}`, `${style.glyph} ${style.label} ${summary.counts[status]}`);
    item.dataset.qualityStatus = status; counts.append(item);
  }
  $("l1-quality-note").textContent = `CH ${state.channel} · ${summary.total} 个工况 · ${summary.total - summary.missingCoordinates} 个已定位 · 约化 χ² 缺失 ${summary.missingMetrics}${summary.missingCoordinates ? ` · 缺少实测坐标 ${summary.missingCoordinates}` : ""} · 灰色表示指标缺失${state.point ? ` · 当前复核 ${state.point.stem}` : ""}`;
  const unlocated = $("l1-quality-unlocated"); unlocated.hidden = !summary.missingCoordinates;
  const missingList = clear($("l1-quality-unlocated-list"));
  for (const point of points.filter(item => l1QualityNumber(item.temp) === null || l1QualityNumber(item.bias) === null)) {
    const button = node("button", "text-button", `${point.stem} · CH ${point.channel} · ${l1QualityStates[l1QualityStatus(point)].label}`);
    button.type = "button"; button.addEventListener("click", handleError(() => selectL1QualityPoint(point, context))); missingList.append(button);
  }
  if (!window.Plotly) { chartMessage("l1-quality-chart", "Plotly 未加载，暂无法显示质量总览"); return; }
  const render = async () => {
    if (token !== state.l1QualityRenderToken || context !== l1QualityContext() || state.layer !== "TB_L1") return;
    const chart = $("l1-quality-chart");
    const metrics = [0, 1, 2, 3].flatMap(channel => l1QualityPoints(channel)).map(point => l1QualityNumber(point.chi2_reduced)).filter(value => value !== null);
    await Plotly.react(plotTarget(chart), l1QualityTraces(points), basicPlotLayout("实测温度 / °C", "实测偏压 / V", {
      margin: { l: 72, r: metrics.length ? 105 : 28, t: 12, b: 46 },
      coloraxis: { colorscale: "Viridis", cmin: 0, cmax: Math.max(1, ...metrics), showscale: points.some(point => l1QualityNumber(point.chi2_reduced) !== null),
        colorbar: { title: { text: "约化 χ²", font: chartFont }, thickness: 12, len: .86, tickfont: numericFont } },
      uirevision: `${state.run.id}-l1-quality-${state.channel}`,
      annotations: points.length === summary.missingCoordinates ? [{ text: "暂无可定位工况，实测坐标保持缺失", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false }] : []
    }), plotConfig);
    if (token !== state.l1QualityRenderToken || context !== l1QualityContext() || state.layer !== "TB_L1") return;
    chart.removeAllListeners?.("plotly_click");
    chart.on?.("plotly_click", handleError(event => selectL1QualityPoint(event.points?.[0]?.customdata, context)));
  };
  state.l1QualityRender = (state.l1QualityRender || Promise.resolve()).catch(() => {}).then(render).catch(error => {
    if (token === state.l1QualityRenderToken && context === l1QualityContext() && state.layer === "TB_L1") throw error;
  });
  return state.l1QualityRender;
}

function filteredEcReviewPoints() {
  const search = $("peak-file-search").value.trim().toLowerCase();
  return (state.ecOverview?.points || []).filter(point => !search || `${point.stem || point.source_name || ""} ${point.E ?? ""}`.toLowerCase().includes(search));
}

function renderEcReviewFiles() {
  if (state.layer !== "EC_L2") return;
  const points = filteredEcReviewPoints(), select = clear($("point-select"));
  for (const point of points) select.append(new Option(`${number(point.E, 3)} keV · ${point.stem || point.source_name || "能线"}`, point.id));
  select.value = state.point?.id || "";
  const index = points.findIndex(point => point.id === state.point?.id);
  $("peak-file-position").textContent = `${index < 0 ? "—" : index + 1} / ${points.length}`;
  $("peak-file-prev").disabled = state.selecting || state.busy || index <= 0;
  $("peak-file-next").disabled = state.selecting || state.busy || !points.length || index === points.length - 1;
  $("point-count").textContent = `${(state.ecOverview?.points || []).length} 条能线`;
}

async function moveEcReviewFile(offset) {
  const points = filteredEcReviewPoints(), index = points.findIndex(point => point.id === state.point?.id);
  const point = points[index + offset];
  if (point) await selectEcPoint(point);
}

function renderL1Workspace() {
  if (state.layer !== "TB_L1") return;
  const files = filteredL1Files(), all = state.l1Overview?.files || [], holder = $("l1-file-list"), scroll = holder.scrollTop || 0, previous = holder.dataset.selectedStem;
  clear(holder);
  for (const file of files) {
    const button = node("button", `l1-file-row${file.stem === state.point?.stem ? " active" : ""} status-${file.status === "attention" ? "attention" : file.status === "fitted" ? "success" : "pending"}`);
    button.type = "button"; button.dataset.l1Stem = file.stem;
    button.title = file.stem;
    button.setAttribute("aria-current", String(file.stem === state.point?.stem));
    const channel = file.channels[state.channel] || {}, condition = Number.isFinite(channel.temp) ? `${number(channel.temp, 1)} °C · ${number(channel.bias, 3)} V` : "";
    const supported = supportedFitChannels(), fittedCount = file.channels.length ? supported.filter(ch => ["fitted", "suspect", "rejected"].includes(file.channels[ch]?.fit_status)).length : file.fitted;
    const problem = file.channels.some(item => item.fit_status === "failed") ? " · 计算失败" : file.status === "attention" ? " · 疑似问题" : "";
    button.append(node("span", "l1-file-name", file.stem), node("span", "l1-file-condition", condition), node("span", "l1-file-state", `${fittedCount} / ${supported.length} 已拟合${problem}`));
    button.addEventListener("click", handleError(() => selectPoint({ ...channel, stem:file.stem, id:channel.id || `${state.run.payload}:${file.stem}:${state.channel}` })));
    holder.append(button);
  }
  holder.scrollTop = scroll;
  if (previous !== state.point?.stem) holder.querySelector(".active")?.scrollIntoView?.({block:"nearest",inline:"nearest"});
  holder.dataset.selectedStem = state.point?.stem || "";
  if (!files.length) holder.append(node("p", "empty-list", "没有匹配文件"));
  const index = files.findIndex(file => file.stem === state.point?.stem);
  $("l1-file-position").textContent = `${index < 0 ? "—" : index + 1} / ${files.length}`;
  $("l1-prev").disabled = index <= 0; $("l1-next").disabled = !files.length || index === files.length - 1;
  $("point-count").textContent = `${all.filter(file => file.has_l0).length} 个文件`;
  const channels = all.find(file => file.stem === state.point?.stem)?.channels || [], summary = clear($("l1-channel-summary"));
  for (const channel of channels) {
    const unsupported = unsupportedSpectralModel(channel.channel, channel, null), attention = !unsupported && (channel.fit_status === "failed" || hasQualityWarning(channel)), fitted = !unsupported && ["fitted", "suspect", "rejected"].includes(channel.fit_status) && !channel.pending_window;
    const card = node("button", `l1-channel-card${channel.channel === state.channel ? " active" : ""} status-${attention ? "attention" : fitted ? "success" : "pending"}`);
    card.type = "button"; card.dataset.l1Channel = channel.channel;
    card.setAttribute("aria-pressed", String(channel.channel === state.channel));
    const heading = node("span", "channel-heading"); heading.append(node("span", "channel-name", `CH ${channel.channel}`), node("span", "channel-state", unsupported ? "待定" : channel.pending_window ? "待重拟合" : channel.fit_status === "failed" ? "计算失败" : attention ? "疑似问题" : fitted ? "已拟合" : "未拟合"));
    const value = node("span", "channel-value", unsupported ? "—" : number(channel.center, 2)); if (!unsupported) value.append(node("small", "", " ADC"));
    card.append(heading, value, node("span", "channel-quality", unsupported ? "康普顿响应" : `R² ${number(channel.rsquared, 4)}`));
    card.title = unsupported || (channel.error ? jobErrorText(channel.error) : `CH ${channel.channel} · 峰位 ${number(channel.center, 3)} ± ${number(channel.center_err, 3)} ADC`);
    card.addEventListener("click", handleError(() => selectChannel(channel.channel))); summary.append(card);
  }
  renderL1Progress();
  renderL1QualityOverview().catch(messageError);
}

async function moveL1File(offset) {
  const files = filteredL1Files(), index = files.findIndex(file => file.stem === state.point?.stem);
  const file = files[index + offset]; if (!file) return;
  const point = file.channels[state.channel]; await selectPoint({ ...point, stem:file.stem, id:point.id || `${state.run.payload}:${file.stem}:${state.channel}` });
}

async function selectChannel(channel) {
  if (channel === state.channel || !Number.isInteger(channel) || channel < 0 || channel > 3) return;
  const token = state.channelToken = (state.channelToken || 0) + 1;
  rememberView(); ++state.loadToken; ++state.spectrumToken; state.selecting = true;
  $("fit-spectrum").disabled = true; $("auto-fit-spectrum").disabled = true;
  if (state.dirty) await saveRevision({}, true);
  if (token !== state.channelToken) return;
  const stem = state.point?.stem || preferredPoint().stem;
  state.channel = channel; $("channel").value = channel; state.dirty = false; state.spectrum = null;
  if (state.layer === "TB_L1" && state.l1Overview) {
    const file = state.l1Overview.files?.find(file => file.stem === stem);
    state.surface.points = l1QualityPoints(channel).filter(point => point.has_l0);
    if (file) { await selectPoint(l1QualityPoints(channel).find(point => point.stem === stem)); return; }
  }
  state.point = null; rememberView(); await loadSurfaceSafely();
}

function renderL1Progress(job = [...(state.run?.jobs || [])].reverse().find(item => item.layer === "TB_L1")) {
  if (state.layer !== "TB_L1") return;
  const holder = $("l1-progress"); holder.hidden = !job; if (!job) return;
  let progress;
  for (const line of (job.log || "").split("\n")) if (line.includes("L1_PROGRESS ")) { try { progress = JSON.parse(line.split("L1_PROGRESS ").at(-1)); } catch {} }
  const summary = job.result?.summary, processor = job.result?.processor;
  const outcome = summary ? processor?.channels_accepted != null
    ? `${summary.channels_succeeded ?? summary.fitted ?? 0} 通道已拟合 · ${processor.channels_accepted} 未见异常 · ${processor.channels_suspect ?? processor.channels_rejected ?? 0} 疑似问题${(summary.channels_failed ?? summary.failed ?? 0) ? ` · ${summary.channels_failed ?? summary.failed} 计算失败` : ""}`
    : `${summary.channels_succeeded ?? summary.fitted ?? 0} 通道完成 · ${summary.channels_failed ?? summary.failed ?? 0} 通道待复核` : "";
  holder.dataset.status = job.status;
  holder.textContent = ["最近计算", statusNames[job.status] || (job.status === "partial" ? "部分完成" : job.status), progress ? `${progress.completed} / ${progress.total} 文件 · ${progress.stem}` : job.config?.only?.join(", ") || "全部文件", outcome].filter(Boolean).join(" · ");
}

async function loadSurface() {
  if (!state.run) return;
  if (state.layer.startsWith("EC")) return loadEcAnalysis();
  const token = ++state.loadToken;
  const selection = state.spectrumToken;
  if (state.layer === "TB_L1") {
    state.l1Overview = null; $("l1-quality-overview").hidden = true;
    state.l1QualityRenderToken = (state.l1QualityRenderToken || 0) + 1;
  }
  const data = await api(runPath(state.layer === "TB_L1" ? "/l1-overview" : `/surface?channel=${state.channel}&layer=${state.layer}`));
  if (token !== state.loadToken) return;
  if (state.layer === "TB_L1") {
    state.l1Overview = data;
    data.points ||= l1QualityPoints().filter(point => point.has_l0);
  }
  if (state.layer === "TB_L0") {
    data.points = (data.files || []).map((f) => { const file = typeof f === "string" ? { stem: f } : f; return { ...(data.points || []).find((p) => p.stem === file.stem), ...file, id: `${state.run.payload}:${file.stem}:${state.channel}`, channel: state.channel }; });
    for (const raw of state.run.inventory?.raw?.tb_samples || []) if (!data.points.some(point => point.stem === raw.stem)) data.points.push({...raw, id:`${state.run.payload}:${raw.stem}:${state.channel}`,channel:state.channel,fit_status:"not_parsed",used:false});
  }
  state.surface = data;
  renderUnpackWorkspace();
  renderL1Workspace();
  const select = clear($("point-select"));
  for (const point of data.points || []) select.append(new Option(pointLabel(point), point.id || point.stem));
  await renderSurface();
  if (token !== state.loadToken || selection !== state.spectrumToken) return;
  const previousStem = state.point?.stem || preferredPoint().stem;
  const selected = (data.points || []).find((p) => p.stem === previousStem) || (data.points || []).find((p) => !isExcluded(p)) || data.points?.[0];
  if (selected) await selectPoint(selected); else {
    state.point = null; state.spectrum = null;
    $("point-select").append(new Option("本通道暂无可用工况", ""));
    $("fit-spectrum").disabled = true; $("toggle-excluded").disabled = true;
    Plotly.purge($("spectrum-chart")); clear($("spectrum-params"));
  }
}

async function loadSurfaceSafely() {
  const context = viewContext();
  try { await loadSurface(); }
  catch (error) { if (context !== viewContext()) return; state.spectrum=null; $("window-status").textContent="输入不可用于本次拟合"; chartMessage("spectrum-chart",error.message); messageError(error); }
}

async function selectPoint(point) {
  if (state.layer.startsWith("EC")) return selectEcPoint(point);
  const token = ++state.spectrumToken;
  state.selecting = true;
  $("fit-spectrum").disabled = true; $("auto-fit-spectrum").disabled = true;
  $("window-lo").disabled = true; $("window-hi").disabled = true;
  $("l1-fit-all-channels").disabled = true;
  try { if (state.dirty) await saveRevision({}, true); }
  catch (error) { if (token === state.spectrumToken) { state.selecting = false; setBusy(state.busy); } throw error; }
  if (token !== state.spectrumToken) return;
  if (state.point?.id !== point.id) { $("peak-noise-ceiling").value=""; $("peak-expected-center").value=""; }
  $("auto-fit-status").textContent=""; fitFeedback();
  state.point = point; state.spectrum = null; state.dirty = false; $("point-select").value = point.id || point.stem;
  renderL1Workspace();
  if (state.layer === "TB_L1") { chartMessage("surface-chart", `正在读取 ${point.stem} · CH ${state.channel}…`); chartMessage("residual-chart", "读取拟合残差…"); $("surface-caption").textContent = `${point.stem} · CH ${state.channel} · 读取中`; $("residual-metric").textContent = ""; $("peak-diagnostics").hidden = true; }
  if (state.layer === "TB_L0") { chartMessage("surface-chart", "正在读取所选文件的真实脉冲谱…"); $("surface-caption").textContent = `${point.stem} · CH ${state.channel} · 读取中`; }
  rememberView(); renderCurrentStatus(); renderUnpackWorkspace();
  await highlightSelectedPoint();
  if (token !== state.spectrumToken) return;
  displayWindowValue("window-lo", ""); displayWindowValue("window-hi", "");
  $("window-lo").disabled = true; $("window-hi").disabled = true;
  clear($("spectrum-params"));
  if (window.Plotly) Plotly.purge($("spectrum-chart"));
  if (state.layer === "TB_L0" && point.fit_status === "not_parsed") {
    $("window-status").textContent = "尚未解析";
    chartMessage("surface-chart", `${point.stem} 尚未生成 L0。点击上方“解包选中文件”调用实际解包器。`);
    chartMessage("residual-chart", `事件：${point.event || "—"}\n遥测：${point.hk || "—"}`);
    renderMetadata({source:"raw",source_path:point.event,stem:point.stem,metadata:{状态:"待解包",文件大小:`${number((point.bytes||0)/1e6,2)} MB`}});
    state.selecting = false; setBusy(state.busy); return;
  }
  clear($("spectrum-chart")).append(node("p", "chart-loading", "正在读取对应工况的真实能谱…"));
  $("selected-channel").textContent = `CH ${state.channel}`;
  const condition = clear($("point-condition"));
  for (const [key, value] of [["温度", `${number(point.temp, 2)} °C`], ["偏压", `${number(point.bias, 3)} V`]]) { const item = node("span", "", key); item.append(node("strong", "", value)); condition.append(item); }
  $("window-status").textContent = "读取真实谱…";
  $("fit-spectrum").disabled = true;
  try {
    const spectrum = await api(runPath(`/spectrum?stem=${encodeURIComponent(point.stem)}&channel=${state.channel}&layer=${state.layer === "TB_L0" ? "TB_L0" : "TB_L1"}`));
    if (token !== state.spectrumToken) return;
    state.spectrum = spectrum;
    if (spectrum.hk_data) {
      const condition = clear($("point-condition"));
      for (const [label, key, unit] of [["温度", "temp", "°C"], ["偏压", "bias", "V"]]) { const value = spectrum.hk_data[key]?.[state.channel]; const item = node("span", "", label); item.append(node("strong", "", `${number(value, 3)} ${unit}`)); condition.append(item); }
    }
    const fit = spectrum.fit || {};
    const override = fit.peak_detection?.manual_window === false && !(state.run.pending_windows || []).includes(point.id) ? null : state.run.config?.windows?.[point.stem]?.[String(state.channel)];
    displayWindowValue("window-lo", override?.lo ?? fit.window_lo ?? fit.lo ?? spectrum.histogram_config?.min ?? "");
    displayWindowValue("window-hi", override?.hi ?? fit.window_hi ?? fit.hi ?? spectrum.histogram_config?.max ?? "");
    $("window-lo").disabled = state.busy; $("window-hi").disabled = state.busy;
    $("window-status").textContent = unsupportedSpectralModel() ? "康普顿响应 · 拟合方案待定" : (state.run.pending_windows || []).includes(point.id) ? "修订已保存 · 待拟合" : override ? "已应用修订窗" : spectrum.fit ? "当前拟合窗" : "尚未拟合 · 可自动寻峰或设置窗口";
    const params = clear($("spectrum-params"));
    for (const [label, value] of [["峰位 / ADC", `${number(fit.center, 3)} ± ${number(fit.center_err, 3)}`], ["σ / ADC", number(fit.sigma, 3)], ["分辨率", fit.resolution === undefined ? "—" : `${number(fit.resolution * (fit.resolution > 1 ? 1 : 100), 2)}%`], ["R²", number(fit.rsquared, 5)], ["拟合模型", fit.fit_mode || fit.mode || "—"]]) {
      const item = node("div", "param-item"); item.append(node("span", "", label), node("strong", "", value)); params.append(item);
    }
    const quality = spectrum.input_quality;
    if (quality?.crc_available) { const item=node("div","param-item"); item.append(node("span","","CRC 有效事件（全通道）"),node("strong","",`${quality.used_events ?? "—"} / ${quality.input_events}`)); params.append(item); }
    if (spectrum.input_note) params.append(node("p","peak-evidence-warning",spectrum.input_note));
    const attemptError = state.layer === "TB_L1" ? point.error : spectrum.last_fit_attempt?.status === "failed" ? spectrum.last_fit_attempt.error : null;
    if (attemptError) params.append(node("p","peak-evidence-warning",`上次拟合：${jobErrorText(attemptError)}`));
    await renderSpectrum(); if (token !== state.spectrumToken) return; if (state.layer === "TB_L0") await renderRawMain();
  } catch (error) { if (token !== state.spectrumToken) return; $("window-status").textContent = "读取失败"; chartMessage(state.layer === "TB_L1" ? "surface-chart" : "spectrum-chart", `此工况能谱不可用：${error.message}`); if (state.layer === "TB_L1") chartMessage("residual-chart", "当前文件的谱读取失败"); messageError(error); }
  if (token !== state.spectrumToken) return;
  state.selecting = false; setBusy(state.busy);
  updateExcludedButton(); renderRunBadge();
}

async function renderSpectrum() {
  const context = plotContext();
  const data = state.spectrum; if (!data) return;
  if (["TB_L0", "EC_L0", "EC_L1"].includes(state.layer)) { $("spectrum-chart").hidden = true; return; }
  $("spectrum-chart").hidden = isPeakReviewLayer();
  const chart = spectrumChart();
  const rateSpectrum = state.layer.startsWith("EC") && state.layer !== "EC_L0";
  const traces = [{ type: "scatter", mode: "lines", x: data.x || [], y: (data.y || []).map((v) => v > 0 ? v : null), line: { color: chartColors.ink, width: 1.2, shape: "hv" }, name: rateSpectrum ? "净计数率" : "真实计数谱", hovertemplate: rateSpectrum ? "ADC %{x:.1f}<br>净计数率 %{y:.5g}<extra></extra>" : "ADC %{x:.1f}<br>计数 %{y:.0f}<extra></extra>" }];
  const curve = ["TB_L0", "EC_L0", "EC_L1"].includes(state.layer) || unsupportedSpectralModel() ? {} : data.curve || {};
  if (curve.x?.length && curve.y?.length) traces.push({ type: "scatter", mode: "lines", x: curve.x, y: curve.y, line: { color: chartColors.fit, width: 2.1 }, name: "峰 + 背景拟合", hovertemplate: "ADC %{x:.1f}<br>拟合 %{y:.2f}<extra></extra>" });
  else if (curve.x?.length && curve.gaussian?.length) traces.push({ type: "scatter", mode: "lines", x: curve.x, y: curve.gaussian, line: { color: chartColors.fit, width: 1.8, dash: "dash" }, name: "高斯峰分量（历史背景参数缺失）", hovertemplate: "ADC %{x:.1f}<br>高斯分量 %{y:.2f}<extra></extra>" });
  if (curve.x?.length && curve.background?.length) traces.push({ type: "scatter", mode: "lines", x: curve.x, y: curve.background.map((v) => v > 0 ? v : null), line: { color: "#89939d", width: 1.3, dash: "dash" }, name: "背景", hoverinfo: "skip" });
  const lo = windowValue("window-lo"); const hi = windowValue("window-hi"); const width = hi - lo;
  const binWidth = spectrumBinWidth(data);
  const range = width > 0 ? [Math.max(0, lo - width * .6), hi + width * .6] : undefined;
  if (!chart.data) clear(chart);
  const editable = !["TB_L0", "EC_L0", "EC_L1"].includes(state.layer);
  const hasNegative = (data.y || []).some((v) => v < 0);
  if (hasNegative) traces[0].y = data.y;
  const peak = peakPlotDetails(data); traces.push(...peak.traces);
  await Plotly.react(plotTarget(chart), traces, { margin: { l: 55, r: 16, t: editable ? 28 : 14, b: 41 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, xaxis: { ...axisStyle, title: { text: state.layer.startsWith("EC") && state.layer !== "EC_L0" ? "校正 ADC" : "ADC", standoff: 5 }, range }, yaxis: { ...axisStyle, title: { text: data.y_label || (binWidth ? `计数 / ${number(binWidth, 3)} ADC bin` : "计数 / bin"), standoff: 6 }, type: hasNegative ? "linear" : "log", exponentformat: "power" }, shapes: [...(editable ? windowShapes(lo, hi) : []), ...peak.shapes], hovermode: "closest", uirevision: `${state.run.id}-${state.channel}-${state.point.id || state.point.stem}` }, plotConfig);
  if (context !== plotContext()) return;
  if (isPeakReviewLayer()) {
    $("surface-caption").textContent = `${state.point.stem}${state.layer === "EC_L2" ? ` · ${number(state.point.E, 3)} keV` : ""} · CH ${state.channel}${binWidth ? ` · bin ${number(binWidth, 3)} ADC` : ""} · 拖动蓝色边界改窗`;
    $("observed-spectrum-label").textContent = rateSpectrum ? "净计数率" : "计数谱";
    $("fitted-spectrum-label").textContent = curve.y?.length ? "峰 + 背景拟合" : "高斯峰分量";
    $("l1-spectrum-legend").dataset.fitAvailable = String(Boolean(curve.y?.length || curve.gaussian?.length));
    $("l1-spectrum-legend").dataset.backgroundAvailable = String(Boolean(curve.background?.length));
    await Plotly.relayout(chart, {"margin.l":65,"margin.r":22});
    if (context !== plotContext()) return;
    await renderL1Residual(unsupportedSpectralModel() ? { ...data, curve:null } : data, range);
    if (context !== plotContext()) return;
  }
  renderPeakDiagnostics(data);
  installWindowHandles();
  if (isPeakReviewLayer()) bindL1Axes();
  updateSpectralModelControls();
}

function l1Residual(data) {
  const curve = data.curve || {}, observed = new Map((data.x || []).map((x, index) => [x, {count:data.y[index], error:data.yerr?.[index]}]));
  const x = [], y = [];
  for (let i = 0; i < (curve.x || []).length; i++) {
    const prediction = curve.y?.[i], entry = observed.get(curve.x[i]), count = entry?.count;
    if (!Number.isFinite(prediction) || !Number.isFinite(count)) continue;
    const error = Number.isFinite(entry.error) && entry.error > 0 ? entry.error : Math.sqrt(Math.max(count, 1));
    x.push(curve.x[i]); y.push((count - prediction) / error);
  }
  return { x, y };
}

async function renderL1Residual(data, range) {
  const context = plotContext();
  const residual = l1Residual(data), chart = $("residual-chart");
  chart.classList.remove("metadata-chart");
  if (!residual.x.length) { chartMessage("residual-chart", unsupportedSpectralModel() ? "康普顿响应 · 拟合方案待定" : "按当前窗口拟合后显示逐 bin 残差"); $("residual-metric").textContent=""; $("residual-caption").textContent=""; return; }
  const guides = [0, -3, 3].map(value => ({type:"line",xref:"paper",yref:"y",x0:0,x1:1,y0:value,y1:value,line:{color:value ? "#c7cfd7" : "#728294",width:1,dash:value ? "dot" : "solid"}}));
  await Plotly.react(plotTarget(chart), [{type:"scatter",mode:"markers",...residual,marker:{size:3,color:chartColors.blue},hovertemplate:"ADC %{x:.1f}<br>标准化残差 %{y:.2f}<extra></extra>"}], basicPlotLayout("ADC", "标准化残差", {margin:{l:65,r:22,t:8,b:38},xaxis:{...axisStyle,title:{text:"ADC"},range},shapes:guides,uirevision:`${state.run.id}-${state.channel}-${state.point.stem}-residual`}), plotConfig);
  if (context !== plotContext()) return;
  $("residual-metric").textContent = `RMS ${number(Math.sqrt(residual.y.reduce((sum,y)=>sum+y*y,0)/residual.y.length),2)}`;
  $("residual-caption").textContent = state.layer === "EC_L2" ? "(净计数率 − 拟合) / 传播误差 · 虚线 ±3 · 横轴联动" : "(计数 − 拟合) / √max(观测计数, 1) · 虚线 ±3 · 横轴联动";
}

function bindL1Axes() {
  for (const [source, target] of [["surface-chart","residual-chart"],["residual-chart","surface-chart"]]) {
    const chart = $(source);
    if (!chart.data || typeof chart.on !== "function") continue;
    if (chart.l1RangeHandler) chart.removeListener?.("plotly_relayout", chart.l1RangeHandler);
    chart.l1RangeHandler = event => {
      if (!isPeakReviewLayer() || state.axisSync || !$(target).data) return;
      const range = event["xaxis.range"] || (event["xaxis.range[0]"] !== undefined ? [event["xaxis.range[0]"],event["xaxis.range[1]"]] : null);
      if (!range && !event["xaxis.autorange"]) return;
      state.axisSync = true;
      Promise.resolve(Plotly.relayout($(target), range ? {"xaxis.range":range} : {"xaxis.autorange":true})).finally(()=>{state.axisSync=false;});
    };
    chart.on("plotly_relayout", chart.l1RangeHandler);
  }
}

function peakPlotDetails(data) {
  if (unsupportedSpectralModel()) return {traces:[],shapes:[]};
  const detection = data.fit?.peak_detection || data.peak_detection, traces = [], shapes = [];
  if (!detection) return { traces, shapes };
  if (Number.isFinite(detection.noise_boundary)) shapes.push({type:"rect",xref:"x",yref:"paper",x0:0,x1:detection.noise_boundary,y0:0,y1:1,fillcolor:"rgba(155,126,66,.09)",line:{width:0},layer:"below"});
  const candidates = detection.candidates || [], x = data.x || [], y = data.y || [];
  const heights = candidates.map(candidate => { let nearest=0; for(let i=1;i<x.length;i++) if(Math.abs(x[i]-candidate.center)<Math.abs(x[nearest]-candidate.center)) nearest=i; return Math.max(y[nearest] || 0, .01); });
  if (candidates.length) traces.push({type:"scatter",mode:"markers",x:candidates.map(c=>c.center),y:heights,marker:{symbol:"triangle-down",size:8,color:candidates.map(c=>c.noise_candidate?"#a5a9ad":Math.abs(c.center-detection.selected_center)<.001?chartColors.blue:"#aa7a39")},text:candidates.map(c=>`${c.noise_candidate?"噪声候选":"峰候选"} · 显著性 ${number(c.significance,2)} · σ ${number(c.sigma,2)}`),hovertemplate:"%{text}<br>ADC %{x:.2f}<extra></extra>",name:"寻峰候选"});
  return {traces,shapes};
}

function renderPeakDiagnostics(data) {
  const fit=data.fit || data, detection=fit.peak_detection, quality=fit.fit_quality || {}, details=$("peak-diagnostics"), holder=clear($("peak-diagnostics-content"));
  details.hidden=!detection || !!unsupportedSpectralModel(); if(details.hidden) return;
  holder.append(node("p","",`${detection.algorithm || "自动寻峰"} · ${detection.manual_window ? "人工范围内检验" : "自动范围"} · 噪声边界 ${number(detection.noise_boundary,2)} ADC`));
  if(Object.keys(quality).length) holder.append(node("p","",`峰面积 SNR ${number(quality.amplitude_snr,2)} · ΔBIC(背景) ${number(quality.delta_bic_background,2)} · ${hasQualityWarning(fit)?"疑似问题，结果已保留":quality.status === "resolved" ? "未见异常" : quality.status || "已记录"}`));
  for(const warning of new Set([...(detection.warnings||[]),...(quality.warnings||[])])) holder.append(node("p","peak-evidence-warning",warning));
  const table=node("table"), header=node("tr"); ["峰位","σ","显著性","判定"].forEach(t=>header.append(node("th","",t))); table.append(header);
  for(const candidate of detection.candidates||[]) { const row=node("tr"); [number(candidate.center,2),number(candidate.sigma,2),number(candidate.significance,2),candidate.noise_candidate?"噪声候选":Math.abs(candidate.center-detection.selected_center)<.001?"选中":"候选"].forEach(t=>row.append(node("td","",t))); table.append(row); }
  const wrap=node("div","table-wrap"); wrap.append(table); holder.append(wrap); details.open=hasQualityWarning(fit);
}

function updateAutoFitControls() {
  const raw=["TB_L0","EC_L0","EC_L1"].includes(state.layer); $("automatic-peak-controls").hidden=raw;
  $("auto-fit-spectrum").disabled=state.busy || state.selecting || !state.spectrum || !state.point || raw || !!unsupportedSpectralModel() || !!state.run?.stages?.[state.layer]?.unavailable_reason;
  for(const id of ["peak-noise-ceiling","peak-expected-center","peak-min-significance"]) $(id).disabled=state.busy;
}

function updateCapabilityControls() {
  const reason=state.run?.stages?.[state.layer]?.unavailable_reason;
  for(const id of ["run-layer","fit-spectrum","refit-tb","auto-fit-spectrum"]) if(reason) { $(id).disabled=true; $(id).title=reason; }
  $("run-pipeline").disabled=state.busy || !state.run;
}

async function autoFitSpectrum() {
  if(!state.point) return;
  const unsupported = unsupportedSpectralModel(); if (unsupported) throw new Error(unsupported);
  const settings={min_significance:Number($("peak-min-significance").value)}, ec=state.layer.startsWith("EC");
  for(const [id,key] of [["peak-noise-ceiling","noise_ceiling"],["peak-expected-center","expected_center"]]) if($(id).value.trim()!=="") settings[key]=Number($(id).value);
  if(ec && settings.expected_center !== undefined) settings.expected_energy_keV=Number(state.point.E);
  if(Object.values(settings).some(v=>!Number.isFinite(v))) throw new Error("请填写有效的自动寻峰约束。");
  if (state.selecting || !state.spectrum) throw new Error("谱文件正在切换，请等待载入完成。");
  const runId=state.run.id, point=state.point, channel=state.channel;
  const selected=ec?{point_id:point.id,channel,...ecBinningSpec()}:{stem:point.stem,channel,fit_channels:[channel]};
  state.dirty=false; $("auto-fit-status").textContent=""; fitFeedback(`正在自动选窗并拟合 · CH ${channel}…`, "running");
  try { const job=await executeJob(ec?"EC_L2":"TB_L1",{...selected,automatic_window:true,peak_detection:settings}); if (state.run.id===runId && state.point?.id===point.id && state.channel===channel) fitCompletionFeedback(job,true,channel); }
  catch(error) { if (state.run.id===runId && state.point?.id===point.id && state.channel===channel) fitFeedback(error.message,"error"); throw error; }
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
function renderRunBadge() { $("run-state").textContent = state.loading ? "载入中" : !state.run ? "未选择试算" : state.busy ? "运行中" : state.dirty ? "有未保存修改" : "已保存"; $("run-state").className = `badge ${state.busy ? "running" : state.dirty ? "stale" : "neutral"}`; }
function updateExcludedButton() { const excluded = isExcluded(state.point); $("toggle-excluded").textContent = excluded ? "恢复此点" : "屏蔽此点"; $("toggle-excluded").className = `button${excluded ? "" : " danger"}`; $("toggle-excluded").disabled = !state.point || state.busy; }
function setBusy(busy) { state.busy = busy; ["run-layer", "run-pipeline", "refit-tb", "save-run", "new-run", "dataset-settings", "acquisition-settings", "run-select", "payload"].forEach((id) => { $(id).disabled = busy || (!state.run && !["new-run", "dataset-settings", "payload"].includes(id) && !(id === "run-select" && state.bootstrap?.runs?.length)); }); ["fit-spectrum", "window-lo", "window-hi","l1-fit-all-channels"].forEach((id) => { $(id).disabled = busy || state.selecting || !state.spectrum; }); $("review-next-layer").disabled = busy || state.selecting || state.loading; $("apply-tb-version").disabled = busy || !$("tb-version-select").value; $("channel").disabled = state.loading; $("point-select").disabled = state.loading || busy; updateAutoFitControls(); if (state.selecting) $("auto-fit-spectrum").disabled = true; updateExcludedButton(); renderRunBadge(); updateCapabilityControls(); updateSpectralModelControls(); renderEcReviewFiles(); updateUnpackControls(); }

async function resizeCharts() {
  if (!window.Plotly) return;
  for (const id of ["surface-chart", "residual-chart", "spectrum-chart", "ec-spectrum-chart", "l1-quality-chart"]) {
    const chart = $(id);
    if (!chart?.data || chart.getBoundingClientRect().width <= 0 || chart.getBoundingClientRect().height <= 0) continue;
    await Plotly.Plots.resize(chart);
    if (id === "surface-chart" && state.layer === "TB_L2") {
      const viewport = surfaceViewport();
      if (chart.dataset.cameraMode !== viewport.mode) {
        chart.dataset.cameraMode = viewport.mode;
        await Plotly.relayout(chart, { "scene.camera": viewport.camera, uirevision: `${state.run.id}-${state.channel}-${viewport.mode}` });
      }
    }
  }
  installWindowHandles();
}
async function selectLayer(id) { rememberView(); if (state.dirty) await saveRevision({}, true); ++state.loadToken; ++state.spectrumToken; state.selecting = false; state.layer = id; $("run-scope").value = "representative"; $("unpack-search").value = ""; state.surface = null; state.ecOverview = null; state.point = null; state.spectrum = null; rememberView(); ++state.spectrumToken; renderNavigation(); if (state.run) { renderStage(); await loadSurfaceSafely(); requestAnimationFrame(resizeCharts); } }

async function refreshRun(reloadCharts = true) {
  if (!state.run) return;
  state.run = await api(runPath());
  renderRun(); if (reloadCharts) await loadSurfaceSafely();
}

async function openRun(id) {
  if (!id) return;
  rememberView(); const token = ++state.openToken;
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
    state.run = run; state.surface = null; state.ecOverview = null; state.l1Overview = null; state.point = null; state.spectrum = null; state.dirty = false; state.selecting = false;
    const position = savedView(id);
    state.layer = layers.some(layer => layer.id === position.layer) ? position.layer : "TB_L0";
    state.channel = [0,1,2,3].includes(position.channel) ? position.channel : 0; $("channel").value = String(state.channel);
    localStorage.setItem("grid-workbench-active-run", id);
    localStorage.setItem(`grid-workbench-payload:${run.payload}`, id);
    renderRun(); await loadSurfaceSafely();
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
  state.bootstrap = await api("/api/bootstrap"); state.datasets = state.bootstrap.datasets || []; renderPayloadOptions(); renderRunOptions(); renderNavigation();
  $("connection-label").textContent = "本地处理服务已连接";
  const active = state.bootstrap.active_run;
  const ids = (state.bootstrap.runs || []).map((run) => run.id);
  const remembered = localStorage.getItem("grid-workbench-active-run");
  const id = ids.includes(remembered) ? remembered : typeof active === "string" ? active : active?.id || ids[0];
  if (id) await openRun(id); else { state.loading = false; setBusy(false); renderRun(); }
  } catch (error) { state.loading = false; setBusy(false); throw error; }
}

function openCreateDialog(payload = $("payload").value) {
  const dataset = payload === state.run?.payload ? state.run?.dataset : [...state.datasets, ...(state.bootstrap?.datasets || [])].find(item => datasetMatchesPayload(item,payload));
  if (!state.datasetDialogReady || state.datasetDialogRunId !== state.run?.id || state.datasetDialogPayload !== payload) {
    $("dataset-raw-dir").value = dataset?.raw_dir || "";
    $("dataset-product-dir").value = dataset?.product_dir || "";
    state.datasetDialogReady = true; state.datasetDialogRunId = state.run?.id; state.datasetDialogPayload = payload; state.datasetPathsDirty = false;
    if (dataset && !state.datasets.some(item => item.id === dataset.id)) state.datasets = [...state.datasets, dataset];
    renderDatasetOptions(payload);
    if (dataset && state.datasets.some(item => item.id === dataset.id)) { $("dataset-select").value = dataset.id; updateSelectedDataset(); }
  }
  $("new-run-payload").value = payload; $("new-run-name").value = "";
  $("dataset-raw-dir").placeholder = state.bootstrap?.defaults?.raw_dir || "留空使用默认数据目录";
  $("dataset-product-dir").placeholder = state.bootstrap?.defaults?.product_dir || "留空使用默认 products 目录";
  $("dataset-create-error").hidden = true; $("new-run-dialog").showModal(); sourceLibraryRefresh().catch(datasetError);
  if (state.datasetPathsDirty) datasetPathsChanged();
}
function datasetPathsChanged() {
  state.datasetPathsDirty = true; state.datasetScanToken = (state.datasetScanToken || 0) + 1;
  clear($("dataset-select")).append(new Option("目录已改变，请扫描并识别", ""));
  $("new-run-form").querySelector('[type="submit"]').disabled = true;
  $("scan-datasets").disabled = false;
  $("dataset-scan-state").textContent = "目录已改变 · 点击扫描并识别";
  clear($("dataset-paths"));
}

async function createRun(event) {
  event.preventDefault(); const button = event.submitter; if (button) button.disabled = true; $("dataset-create-error").hidden = true;
  try {
    if (state.datasetPathsDirty || !$("dataset-select").value) throw new Error("请先扫描并识别目录，再创建试算。");
    const payload = $("new-run-payload").value.trim(); if (!payload) throw new Error("请填写载荷编号。");
    const result = await api("/api/runs", { method: "POST", body: { dataset_id: $("dataset-select").value || undefined, payload, name: $("new-run-name").value.trim() || undefined, ...registryCreateSpec() } });
  $("new-run-dialog").close(); state.layer = "TB_L0"; state.bootstrap = await api("/api/bootstrap"); state.datasets = state.bootstrap.datasets || state.datasets; await openRun((result.run || result).id);
    notify("试算已创建。", "success");
  } catch (error) { const message = $("dataset-create-error"); message.textContent = error.message; message.hidden = false; message.tabIndex = -1; message.focus(); } finally { if (button) button.disabled = !!state.datasetPathsDirty || !$("dataset-select").value; }
}

function windowPatch() {
  if (!state.point || !state.spectrum) return {};
  const lo = windowValue("window-lo"), hi = windowValue("window-hi");
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || !(lo < hi) || lo < 0) throw new Error("请填写有效拟合窗口：下限 ≥ 0，且上限大于下限。");
  const bounds = spectrumWindowBounds();
  if (hi - lo < bounds.minimum_width) throw new Error(`当前分箱下拟合窗口至少需要 ${number(bounds.minimum_width, 3)} ADC，请扩大窗口。`);
  if (state.layer.startsWith("EC")) return { ec_windows: { [state.point.id]: { lo, hi } } };
  return { windows: { [state.point.stem]: { [String(state.channel)]: { lo, hi } } } };
}

async function saveRevision(extra = {}, silent = false) {
  const runId = state.run.id, context = `${viewContext()}:${state.point?.id}`, values = [$("window-lo").value,$("window-hi").value].join(":");
  const patch = { ...(state.dirty ? windowPatch() : {}), ...extra, reason: $("revision-note").value.trim() || "工作台人工复核" };
  const result = await api(runPath("/config"), { method: "PATCH", body: patch });
  if (state.run.id !== runId) return;
  const run = result.run || result;
  if ((run.config_revision || 0) >= (state.run.config_revision || 0)) state.run = run;
  if (context === `${viewContext()}:${state.point?.id}` && values === [$("window-lo").value,$("window-hi").value].join(":")) state.dirty = false;
  renderRun();
  $("window-status").textContent = "已保存修订窗";
  if (!silent) notify("修订配置与说明已保存，重开此试算可恢复。尚未执行的拟合需要点击计算按钮。", "success");
}

async function toggleExcluded() {
  const point = state.point; if (!point) return;
  const ids = [...excludedIds()]; const id = point.id || `${point.stem}:${state.channel}`; const index = ids.indexOf(id);
  const excluded = isExcluded(point);
  if (excluded && index >= 0) ids.splice(index, 1); else if (!excluded) ids.push(id);
  const ec = state.layer.startsWith("EC");
  await saveRevision({ [ec ? "ec_excluded" : "excluded"]: ids }, true);
  await loadSurfaceSafely();
  notify(`${point.stem} · CH ${state.channel} 已${excluded ? "恢复" : "屏蔽"}。点击“${ec ? "重新拟合 EC L3" : "重新拟合 TB"}”计算新的标定结果。`, "success");
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
  setBusy(true); notify(`${job.layer} 正在运行，已恢复进度。`, "info");
  try {
    while (["queued", "running", "pending"].includes(job.status) && token === state.pollToken) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const response = await api(`/api/runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(job.id)}`); job = response.job || response;
      if (token !== state.pollToken) return;
      const index = state.run.jobs.findIndex((item) => item.id === job.id); if (index >= 0) state.run.jobs[index] = job;
      syncJobStage(job); renderL1Progress(job); if ($("records-drawer").open) renderJobs();
    }
    if (token !== state.pollToken) return;
    await refreshRun(true);
    notify(job.status === "partial" ? `${job.layer} 部分完成，可用结果已保存，请筛选需检查的文件。` : normalizedStatus(job.status) === "succeeded" ? `${job.layer} 计算完成。` : `${job.layer} 计算失败：${jobErrorText(job.error || "详见运行记录")}`, job.status === "partial" ? "warning" : normalizedStatus(job.status) === "succeeded" ? "success" : "error");
  } finally { if (token === state.pollToken) setBusy(false); }
}

function syncJobStage(job) {
  if (!state.run || !job.layer) return;
  const previous = state.run.stages?.[job.layer] || {};
  state.run.stages = { ...(state.run.stages || {}), [job.layer]: { ...previous, status: job.status, stale: ["queued", "running"].includes(normalizedStatus(job.status)) ? false : previous.stale } };
  renderNavigation(); renderUnpackProgress(job);
  renderL1Progress(job);
  if (state.layer === job.layer) renderCurrentStatus();
}

async function executeJob(layer, extra = {}, manageBusy = true) {
  if (manageBusy) setBusy(true);
  try {
    const payload = { layer, representative: $("run-scope").value !== "full", ...extra };
    let job = await api(runPath("/jobs"), { method: "POST", body: payload }); job = job.job || job;
    state.run.jobs = [...(state.run.jobs || []).filter((item) => item.id !== job.id), job];
    state.run.stages = { ...(state.run.stages || {}), [layer]: { ...(state.run.stages?.[layer] || {}), status: job.status || "queued" } };
    renderJobs(); renderNavigation(); renderStage(); renderL1Progress(job);
    notify(`${layer.replace("_", " ")} 正在运行…`, "info");
    while (["queued", "running", "pending"].includes(job.status)) {
      await new Promise((resolve) => setTimeout(resolve, 850));
      const response = await api(runPath(`/jobs/${encodeURIComponent(job.id)}`)); job = response.job || response;
      const index = state.run.jobs.findIndex((item) => item.id === job.id); if (index >= 0) state.run.jobs[index] = job;
      syncJobStage(job); if ($("records-drawer").open) renderJobs();
    }
    await refreshRun(true);
    if (job.status === "partial") { const summary=job.result?.summary; notify(`${layer.replace("_", " ")} 部分完成${summary ? `：${summary.channels_succeeded} 通道完成，${summary.channels_failed} 通道计算失败` : ""}。可用结果已保存。`, "warning"); return job; }
    if (normalizedStatus(job.status) !== "succeeded") throw new Error(`${layer.replace("_", " ")} 失败：${jobErrorText(job.error || job.message)}`);
    notify(`${layer.replace("_", " ")} 已完成。`, "success");
    return job;
  } finally { if (manageBusy) setBusy(false); }
}

async function fitSpectrum() {
  if (!state.point || state.selecting) throw new Error("谱文件正在切换，请等待载入完成。");
  const unsupported = unsupportedSpectralModel(); if (unsupported) throw new Error(unsupported);
  if (!state.spectrum) throw new Error("请先读取当前谱并选择拟合窗口。");
  const runId = state.run.id, point = state.point, channel = state.channel, ec = state.layer.startsWith("EC"), patch = windowPatch(), binning = ec ? ecBinningSpec() : {}; setBusy(true);
  fitFeedback(`正在按当前窗口拟合 · CH ${channel}…`, "running");
  try {
    await saveRevision(patch, true);
    if (runId !== state.run.id) throw new Error("试算已切换，请在当前试算重新选择拟合。");
    const job = await executeJob(ec ? "EC_L2" : "TB_L1", ec ? { point_id:point.id,channel,automatic_window:false,...binning } : { stem:point.stem,channel,fit_channels:[channel],automatic_window:false }, false);
    if (state.run.id===runId && state.point?.id===point.id && state.channel===channel) fitCompletionFeedback(job,false,channel);
    if (job?.status !== "partial") notify(`${point.stem} · CH ${channel} 拟合已完成。`, "success");
  } catch(error) { if (state.run.id===runId && state.point?.id===point.id && state.channel===channel) fitFeedback(error.message,"error"); throw error; }
  finally { setBusy(false); }
}

async function fitAllL1Channels() {
  if (!state.point || state.selecting) throw new Error("谱文件正在切换，请等待载入完成。");
  const runId = state.run.id, selected = {stem:state.point.stem,channel:state.channel,representative:false,fit_channels:supportedFitChannels()};
  setBusy(true);
  try {
    if (state.dirty) await saveRevision({},true);
    if (runId !== state.run.id) throw new Error("试算已切换，请在当前试算重新选择拟合。");
    await executeJob("TB_L1",selected,false);
  } finally { setBusy(false); }
}

async function runCurrentLayer() {
  const runId = state.run.id, layer = state.layer, point = state.point, channel = state.channel, full = $("run-scope").value === "full";
  if (!full && isPeakReviewLayer(layer)) return fitSpectrum();
  if (!full && ["TB_L0","TB_L1","EC_L0","EC_L1","EC_L2"].includes(layer) && (!point || state.selecting)) throw new Error("请等待所选文件载入完成。");
  const extra = point && !full ? ["TB_L0","TB_L1","EC_L0","EC_L1"].includes(layer) ? {stem:point.stem,channel} : layer === "EC_L2" ? {point_id:point.id,channel} : {} : {};
  if (layer==="TB_L1" && hasPlasticChannel()) extra.fit_channels=supportedFitChannels();
  setBusy(true);
  try {
    if (state.dirty) await saveRevision({},true);
    if (runId !== state.run.id) throw new Error("试算已切换，请在当前试算重新选择计算。");
    await executeJob(layer,{...extra,representative:!full},false);
  } finally { setBusy(false); }
}

function configureLayerPanels() {
  const ec = state.layer.startsWith("EC"), raw = ["TB_L0", "EC_L0", "EC_L1"].includes(state.layer);
  const titles = { TB_L0: "工况原始脉冲谱", TB_L1: "当前工况能谱与拟合", TB_L2: "温度–偏压响应与残差", EC_L0: "EC 原始脉冲谱", EC_L1: "EC 温度–偏压校正谱", EC_L2: "当前能线谱与拟合", EC_L3: "能量刻度与拟合残差" };
  $("tb-analysis").dataset.layer = state.layer;
  const l1 = state.layer === "TB_L1", review = isPeakReviewLayer(), ec2 = state.layer === "EC_L2";
  $("l1-quality-overview").hidden = !l1 || !state.l1Overview;
  $("tb-analysis").dataset.peakReview = String(review);
  for (const id of ["l1-workbench","l1-channel-overview","l1-fit-all-channels"]) $(id).hidden = !l1;
  $("l1-spectrum-legend").hidden = !review;
  for (const id of ["peak-file-search","peak-file-controls"]) $(id).hidden = !ec2;
  $("point-select").closest?.(".point-field")?.classList.toggle("l1-hidden",l1);
  $("point-select").hidden = l1;
  $("point-field-label").hidden = l1;
  $("analysis-tab-comparison").hidden = review;
  if (review) showAnalysisView("residual");
  $("fit-spectrum").textContent = "按当前窗口拟合";
  $("main-chart-title").textContent = titles[state.layer]; $("tb-analysis").dataset.layer = state.layer;
  $("surface-chart").setAttribute("aria-label", titles[state.layer]);
  $("inspector-title").textContent = isUnpackLayer() ? "解包结果详情" : ec ? raw ? "数据文件详情" : "能峰复核" : l1 ? "拟合详情" : "工况能谱";
  $("point-field-label").textContent = ec ? raw ? "数据文件" : "能线 / 源峰" : "工况文件";
  $("surface-mode").hidden = state.layer !== "TB_L2";
  $("ec-detail-mode").hidden = state.layer !== "EC_L3";
  $("spectrum-chart").hidden = raw || review;
  $("dependency-toolbar").hidden = !ec || state.layer === "EC_L0";
  $("window-section").hidden = raw;
  $("automatic-peak-controls").hidden = raw;
  if(raw) $("peak-diagnostics").hidden = true;
  document.querySelector(".spectrum-legend").hidden = raw || review;
  $("refit-tb").hidden = raw;
  $("refit-tb").textContent = ec ? "计算 EC L3" : l1 ? "计算 TB L2" : "重新拟合 TB";
  $("fitting-help").textContent = review ? `窗口只用于当前文件、当前通道。拟合后从“下一层”进入${l1 ? "曲面" : "能量刻度"}复核。` : "窗口只用于当前文件、当前通道；修改后重新拟合标定结果。";
  $("analysis-tab-residual").firstChild.textContent = raw ? "数据与元信息 " : "拟合残差 ";
  $("analysis-tab-comparison").textContent = raw ? "输入与依赖" : ec ? "刻度参数" : "参数比较";
  $("residual-caption").textContent = raw ? "当前通道的真实输入数据与文件来源" : ec ? "点击能线点，联动右侧校正谱与拟合窗口" : state.layer === "TB_L1" ? "点击工况点，下钻真实 L0 谱与当前 L1 峰拟合" : "相对曲面的偏差 · 点击测量点定位工况";
  $("main-chart-legend").hidden = raw || review;
  const unpack = isUnpackLayer();
  document.body.classList.toggle("l0-workspace", unpack);
  $("unpack-workspace").hidden = !unpack;
  for (const id of ["run-scope", "run-scope-label", "run-layer"]) $(id).hidden = unpack;
  $("run-scope").options[0].textContent = ["TB_L0", "TB_L1", "EC_L0", "EC_L1"].includes(state.layer) ? "当前文件" : state.layer === "EC_L2" ? "当前能峰" : "代表样例";
  updateRunScopeCaption();
  updateSpectralModelControls();
  if (state.spectrum && ec && state.layer !== "EC_L0") renderSpectrumProvenance(state.spectrum);
  else { $("spectrum-evidence-status").hidden = true; $("spectrum-provenance").hidden = true; }
  if (ec && state.layer !== "EC_L0") loadTbVersions().catch(messageError);
}

function updateRunScopeCaption() { const full = $("run-scope").value === "full", fileLayer = ["TB_L1", "EC_L1"].includes(state.layer); $("run-layer").textContent = full ? "运行本层全部数据" : state.layer === "EC_L1" ? "校正当前文件" : isPeakReviewLayer() ? "拟合当前窗口" : "运行本层"; $("run-layer").title = full ? "处理本层全部适用输入" : isPeakReviewLayer() ? `当前文件 · CH ${state.channel} · 使用图中窗口` : fileLayer ? "仅处理当前选中文件" : "使用当前层的处理器"; }


function basicPlotLayout(x, y, extra = {}) { return { margin: { l: 65, r: 25, t: 20, b: 46 }, font: chartFont, hoverlabel: chartHover, paper_bgcolor: "#fff", plot_bgcolor: "#fff", showlegend: false, hovermode: "closest", xaxis: { ...axisStyle, title: { text: x, standoff: 7 } }, yaxis: { ...axisStyle, title: { text: y, standoff: 7 } }, ...extra }; }

async function renderSurface() {
  const context = plotContext();
  if (state.layer === "TB_L2") return renderTBSurface();
  if (state.layer.startsWith("EC")) return renderEcOverview();
  const points = state.surface?.points || [];
  if (state.layer === "TB_L0") { await renderRawMain(); return; }
  if (state.layer === "TB_L1") { renderL1Workspace(); return; }
  $("residual-chart").classList.remove("metadata-chart");
  const groups = [{ matches: isUsed, name: "参与拟合", color: chartColors.blue }, { matches: isExcluded, name: "已屏蔽", color: chartColors.excluded }, { matches: (p) => !isExcluded(p) && pointUsage(p) === false, name: "无可用参数", color: chartColors.muted }];
  const traces = groups.map((g) => { const ps = points.filter(g.matches); return { type: "scatter", mode: "markers", x: ps.map((p) => p.temp), y: ps.map((p) => p.center), error_y: { type: "data", array: ps.map((p) => p.center_err || 0), visible: true, color: g.color, thickness: 1 }, customdata: ps.map((p) => p.id), text: ps.map((p) => `${p.stem}<br>偏压 ${number(p.bias, 3)} V`), marker: { color: g.color, size: 7, symbol: g.name === "已屏蔽" ? "x" : "circle" }, name: g.name, hovertemplate: "%{text}<br>温度 %{x:.2f} °C<br>峰位 %{y:.3f} ADC<extra></extra>" }; });
  await Plotly.react(plotTarget($("surface-chart")), traces, basicPlotLayout("温度 / °C", "L1 峰位 / ADC"), plotConfig); if (context !== plotContext()) return; bindPointClicks($("surface-chart"));
  if (context !== plotContext()) return;
  await Plotly.react(plotTarget($("residual-chart")), [{ type: "scatter", mode: "markers", x: points.map((p) => p.temp), y: points.map((p) => p.rsquared ?? p.fit?.rsquared ?? null), customdata: points.map((p) => p.id), text: points.map((p) => p.stem), marker: { color: points.map((p) => isExcluded(p) ? chartColors.excluded : pointUsage(p) === false ? chartColors.muted : chartColors.blue), size: 7 }, hovertemplate: "%{text}<br>R² %{y:.5f}<extra></extra>" }], basicPlotLayout("温度 / °C", "R²", { margin: { l: 65, r: 25, t: 10, b: 42 }, yaxis: { ...axisStyle, title: {text:"R²"}, range:[Math.min(0,...points.map(p=>Number(p.rsquared ?? p.fit?.rsquared)).filter(Number.isFinite)),1.05] } }), plotConfig); if (context !== plotContext()) return; bindPointClicks($("residual-chart"));
  if (context !== plotContext()) return;
  $("point-count").textContent = `${points.length} 个工况`; $("surface-caption").textContent = "每个点对应真实 L1 拟合结果，点击复核窗口与谱峰"; $("residual-metric").textContent = ""; renderParams();
}

function renderMetadata(data) {
  const holder = clear($("spectrum-params")), file = data.path || data.source_path || data.stem || "";
  const entries = [["当前通道事件", number(data.n_events,0)], ["谱类型", data.field === "amp_corr" ? "TB 校正谱" : "原始谱"]];
  if (data.input_quality?.input_events) entries.push(["全部事件", number(data.input_quality.input_events,0)]);
  if (data.tb_corrfile) entries.push(["TB 参数",data.tb_corrfile]);
  if (data.metadata) for (const [key,value] of Object.entries(data.metadata)) entries.push([key,valueText(value)]);
  const labels={temp_err:"温度误差 / °C",bias_err:"偏压误差 / V",temp:"温度 / °C",bias:"偏压 / V"};
  if (data.hk_data) for (const [key,value] of Object.entries(data.hk_data)) if (!["temp","bias"].includes(key)) entries.push([labels[key] || key,Array.isArray(value) ? number(value[state.channel],4) : valueText(value)]);
  entries.push(["谱文件",String(file).split(/[\\/]/).pop()]);
  for (const [label,value] of entries) { const item=node("div",label === "谱文件" ? "param-item wide" : "param-item"); if (label==="谱文件") item.title=file; item.append(node("span","",label),node("strong","",value)); holder.append(item); }
}

async function renderRawMain() {
  const context = plotContext();
  const data = state.spectrum;
  if (!data) { chartMessage("surface-chart", "正在载入文件列表与真实脉冲谱…"); return; }
  const traces = [{ type: "scatter", mode: "lines", x: data.x || [], y: (data.y || []).map((n) => n > 0 ? n : null), line: { color: chartColors.ink, width: 1.4, shape: "hv" }, hovertemplate: "ADC %{x:.1f}<br>计数 %{y:.0f}<extra></extra>" }];
  await Plotly.react(plotTarget($("surface-chart")), traces, basicPlotLayout(data.field === "amp_corr" ? "校正 ADC" : "ADC", "计数 / bin", { yaxis: { ...axisStyle, type: "log", title: { text: "计数 / bin" } }, xaxis: { ...axisStyle, title: { text: data.field === "amp_corr" ? "校正 ADC" : "ADC" }, range: data.display_range } }), plotConfig);
  if (context !== plotContext()) return;
  $("surface-caption").textContent = `${data.stem || state.point?.stem} · CH ${state.channel} · ${number(data.n_events ?? data.y?.reduce((a, b) => a + b, 0), 0)} 事件`;
  $("point-count").textContent = `${state.layer === "TB_L0" ? state.surface?.points?.length || 0 : state.ecOverview?.files?.length || 0} 个文件`;
  const chart = $("residual-chart"); if (chart.data) Plotly.purge(chart); clear(chart); chart.classList.add("metadata-chart");
  const table = node("table");
  for (const [key, value] of [["文件", data.path || data.source_path || data.stem], ["数据源", data.source === "run" ? "本次 L0/L1" : "历史 L0/L1"], ["显示事件 / 总事件", `${number(data.events_in_display ?? data.n_events, 0)} / ${number(data.n_events, 0)}`], ["TB 校正参数", data.tb_corrfile || "不适用 / 尚未校正"]]) { const row = node("tr"); row.append(node("th", "", key), node("td", "", value)); table.append(row); }
  chart.append(table); $("residual-metric").textContent = "";
  const params = clear($("parameter-table")); const description = state.run.dataset || {};
  for (const [key, value] of [["原始数据", description.raw_dir || state.run.raw_dir], ["历史产品", description.product_dir || state.run.product_dir], ["本层输入", activeLayer().input], ["本层输出", activeLayer().output]]) params.append(node("p", "input-dependency", `${key}：${value || "工作区默认目录"}`));
  clear($("fit-diagnostics")); renderMetadata(data);
}

async function loadEcAnalysis() {
  const token = ++state.loadToken, runId = state.run.id, layer = state.layer, selection = state.spectrumToken;
  const overview = await api(runPath(`/ec-overview?channel=${state.channel}&layer=${layer}`));
  if (token !== state.loadToken || runId !== state.run.id || layer !== state.layer || selection !== state.spectrumToken) return;
  state.ecOverview = overview;
  const raw = ["EC_L0", "EC_L1"].includes(layer);
  let points = overview.points || [];
  if (raw) {
    let files = overview.files || [];
    if (!files.length) files = artifactsForLayer().filter((a) => /\.parquet$/i.test(a.name || a.path)).map((a) => ({ stem: String(a.name || a.path).split(/[\\/]/).pop().replace(/\.l[01]\.parquet$/, ""), path: a.path }));
    points = files.map((f) => typeof f === "string" ? { id: f, stem: f } : { ...f, id: f.id || f.stem, stem: f.stem || f.name }); overview.files = points;
  }
  const previousId = state.point?.id || preferredPoint().id, previousStem = state.point?.stem || preferredPoint().stem; const select = clear($("point-select"));
  for (const p of points) select.append(new Option(raw ? p.stem : `${number(p.E, 3)} keV · ${p.stem || p.source_name || "能线"}${isExcluded(p) ? " · 已屏蔽" : ""}`, p.id));
  if (!raw) await renderEcOverview();
  if (token !== state.loadToken || runId !== state.run.id || layer !== state.layer || selection !== state.spectrumToken) return;
  const selected = points.find((p) => p.id === previousId) || points.find((p) => p.stem === previousStem) || points.find((p) => !isExcluded(p)) || points[0];
  renderUnpackWorkspace();
  if (selected) await selectEcPoint(selected); else { state.point = null; state.spectrum = null; chartMessage("spectrum-chart", "本通道没有可用能峰或数据文件，请检查目录或运行上游层。"); if (raw) chartMessage("surface-chart", "本层暂无可用 Parquet 输入。"); }
  setBusy(state.busy);
}

function bindEcClicks(chart) { if (typeof chart.removeAllListeners === "function") chart.removeAllListeners("plotly_click"); chart.on("plotly_click", (event) => { const id = event.points.find((p) => p.customdata)?.customdata; const point = state.ecOverview?.points?.find((p) => p.id === id); if (point) selectEcPoint(point).catch(messageError); }); }

function ecPrediction(p) { const coef = state.ecOverview?.params?.EC_low || state.ecOverview?.params?.EC_high || state.ecOverview?.calibration?.EC_low; return Array.isArray(coef) && coef.length === 3 ? coef[0] * p.center * p.center + coef[1] * p.center + coef[2] : null; }
function ecPointStatus(point) {
  if (isExcluded(point)) return "人工屏蔽";
  if (point.fit_status === "model_pending" || point.unsupported_spectral_model) return "模型待定";
  const reasons = point.quality_reasons || [], used = pointUsage(point);
  const label = used === true ? "参与 L3 拟合" : used === false ? "未参与 L3 · 无可用参数" : "尚未标记拟合状态";
  return `${label}${reasons.length || hasQualityWarning(point) ? ` · 疑似问题${reasons.length ? `：${reasons.join(" · ")}` : ""}` : ""}`;
}

async function renderEcOverview() {
  const context = plotContext();
  const data = state.ecOverview || {}, points = data.points || [], l3 = state.layer === "EC_L3";
  if (["EC_L0", "EC_L1"].includes(state.layer)) return renderRawMain();
  if (state.layer === "EC_L2") { renderEcReviewFiles(); return; }
  $("residual-chart").classList.remove("metadata-chart");
  const traces = [];
  if (l3 && data.curve?.x?.length) traces.push({ type: "scatter", mode: "lines", x: data.curve.x, y: data.curve.y, line: { color: chartColors.fit, width: 1.8 }, name: "E = f(ADC)", hoverinfo: "skip" });
  const x = (p) => l3 ? p.center : p.E, y = (p) => l3 ? p.E : p.center;
  traces.push({ type: "scatter", mode: "markers", x: points.map(x), y: points.map(y), error_y: l3 ? undefined : { type: "data", array: points.map((p) => p.center_err || 0), visible: true, thickness: 1, color: "#6888b1" }, customdata: points.map((p) => p.id), text: points.map((p) => `${p.stem}<br>${number(p.E, 3)} keV · ${ecPointStatus(p)}`), marker: { color: points.map((p) => isExcluded(p) ? chartColors.excluded : pointUsage(p) === false ? chartColors.muted : chartColors.blue), symbol: points.map((p) => isExcluded(p) ? "x" : "circle"), size: 8, line: { color: "#fff", width: .8 } }, name: "真实能峰", hovertemplate: "%{text}<br>横坐标 %{x:.3f}<br>纵坐标 %{y:.3f}<extra></extra>" });
  if (state.point && points.some((p) => p.id === state.point.id)) traces.push({ type: "scatter", mode: "markers", x: [x(state.point)], y: [y(state.point)], customdata: [state.point.id], marker: { size: 13, color: "rgba(0,0,0,0)", line: { color: chartColors.fit, width: 2 } }, hoverinfo: "skip", name: "当前选中" });
  await Plotly.react(plotTarget($("surface-chart")), traces, basicPlotLayout(l3 ? "校正峰位 / ADC" : "已知能量 / keV", l3 ? "能量 / keV" : "校正峰位 / ADC", { uirevision: `${state.run.id}-${state.layer}-${state.channel}` }), plotConfig); if (context !== plotContext()) return; bindEcClicks($("surface-chart"));
  if (context !== plotContext()) return;
  const showResidual = l3 && $("ec-detail-mode").value === "residual";
  const secondary = [{ type: "scatter", mode: "markers", x: points.map((p) => p.E), y: points.map((p) => showResidual ? p.residual_percent ?? (ecPrediction(p) === null ? null : 100 * (ecPrediction(p) - p.E) / p.E) : p.resolution), customdata: points.map((p) => p.id), text: points.map((p) => `${p.stem} · ${number(p.E, 3)} keV<br>${ecPointStatus(p)}`), marker: { color: points.map((p) => isExcluded(p) ? chartColors.excluded : pointUsage(p) === false ? chartColors.muted : chartColors.blue), symbol: points.map((p) => isExcluded(p) ? "x" : "circle"), size: 7 }, hovertemplate: "%{text}<br>%{y:.4f}%<extra></extra>" }];
  if (!showResidual && data.resolution_curve?.x?.length) secondary.unshift({ type: "scatter", mode: "lines", x: data.resolution_curve.x, y: data.resolution_curve.y, line: { color: chartColors.fit, width: 1.6 }, hoverinfo: "skip" });
  await Plotly.react(plotTarget($("residual-chart")), secondary, basicPlotLayout("能量 / keV", showResidual ? "能量残差 / %" : "分辨率 / %", { margin: { l: 65, r: 25, t: 10, b: 42 }, yaxis: { ...axisStyle, title: { text: showResidual ? "能量残差 / %" : "分辨率 / %" }, zeroline: true, zerolinecolor: "#9daab6" } }), plotConfig); if (context !== plotContext()) return; bindEcClicks($("residual-chart"));
  if (context !== plotContext()) return;
  $("surface-caption").textContent = l3 ? "校正峰位与已知能量 · 点击点复核输入谱峰" : "所选通道的 X 射线与放射源能线 · 点击点修改峰窗口";
  $("point-count").textContent = `${points.filter(isUsed).length} / ${points.length} 参与 L3`;
  $("residual-metric").textContent = "";
  renderEcParams();
}

function renderEcParams() {
  const data = state.ecOverview || {}, params = data.params || data.calibration || {}, baseline = data.baseline?.params || data.baseline?.calibration || {};
  const table = node("table"), head = node("tr"); ["参数", "初始基线", "当前试算", "变化 Δ"].forEach((v) => head.append(node("th", "", v))); table.append(head);
  const display = (value) => Array.isArray(value) ? value.map((n) => number(n, 7)).join(" · ") : number(value, 7);
  const difference = (before, after) => before !== null && before !== undefined && after !== null && after !== undefined && Number.isFinite(Number(before)) && Number.isFinite(Number(after)) ? Number(after) - Number(before) : null;
  for (const key of new Set([...Object.keys(baseline), ...Object.keys(params)])) {
    if (key === "channel") continue;
    const before = baseline[key], after = params[key];
    if (typeof after === "object" && !Array.isArray(after) && after !== null) continue;
    const delta = Array.isArray(before) && Array.isArray(after) ? after.map((n, i) => difference(before[i], n)) : difference(before, after);
    const row = node("tr"); row.append(node("td", "", key), node("td", "", display(before)), node("td", "", display(after)), node("td", typeof delta === "number" && delta > 0 ? "positive" : typeof delta === "number" && delta < 0 ? "negative" : "", display(delta))); table.append(row);
  }
  if (table.rows.length === 1) { const row = node("tr"); row.append(node("td", "", "尚无当前通道刻度参数")); table.append(row); }
  clear($("parameter-table")).append(table);
  const diagnostics = clear($("fit-diagnostics"));
  diagnostics.append(node("span", "", `初始：${data.baseline?.version || "无基线"} · 当前：${data.version || "尚未生成"}`), node("span", "", "数组顺序为 [a, b, c]；Δ = 当前 − 基线。能量模型沿用 E=f(ADC)。"));
}

async function selectEcPoint(point) {
  const token = ++state.spectrumToken, layer = state.layer, raw = ["EC_L0", "EC_L1"].includes(layer);
  state.selecting = true;
  for (const id of ["fit-spectrum","auto-fit-spectrum","window-lo","window-hi"]) $(id).disabled=true;
  try { if (state.dirty) await saveRevision({}, true); }
  catch(error) { if (token===state.spectrumToken) { state.selecting=false; setBusy(state.busy); } throw error; }
  if (token !== state.spectrumToken || layer !== state.layer) return;
  if (state.point?.id !== point.id) { $("peak-noise-ceiling").value=""; $("peak-expected-center").value=""; }
  $("auto-fit-status").textContent=""; fitFeedback();
  state.point = point; state.spectrum = null; state.dirty = false; $("point-select").value = point.id; $("selected-channel").textContent = `CH ${state.channel}`;
  renderEcReviewFiles();
  if (isPeakReviewLayer()) { chartMessage("surface-chart", `正在读取 ${point.stem} · CH ${state.channel}…`); chartMessage("residual-chart", "读取拟合残差…"); }
  if (raw) { chartMessage("surface-chart", "正在读取所选文件的真实脉冲谱…"); $("surface-caption").textContent = `${point.stem} · CH ${state.channel} · 读取中`; }
  rememberView(); renderCurrentStatus(); renderUnpackWorkspace();
  if (layer === "EC_L0" && point.fit_status === "not_parsed") {
    chartMessage("surface-chart", `${point.stem} 尚未生成 L0。点击上方“解包选中文件”调用实际解包器。`);
    chartMessage("residual-chart", `事件：${point.event || "—"}\n遥测：${point.hk || "—"}`);
    renderMetadata({source:"raw",source_path:point.event,stem:point.stem,metadata:{状态:"待解包"}});
    state.selecting=false; setBusy(state.busy); return;
  }
  $("spectrum-evidence-status").hidden = true; $("spectrum-provenance").hidden = true;
  const conditions = clear($("point-condition")); for (const [key, value] of raw ? [["通道", `CH ${state.channel}`]] : unsupportedSpectralModel() ? [["能量", `${number(point.E, 3)} keV`]] : [["能量", `${number(point.E, 3)} keV`], ["峰位", `${number(point.center, 3)} ADC`]]) { const item = node("span", "", key); item.append(node("strong", "", value)); conditions.append(item); }
  chartMessage(isPeakReviewLayer() ? "surface-chart" : "spectrum-chart", "正在读取真实校正谱…");
  const query = raw ? `layer=${layer}&stem=${encodeURIComponent(point.stem)}&channel=${state.channel}` : `layer=EC_L2&point_id=${encodeURIComponent(point.id)}&channel=${state.channel}`;
  let spectrum;
  try { spectrum = await api(runPath(`/ec-spectrum?${query}`)); }
  catch(error) { if (token===state.spectrumToken && layer===state.layer) { state.selecting=false; chartMessage(isPeakReviewLayer() ? "surface-chart" : "spectrum-chart", error.message); setBusy(state.busy); } throw error; }
  if (token !== state.spectrumToken || state.layer !== layer) return;
  state.spectrum = spectrum;
  const fit = spectrum.fit || point, override = fit.peak_detection?.manual_window === false && !(state.run.pending_ec_windows || []).includes(point.id) ? null : state.run.config?.ec_windows?.[point.id];
  displayWindowValue("window-lo", override?.lo ?? spectrum.lo ?? fit.lo ?? point.lo ?? ""); displayWindowValue("window-hi", override?.hi ?? spectrum.hi ?? fit.hi ?? point.hi ?? "");
  $("window-status").textContent = override ? "已保存修订窗" : "当前拟合窗";
  const holder = clear($("spectrum-params"));
  if (!raw) for (const [label, value] of [["峰位 / ADC", `${number(fit.center, 3)} ± ${number(fit.center_err, 3)}`], ["σ / ADC", number(fit.sigma, 3)], ["能量 / keV", number(point.E, 3)], ["R²", number(fit.rsquared, 5)], ["拟合模型", fit.fit_mode || "—"], ["能峰来源", point.origin || spectrum.fit_source || spectrum.source || "历史参考"]]) { const item = node("div", "param-item"); item.append(node("span", "", label), node("strong", "", value)); holder.append(item); }
  if (!raw && spectrum.curve?.note) { const item = node("div", "param-item wide"); item.append(node("span", "", "历史曲线说明"), node("strong", "", "历史记录缺少背景参数，图中仅显示高斯峰分量；重拟合后保存完整曲线。")); holder.append(item); }
  if (!raw && point.quality_reasons?.length) { const item = node("div", "param-item wide"); item.append(node("span", "", "拟合问题提示"), node("strong", "", point.quality_reasons.join(" · "))); holder.append(item); }
  renderSpectrumProvenance(spectrum);
  await renderSpectrum(); if (token !== state.spectrumToken || layer !== state.layer) return; if (raw) await renderRawMain(); else await renderEcOverview(); state.selecting=false; setBusy(state.busy);
}

function renderSpectrumProvenance(spectrum) {
  const status = clear($("spectrum-evidence-status")), details = $("spectrum-provenance"), files = clear($("spectrum-provenance-files"));
  const visible = ["EC_L1", "EC_L2", "EC_L3"].includes(state.layer); status.hidden = true; details.hidden = !visible; if (!visible) return;
  const evidence = spectrum.dependency_evidence || {}, selected = evidence.selected_tb || state.run.selected_tb;
  const mismatch = evidence.matches_selected === false;
  const warning = (text) => { status.hidden = false; status.append(node("p", "evidence-warning", text)); };
  if (evidence.matches_selected === true) status.append(badge("succeeded", "谱文件 TB 依赖匹配所选版本"));
  else if (mismatch) warning("TB 参数已更换，运行当前文件的 EC L1 应用校正。");
  else status.append(node("p", "evidence-unknown", selected ? "TB 参数未记录" : "未选择 TB 参数"));
  const fitKnown = Object.prototype.hasOwnProperty.call(spectrum, "fit_input_verified");
  if (fitKnown && spectrum.fit_input_verified === false) warning("谱已更新，请重新拟合当前能峰。");
  else if (fitKnown && spectrum.fit_input_verified === true) status.append(badge("succeeded", "峰参数与谱输入校验一致"));
  else if (fitKnown) status.append(node("p", "evidence-unknown", "输入谱版本未记录"));
  files.append(node("p", "evidence-selection", `所选 TB：${selected ? versionText(selected) : "未选择"}`));
  for (const file of evidence.files || []) {
    const row = node("div", "evidence-file"), heading = node("div", "evidence-file-heading");
    heading.append(node("strong", "", file.role === "background" ? "背景文件" : "信号文件"), badge(file.matches_selected === true ? "succeeded" : file.matches_selected === false ? "stale" : "idle", file.matches_selected === true ? "匹配所选 TB" : file.matches_selected === false ? "不同 TB" : "依赖未核实"));
    row.append(heading, node("span", "", file.filename || "未知文件"), node("span", "", `TB：${file.tb_corrfile || "文件未记录"}`)); files.append(row);
  }
  if (!evidence.files?.length) files.append(node("p", "evidence-unknown", `谱来源：${spectrum.source_path || spectrum.path || "未提供"}`));
  if (spectrum.fit_input_note) files.append(node("p", "evidence-unknown", spectrum.fit_input_note));
  details.open = mismatch || spectrum.fit_input_verified === false;
}

function installWindowHandles() {
  const chart = spectrumChart(); chart.querySelectorAll(".window-handle").forEach((el) => el.remove());
  if (["TB_L0", "EC_L0", "EC_L1"].includes(state.layer) || !chart._fullLayout?.xaxis) return;
  chart.style.position = "relative";
  for (const [id, text] of [["window-lo", "下限"], ["window-hi", "上限"]]) {
    const handle = node("button", "window-handle", ""); handle.type = "button"; handle.setAttribute("aria-label", `拖动拟合窗口${text}`); handle.title = `拖动${text} · 键盘方向键微调`; handle.dataset.bound = id;
    const cap = node("span", "window-handle-cap", text); handle.append(cap); chart.append(handle);
    const position = () => { const ax = chart._fullLayout.xaxis; handle.style.left = `${ax._offset + ax.l2p(windowValue(id)) - 7}px`; handle.style.top = `${chart._fullLayout._size.t - 18}px`; handle.style.height = `${chart._fullLayout._size.h + 18}px`; };
    position();
    handle.addEventListener("pointerdown", (event) => { if (state.busy) return; event.preventDefault(); event.stopPropagation(); handle.setPointerCapture(event.pointerId); handle.classList.add("dragging"); });
    handle.addEventListener("pointermove", (event) => { if (!handle.hasPointerCapture(event.pointerId)) return; const ax = chart._fullLayout.xaxis, rect = chart.getBoundingClientRect(); const value = boundedWindowValue(id, ax.p2l(event.clientX - rect.left - ax._offset)); setEditedWindow(id, value); position(); });
    const finish = (event) => { if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId); handle.classList.remove("dragging"); };
    handle.addEventListener("pointerup", finish); handle.addEventListener("pointercancel", finish);
    handle.addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); setEditedWindow(id, boundedWindowValue(id, windowValue(id) + (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 10 : .1))); position(); } });
  }
  if (typeof chart.removeAllListeners === "function") chart.removeAllListeners("plotly_relayout");
  chart.on("plotly_relayout", () => { for (const handle of chart.querySelectorAll(".window-handle")) { const ax = chart._fullLayout.xaxis; handle.style.left = `${ax._offset + ax.l2p(windowValue(handle.dataset.bound)) - 7}px`; } });
}

function boundedWindowValue(id, value) { const { min, max, minimum_width: width } = spectrumWindowBounds(), other = windowValue(id === "window-lo" ? "window-hi" : "window-lo"); return id === "window-lo" ? Math.floor(Math.max(min, Math.min(value, other - width, max - width)) * 1000) / 1000 : Math.ceil(Math.min(max, Math.max(value, other + width, min + width)) * 1000) / 1000; }

function setEditedWindow(id, value) { const input = $(id); input.value = Number(value).toFixed(3); input.dataset.edited = "true"; input.title = ""; state.dirty = true; $("window-status").textContent = "窗口已修改 · 未拟合"; fitFeedback(unsupportedSpectralModel() ? "窗口已修改" : "窗口已修改 · 点击“按当前窗口拟合”"); renderRunBadge(); if (spectrumChart().data) Plotly.relayout(spectrumChart(), { shapes: [...windowShapes(windowValue("window-lo"), windowValue("window-hi")), ...peakPlotDetails(state.spectrum || {}).shapes] }); }

async function reviewNextLayer() {
  if (!isPeakReviewLayer()) return;
  const next = state.layer === "TB_L1" ? "TB_L2" : "EC_L3";
  if (state.dirty) await saveRevision({},true);
  await selectLayer(next);
}

function renderPayloadOptions() {
  const current = state.run?.payload || $("payload").value, runs = state.bootstrap?.runs || [], payloads = new Set();
  for (const dataset of state.bootstrap?.datasets || state.datasets || []) {
    const assigned = runs.filter(run => run.dataset_id === dataset.id).map(run => run.payload);
    const matched = dataset.acquisition_preset_match?.reference?.payload;
    for (const payload of assigned.length ? assigned : [matched || dataset.payload]) if (payload) payloads.add(payload);
  }
  for (const run of runs) payloads.add(run.payload);
  if (state.run) payloads.add(state.run.payload);
  if (!payloads.size) for (const item of state.bootstrap?.payloads || []) payloads.add(typeof item === "string" ? item : item.id || item.payload);
  const select = clear($("payload")); for (const payload of [...payloads].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}))) select.append(new Option(payload,payload)); select.value = current;
}

function renderDatasetOptions(payload) {
  const select = clear($("dataset-select"));
  for (const dataset of state.datasets) select.append(new Option(dataset.acquisition_preset_match?.reference?.payload || dataset.payload || dataset.id, dataset.id));
  const selected = payload ? state.datasets.find((d) => datasetMatchesPayload(d,payload)) : state.datasets[0]; if (selected) select.value = selected.id; else select.append(new Option("没有可用数据集，请扫描目录", ""));
  updateSelectedDataset();
}
function updateSelectedDataset() { const dataset = state.datasets.find((d) => d.id === $("dataset-select").value); const holder = clear($("dataset-paths")); if (!dataset) return; $("new-run-payload").value = dataset.payload || dataset.source_payload || ""; holder.append(node("span", "", `数据：${dataset.raw_dir || "未选择"}`), node("span", "", `产品：${dataset.product_dir || "未选择"}`)); registryDatasetSelection().catch(datasetError); }
function datasetError(error) { $("dataset-scan-state").textContent = error.message; $("dataset-scan-state").className = "scan-error"; }
async function scanDatasets(preferredPayload) { const button = $("scan-datasets"), submit = $("new-run-form").querySelector('[type="submit"]'), token = (state.datasetScanToken || 0) + 1; state.datasetScanToken = token; state.datasets = []; clear($("dataset-select")).append(new Option("正在扫描目录…", "")); clear($("dataset-paths")); button.disabled = true; submit.disabled = true; $("dataset-scan-state").textContent = "正在扫描目录…"; try { const result = await api("/api/datasets/scan", { method: "POST", body: { raw_dir: $("dataset-raw-dir").value.trim() || undefined, product_dir: $("dataset-product-dir").value.trim() || undefined } }); if (token !== state.datasetScanToken) return; state.datasets = result.datasets || []; state.datasetPathsDirty = false; renderDatasetOptions(typeof preferredPayload === "string" ? preferredPayload : undefined); $("dataset-scan-state").textContent = `找到 ${state.datasets.length} 个数据集`; $("dataset-scan-state").className = ""; if (!state.datasets.length) throw new Error("未发现可识别的数据或产品。请检查目录层级。"); } catch (error) { if (token !== state.datasetScanToken) return; throw error; } finally { if (token === state.datasetScanToken) { button.disabled = false; submit.disabled = !state.datasets.length || state.datasetPathsDirty; } } }

let directoryTarget = "", directoryState = null, directoryLoadToken = 0;
async function browseDirectory(target) { directoryTarget = target; $("directory-dialog").showModal(); await loadDirectory($(target).value.trim()); }
async function loadDirectory(path) {
  const token = ++directoryLoadToken, holder = clear($("directory-list")); directoryState = null;
  for (const id of ["directory-location", "directory-go", "directory-up", "choose-directory"]) $(id).disabled = true;
  holder.append(node("p", "muted-text", "正在读取目录…"));
  try {
    const result = await api(`/api/directories${path ? `?path=${encodeURIComponent(path)}` : ""}`); if (token !== directoryLoadToken) return;
    directoryState = result; $("directory-location").value = result.path; clear(holder);
    for (const entry of result.directories || []) { const button = node("button", "directory-entry", `▸  ${entry.name}`); button.title = entry.path; button.addEventListener("click", handleError(() => loadDirectory(entry.path))); holder.append(button); }
    if (!holder.children.length) holder.append(node("p", "muted-text", "此目录没有子目录，可直接选择。"));
  } catch (error) { if (token === directoryLoadToken) clear(holder).append(node("p", "scan-error", error.message)); }
  finally { if (token === directoryLoadToken) { $("directory-location").disabled = false; $("directory-go").disabled = false; $("directory-up").disabled = !directoryState?.parent; $("choose-directory").disabled = !directoryState; } }
}

async function loadTbVersions() {
  const runId = state.run.id; const result = await api(runPath("/tb-versions")); if (state.run.id !== runId) return;
  state.tbVersions = result.versions || []; const select = clear($("tb-version-select"));
  for (const version of state.tbVersions) { const origin = { historical: "历史产品", computed: "已计算版本", external: "外部导入", selected: "已选版本" }[version.origin] || version.origin || "TB L2"; const modified = version.modified_at ? timestamp(typeof version.modified_at === "number" ? version.modified_at * 1000 : version.modified_at) : "时间未知"; const option = new Option(`${version.filename || version.label}${version.selected ? " · 当前" : ""}`, version.id); option.title = `${modified}\n${origin}\n${version.source_path || version.path || version.filename}\n${version.sha256 || ""}`; select.append(option); }
  const selected = state.tbVersions.find((v) => v.selected); if (selected) select.value = selected.id;
  if (!state.tbVersions.length) select.append(new Option("未找到有效 TB L2 参数包，请先载入产品目录", ""));
  $("tb-binding-state").textContent = selected ? "已应用" : "选择 EC 校正使用的 TB 参数";
  $("apply-tb-version").disabled = state.busy || !select.value;
}
async function applyTbVersion() { if (state.dirty) await saveRevision({}, true); const id = $("tb-version-select").value; if (!id) throw new Error("请先选择 TB L2 参数包。"); state.run = await api(runPath("/dependency"), { method: "PATCH", body: { tb_version_id: id } }); state.run = state.run.run || state.run; renderRun(); await loadSurfaceSafely(); notify("已应用 TB 参数，运行 EC L1 更新校正谱。", "success"); }

function pipelinePlan(mode, tbStem, ecStem) {
  if (mode === "all") return layers.map(layer => ({layer:layer.id,representative:false}));
  if (mode === "tb") {
    if (!tbStem) throw new Error("请选择 TB 文件。");
    return ["TB_L0","TB_L1"].map(layer => ({layer,stem:tbStem,representative:false}));
  }
  if (!ecStem) throw new Error("请选择 EC 文件。");
  const rows = state.run.inventory?.raw?.reading_preview?.rows || [], signal = rows.find(row => row.stem === ecStem);
  const backgrounds = signal?.kind === "source" ? rows.filter(row => row.kind === "background").map(row=>row.stem) : [];
  return [...new Set([...backgrounds,ecStem])].flatMap(stem=>["EC_L0","EC_L1"].map(layer=>({layer,stem,representative:false}))).concat({layer:"EC_L2",stem:ecStem,representative:false});
}
function ensurePipelineDialog() {
  if ($("pipeline-dialog")) return;
  const dialog = node("dialog","workflow-dialog"); dialog.id="pipeline-dialog";
  const heading=node("div","dialog-title"); heading.append(node("h2","","运行流程")); const close=node("button","icon-button","×"); close.setAttribute("aria-label","关闭运行流程"); close.addEventListener("click",()=>dialog.close()); heading.append(close); dialog.append(heading);
  const modeLabel=node("label","field-label","处理范围"), mode=node("select","full-width"); mode.id="pipeline-mode"; mode.setAttribute("aria-label","流程范围");
  for (const [value,label] of [["tb","TB 当前文件 · L0 → L1"],["ec","EC 当前文件 · L0 → L2"],["all","全部数据 · TB / EC 七层"]]) mode.append(new Option(label,value));
  modeLabel.append(mode); dialog.append(modeLabel);
  for (const [family,label] of [["tb","TB 文件"],["ec","EC 文件"]]) { const field=node("label","field-label",label); field.id=`pipeline-${family}-field`; const input=node("select","full-width"); input.id=`pipeline-${family}-file`; input.setAttribute("aria-label",label); field.append(input); dialog.append(field); input.addEventListener("change",renderPipelinePlan); }
  const preview=node("ol","workflow-plan"); preview.id="pipeline-plan"; dialog.append(preview);
  const error=node("p","scan-error"); error.id="pipeline-error"; error.setAttribute("role","alert"); error.hidden=true; dialog.append(error);
  const footer=node("div","dialog-actions"), cancel=node("button","button","取消"), start=node("button","button primary","开始处理"); start.id="start-pipeline";
  cancel.addEventListener("click",()=>dialog.close()); start.addEventListener("click",handleError(async()=>{ const plan=pipelinePlan(mode.value,$("pipeline-tb-file").value,$("pipeline-ec-file").value); dialog.close(); await runPipeline(plan,mode.value); }));
  footer.append(cancel,start); dialog.append(footer); mode.addEventListener("change",renderPipelinePlan); document.body.append(dialog);
}
function renderPipelinePlan() {
  const mode=$("pipeline-mode").value; $("pipeline-tb-field").hidden=mode!=="tb"; $("pipeline-ec-field").hidden=mode!=="ec";
  const preview=clear($("pipeline-plan")), error=$("pipeline-error"); error.hidden=true;
  try {
    const plan=pipelinePlan(mode,$("pipeline-tb-file").value,$("pipeline-ec-file").value);
    const unavailable = plan.find(step=>state.run.stages?.[step.layer]?.unavailable_reason); if (unavailable) throw new Error(state.run.stages[unavailable.layer].unavailable_reason);
    for (const step of plan) preview.append(node("li","",`${step.layer.replace("_"," ")} · ${step.stem || "全部输入"}`));
    if (mode==="all") preview.append(node("li","workflow-note","EC 将采用本次生成的 TB 曲面参数。"));
    else if (mode==="ec") preview.append(node("li","workflow-note",`TB 参数：${state.run.selected_tb?.filename || "请先在 EC L1 选择 TB 参数"}`));
    $("start-pipeline").disabled=mode==="ec" && !state.run.selected_tb;
  } catch (e) { error.textContent=e.message; error.hidden=false; $("start-pipeline").disabled=true; }
}
function openPipelineDialog() {
  document.querySelector(".pipeline-menu").open=false;
  ensurePipelineDialog(); const family=state.layer.slice(0,2), raw=state.run.inventory?.raw || {}, rows=raw.reading_preview?.rows || [];
  $("pipeline-mode").value=family==="TB" ? "tb" : "ec";
  for (const type of ["tb","ec"]) {
    const select=clear($("pipeline-"+type+"-file"));
    const files=rows.length ? rows.filter(row=>type==="tb" ? row.kind==="tb" : ["xray","source"].includes(row.kind) && (row.channel==null || row.channel===state.channel)) : type==="tb" ? raw.tb_samples || [] : raw.ec_samples || [];
    for (const file of files) select.append(new Option(file.stem,file.stem));
    const preferred=family.toLowerCase()===type ? state.point?.stem : savedView().familyStems?.[type.toUpperCase()];
    if (files.some(file=>file.stem===preferred)) select.value=preferred;
    else if (files.length) select.value=files[0].stem;
  }
  renderPipelinePlan(); $("pipeline-dialog").showModal();
}
async function runPipeline(plan, mode) {
  const channel=state.channel, selectedPointId=state.point?.id;
  setBusy(true); notify("正在运行流程…","info");
  try {
    if (state.dirty) await saveRevision({},true);
    for (const step of plan) {
      if (mode==="all" && step.layer==="EC_L1") { if (!state.run.tb_version?.path) throw new Error("TB 曲面参数尚未生成。"); const binding=await api(runPath("/dependency"),{method:"PATCH",body:{path:state.run.tb_version.path}}); state.run=binding.run || binding; }
      if (mode!=="all") { state.channel=channel; $("channel").value=channel; }
      await selectLayer(step.layer);
      if (mode!=="all" && isPeakReviewLayer() && unsupportedSpectralModel()) throw new Error(unsupportedSpectralModel());
      if (step.stem && step.layer!=="EC_L2") {
        const items = step.layer.startsWith("TB") ? state.surface?.points : state.ecOverview?.files;
        const selected = items?.find(point=>point.stem===step.stem);
        if (selected) await selectPoint(selected);
      }
      if (step.layer==="EC_L2" && step.stem) {
        const points=(state.ecOverview?.points || []).filter(point=>point.stem===step.stem);
        if (!points.length) throw new Error(`未找到 ${step.stem} 的能峰输入，请检查 EC L1 输出。`);
        for (const point of points) { await selectEcPoint(point); await executeJob(step.layer,{point_id:point.id,channel,representative:false,...ecBinningSpec()},false); }
        const current=(state.ecOverview?.points || []).filter(point=>point.stem===step.stem);
        if (current.length) await selectEcPoint(current.find(point=>point.id===selectedPointId) || current[0]);
      } else { const {layer,...spec}=step; const job = await executeJob(layer,{...spec,...(mode!=="all"?{channel,...(layer==="TB_L1"?{fit_channels:[channel]}:{})}:{})},false); if (job?.status === "partial") { notify(`${layer.replace("_"," ")} 有通道计算失败，可用结果已保存；请处理缺失结果后继续。`,"warning"); return; } }
    }
    notify("流程已完成。","success");
  } finally { setBusy(false); }
}

function handleError(action) { return async (...args) => { try { await action(...args); } catch (error) { messageError(error); } }; }
const acquisitionDraft = { profile: null, presets: [], revision: null, sha256: null, previewFingerprint: null, previewValid: false, busy: false, changed: false, runId: null };
const acquisitionFieldLabels = { channel: "通道字段", max: "幅度最大值字段", base: "基线字段", utc: "时间戳字段（按逗号列出）", crc: "CRC 字段", ccm: "CCM 字段", temp: "温度字段（{channel} 为通道）", bias: "偏压字段（{channel} 为通道）", current: "HK 电流字段（电流压降换算使用）" };
const acquisitionTransformLabels = { base_divisor: "基线除数", ccm_divisor: "电流归一化除数", temp_scale: "温度比例", temp_offset: "温度偏移 / °C", bias_scale: "偏压比例", bias_offset: "偏压偏移 / V", tail_samples: "末尾稳定工况样本数", stable_bias_min: "稳定偏压下限 / V", stable_bias_tolerance: "稳态偏压容差 / V" };
const acquisitionMultipacketLabels = { event_multi_evt: "事件多包数量", event_multi_step: "事件多包步长", hk_multi_evt: "遥测多包数量", hk_multi_step: "遥测多包步长" };
const acquisitionManifestColumns = ["event", "hk", "kind", "channel", "energy_keV", "peak_energies_keV", "source", "exposure_minutes", "temp", "bias"];
const acquisitionRuleColumns = ["kind", "directory", "event_regex", "hk_regex", "metadata_regex", "prefer_suffix", "metadata"];
function acquisitionCopy(value) { return JSON.parse(JSON.stringify(value)); }
function acquisitionStatus(text, error = false) { $("acquisition-status").textContent = text; $("acquisition-status").className = error ? "profile-error" : ""; }
function acquisitionError(error) { acquisitionStatus(error.message || String(error), true); }
function acquisitionMarkChanged() { if (!acquisitionDraft.profile) return; acquisitionDraft.changed = true; acquisitionDraft.previewValid = false; $("save-acquisition").disabled = true; acquisitionStatus("配置已修改，预览后保存。"); registryRefreshButtons(); }
function acquisitionSetBusy(busy) { acquisitionDraft.busy = busy; for (const id of ["preview-acquisition", "load-acquisition-preset", "load-event-xml", "load-hk-xml", "use-event-xml-preset", "use-hk-xml-preset", "choose-event-xml-file", "choose-hk-xml-file"]) $(id).disabled = busy || (id === "preview-acquisition" && !acquisitionDraft.profile); $("save-acquisition").disabled = busy || !acquisitionDraft.previewValid; $("acquisition-dialog").setAttribute("aria-busy", String(busy)); registryRefreshButtons(); }
function acquisitionShowTab(tab) { if (tab === "metadata") acquisitionEnsureMetadata(); for (const name of ["xml", "metadata", "preview"]) { const active = tab === name; $("acquisition-tab-" + name).classList.toggle("active", active); $("acquisition-tab-" + name).setAttribute("aria-selected", String(active)); $("acquisition-panel-" + name).hidden = !active; } $("acquisition-dialog").querySelector(".acquisition-scroll").scrollTop = 0; }
function acquisitionNumber(id, nullable = false) { const raw = $(id).value.trim(); if (raw === "" && nullable) return null; const n = Number(raw); if (raw === "" || !Number.isFinite(n)) throw new Error(`请填写有效数值：${$(id).closest("label")?.firstChild?.textContent || id}`); return n; }
function acquisitionFormField(holder, key, label, value, numeric = false) { const field = node("label", "", label), input = node("input"); input.id = "profile-" + key; input.type = numeric ? "number" : "text"; if (numeric) input.step = "any"; input.value = Array.isArray(value) ? value.join(", ") : value ?? ""; field.append(input); holder.append(field); }
function acquisitionRemoveButton(row, label) { const cell = node("td"), button = node("button", "profile-remove-row", "×"); button.type = "button"; button.setAttribute("aria-label", label); button.addEventListener("click", () => { row.remove(); acquisitionMarkChanged(); }); cell.append(button); row.append(cell); }
function acquisitionKindInput(value) { const input = node("select"); for (const [key, label] of [["tb", "TB 工况"], ["xray", "X 射线"], ["source", "放射源"], ["background", "本底"]]) input.append(new Option(label, key)); input.value = value || "tb"; return input; }
function acquisitionAppendManifest(item = {}) {
  const row = node("tr"); row._profileOriginal = acquisitionCopy(item);
  for (const key of acquisitionManifestColumns) { const cell = node("td"), input = key === "kind" ? acquisitionKindInput(item[key]) : node("input"); input.dataset.key = key; input.setAttribute("aria-label", key); if (key !== "kind") { input.value = Array.isArray(item[key]) ? item[key].join(", ") : item[key] ?? ""; if (["channel", "energy_keV", "exposure_minutes", "temp", "bias"].includes(key)) { input.type = "number"; input.step = key === "channel" ? "1" : "any"; } } cell.append(input); row.append(cell); }
  acquisitionRemoveButton(row, "删除文件配对"); $("profile-manifest-rows").append(row);
}
function acquisitionAppendRule(item = {}) {
  const row = node("tr"); row._profileOriginal = acquisitionCopy(item);
  for (const key of acquisitionRuleColumns) { const cell = node("td"), input = key === "kind" ? acquisitionKindInput(item[key]) : key.endsWith("regex") || key === "metadata" ? node("textarea") : node("input"); input.dataset.key = key; input.setAttribute("aria-label", key); input.value = key === "metadata" ? item.metadata ? JSON.stringify(item.metadata) : "" : item[key] ?? ""; input.spellcheck = false; cell.append(input); row.append(cell); }
  acquisitionRemoveButton(row, "删除匹配规则"); $("profile-rule-rows").append(row);
}
function acquisitionAppendSource(item = {}) {
  const row = node("tr"); row._profileOriginal = acquisitionCopy(item);
  for (const key of ["name", "aliases", "energies_keV"]) { const cell = node("td"), input = node("input"); input.dataset.key = key; input.setAttribute("aria-label", key); input.value = Array.isArray(item[key]) ? item[key].join(", ") : item[key] ?? ""; cell.append(input); row.append(cell); }
  acquisitionRemoveButton(row, "删除发射源"); $("profile-source-rows").append(row);
}
function acquisitionUpdateModes() {
  const mode = $("profile-condition-mode").value, manual = $("profile-discovery-mode").value === "manifest";
  $("profile-manifest-section").hidden = !manual; $("profile-rules-section").hidden = manual;
  for (const id of ["profile-condition-temp", "profile-condition-bias"]) $(id).disabled = mode !== "fixed";
  for (const id of ["profile-condition-temp-scale", "profile-condition-bias-scale"]) $(id).disabled = mode !== "filename";
  const notes = { hk: "当前使用 HK 实测：原始温度、偏压经字段映射与比例换算后用于计算。文件名工况仅作为标称值。", filename: "显式采用文件名标称工况：在规则的工况表达式中提取 temp / bias，并应用提取比例。请核对预览后运行。", manifest: "显式采用映射表工况：在每行填写温度 / 偏压。请同时选择“逐文件映射表”并确保所需值完整。", fixed: "显式采用固定工况：本次所有输入使用上方温度 / 偏压，替代 HK 测量值。" };
  $("profile-condition-help").textContent = notes[mode] || "";
  const tempMode = $("profile-temp-mode").value, biasMode = $("profile-bias-mode").value;
  for (const key of ["temp_scale", "temp_offset"]) $("profile-transform-" + key).disabled = tempMode !== "affine";
  for (const key of ["bias_scale", "bias_offset"]) $("profile-transform-" + key).disabled = biasMode !== "affine";
  $("profile-field-current").disabled = biasMode === "affine";
  $("profile-transform-equations").textContent = `${tempMode === "grid10_signed" ? "温度：t = raw / 256；t > 128 时减去 255。" : "温度：raw × 比例 + 偏移。"} ${biasMode === "grid10_current_drop" ? "偏压：20.57 × 2.5 × Vraw / 4096 − 499 × 2.5 × Iraw / (4096 × 548.88)。" : biasMode === "grid11_current_drop" ? "偏压：Vraw / 1000 − 499 × Iraw × 10⁻⁶。" : "偏压：raw × 比例 + 偏移。"} 多包参数留空时使用解析器默认值。`;
}
function acquisitionRenderProfile(profile) {
  acquisitionDraft.profile = acquisitionCopy(profile); const parser = profile.parser || {}, conditions = parser.conditions || {}, xml = profile.xml || {}, discovery = profile.discovery || {};
  for (const role of ["event", "hk"]) { $(role + "-xml-path").value = xml[role]?.source_path || ""; $(role + "-xml-text").value = xml[role]?.text || ""; $(role + "-xml-status").textContent = xml[role]?.imported_name ? `浏览器导入 · ${xml[role].imported_name}` : xml[role]?.text ? "已载入 XML 快照" : "未载入"; const select = clear($(role + "-xml-preset")); for (const preset of acquisitionDraft.presets) select.append(new Option(preset.label || preset.id, preset.id)); if (!select.options.length) select.append(new Option("无内置 XML 预设", "")); }
  $("profile-event-tag").value = parser.event_tag || ""; $("profile-hk-tag").value = parser.hk_tag || ""; $("profile-endian").value = parser.endian || "MSB";
  const fields = clear($("profile-fields")); for (const [key, label] of Object.entries(acquisitionFieldLabels)) acquisitionFormField(fields, "field-" + key, label, parser.fields?.[key]);
  const transforms = clear($("profile-transforms")); for (const [key, label] of Object.entries(acquisitionTransformLabels)) acquisitionFormField(transforms, "transform-" + key, label, parser.transform?.[key] ?? (key === "stable_bias_tolerance" ? .2 : undefined), true);
  $("profile-temp-mode").value = parser.transform?.temp_mode || "affine"; $("profile-bias-mode").value = parser.transform?.bias_mode || "affine"; $("profile-hk-selection").value = parser.transform?.hk_selection || "legacy_tail";
  const histogram = profile.fitting?.tb_histogram || { min: 0, max: 1200, bin_width: 1 }; $("profile-hist-min").value = histogram.min; $("profile-hist-max").value = histogram.max; $("profile-hist-width").value = histogram.bin_width;
  const multi = clear($("profile-multipacket")); for (const [key, label] of Object.entries(acquisitionMultipacketLabels)) acquisitionFormField(multi, key, label, parser[key], true);
  $("profile-condition-mode").value = conditions.mode || "hk"; for (const key of ["temp", "bias", "temp_scale", "bias_scale"]) $("profile-condition-" + key.replaceAll("_", "-")).value = conditions[key] ?? (key.endsWith("_scale") ? 1 : "");
  $("profile-discovery-mode").value = discovery.mode || "rules";
  acquisitionDraft.metadataRendered = false;
  for (const id of ["profile-manifest-rows", "profile-rule-rows", "profile-source-rows"]) clear($(id));
  if (!$("acquisition-panel-metadata").hidden) acquisitionEnsureMetadata();
  acquisitionUpdateModes(); acquisitionDraft.previewFingerprint = null; acquisitionDraft.previewValid = false; $("save-acquisition").disabled = true;
  clear($("acquisition-preview-messages")); clear($("acquisition-preview-results")).append(node("p", "empty-list", "尚未预览当前草稿。")); $("acquisition-preview-summary").textContent = "修改配置后点击“预览与校验”";
}
function acquisitionReadProfile() {
  const profile = acquisitionCopy(acquisitionDraft.profile), parser = profile.parser ||= {}; profile.xml ||= {};
  for (const role of ["event", "hk"]) profile.xml[role] = { ...(profile.xml[role] || {}), source_path: $(role + "-xml-path").value.trim() || null, text: $(role + "-xml-text").value || null };
  parser.event_tag = $("profile-event-tag").value.trim(); parser.hk_tag = $("profile-hk-tag").value.trim(); parser.endian = $("profile-endian").value; parser.fields ||= {};
  for (const key of Object.keys(acquisitionFieldLabels)) { const value = $("profile-field-" + key).value.trim(); parser.fields[key] = key === "utc" ? value.split(",").map((s) => s.trim()).filter(Boolean) : value; }
  parser.transform ||= {}; for (const key of Object.keys(acquisitionTransformLabels)) parser.transform[key] = acquisitionNumber("profile-transform-" + key);
  parser.transform.temp_mode = $("profile-temp-mode").value; parser.transform.bias_mode = $("profile-bias-mode").value; parser.transform.hk_selection = $("profile-hk-selection").value;
  const histogram = { min: acquisitionNumber("profile-hist-min"), max: acquisitionNumber("profile-hist-max"), bin_width: acquisitionNumber("profile-hist-width") };
  if (profile.fitting?.tb_histogram || histogram.min !== 0 || histogram.max !== 1200 || histogram.bin_width !== 1) { profile.fitting ||= {}; profile.fitting.tb_histogram = histogram; }
  for (const key of Object.keys(acquisitionMultipacketLabels)) parser[key] = acquisitionNumber("profile-" + key, true);
  const conditionMode = $("profile-condition-mode").value;
  parser.conditions = { ...(parser.conditions || {}), mode: conditionMode, temp: acquisitionNumber("profile-condition-temp", true), bias: acquisitionNumber("profile-condition-bias", true), temp_scale: acquisitionNumber("profile-condition-temp-scale"), bias_scale: acquisitionNumber("profile-condition-bias-scale") };
  if (conditionMode === "fixed" && (parser.conditions.temp === null || parser.conditions.bias === null)) throw new Error("固定工况需要填写温度和偏压。");
  profile.discovery ||= {}; profile.discovery.mode = $("profile-discovery-mode").value;
  if (acquisitionDraft.metadataRendered) {
  profile.discovery.manifest = [...$("profile-manifest-rows").rows].map((row) => { const item = { ...row._profileOriginal }; for (const input of row.querySelectorAll("[data-key]")) { const raw = input.value.trim(), key = input.dataset.key; if (!raw) { delete item[key]; continue; } if (["channel", "energy_keV", "exposure_minutes", "temp", "bias"].includes(key)) { if (!Number.isFinite(Number(raw))) throw new Error(`文件映射 ${key} 需要有效数值。`); item[key] = Number(raw); } else if (key === "peak_energies_keV") item[key] = raw.split(",").map((value) => { const n = Number(value.trim()); if (!(n > 0)) throw new Error("映射表的源能线须为正数，使用逗号分隔。"); return n; }); else item[key] = raw; } return item; });
  profile.discovery.rules = [...$("profile-rule-rows").rows].map((row) => { const item = { ...row._profileOriginal }; for (const input of row.querySelectorAll("[data-key]")) { const key = input.dataset.key, value = input.value.trim(); if (key === "metadata") { if (value) { try { item.metadata = JSON.parse(value); } catch { throw new Error("文件规则常量须为 JSON 对象，例如 {\"exposure_minutes\":60}"); } if (!item.metadata || Array.isArray(item.metadata) || typeof item.metadata !== "object") throw new Error("文件规则常量须为 JSON 对象"); } else delete item.metadata; } else item[key] = value || (key === "metadata_regex" ? null : ""); } return item; });
  profile.discovery.sources = [...$("profile-source-rows").rows].map((row) => { const item = { ...row._profileOriginal }; for (const input of row.querySelectorAll("[data-key]")) { const key = input.dataset.key, raw = input.value.trim(); item[key] = key === "aliases" ? raw.split(",").map((s) => s.trim()).filter(Boolean) : key === "energies_keV" ? raw.split(",").map((s) => s.trim()).filter(Boolean).map((s) => { const value = Number(s); if (!(value > 0)) throw new Error("源能线应填写正数，使用逗号分隔。"); return value; }) : raw; } return item; });
  }
  return profile;
}
function acquisitionEnsureMetadata() {
  if (!acquisitionDraft.profile || acquisitionDraft.metadataRendered) return;
  const discovery = acquisitionDraft.profile.discovery || {};
  for (const item of discovery.manifest || []) acquisitionAppendManifest(item);
  for (const item of discovery.rules || []) acquisitionAppendRule(item);
  for (const item of discovery.sources || []) acquisitionAppendSource(item);
  acquisitionDraft.metadataRendered = true;
}
async function acquisitionOpen() {
  if (!state.run || state.busy) return; if (state.dirty) await saveRevision({}, true);
  acquisitionDraft.runId = state.run.id; acquisitionDraft.changed = false; acquisitionDraft.profile = null; $("acquisition-dialog").showModal(); acquisitionShowTab("xml"); acquisitionSetBusy(true); acquisitionStatus("正在载入本试算的读取配置…");
  try { const result = await api(runPath("/acquisition")); if (state.run.id !== acquisitionDraft.runId) return; acquisitionDraft.presets = Array.isArray(result.presets) ? result.presets : Object.entries(result.presets || {}).map(([id, preset]) => ({ id, ...preset })); acquisitionDraft.revision = result.revision; acquisitionDraft.sha256 = result.sha256 || result.hash; acquisitionRenderProfile(result.profile); $("acquisition-identity").textContent = `${state.run.payload} 读取配置`; $("acquisition-version").textContent = `${result.revision ? `修订 ${typeof result.revision === "object" ? result.revision.id || result.revision.version || "已保存" : result.revision}` : "默认读取配置"}`; const select = clear($("acquisition-preset")); select.append(new Option("10B · 波形与电流压降", "10B"), new Option("14B · 默认读取配置", "14B"), new Option("15B · 默认读取配置", "15B")); select.value = state.run.dataset?.profile || state.run.payload; if (!select.value) select.value = "14B"; $("acquisition-reason").value = ""; acquisitionStatus("配置已载入。"); $("acquisition-version").title = acquisitionDraft.sha256 || ""; await registryOpen(result); }
  catch (error) { acquisitionError(error); }
  finally { acquisitionSetBusy(false); }
}
async function acquisitionLoadPreset() { acquisitionSetBusy(true); try { const result = await api(`/api/acquisition/defaults?profile=${encodeURIComponent($("acquisition-preset").value)}`); acquisitionRenderProfile(result.profile); acquisitionMarkChanged(); acquisitionStatus("默认预设已载入草稿；保存后才应用于本试算。"); } finally { acquisitionSetBusy(false); } }
async function acquisitionLoadXml(role, preset = false) {
  const path = $(role + "-xml-path").value.trim(), id = $(role + "-xml-preset").value; if (!(preset ? id : path)) throw new Error(preset ? "请选择 XML 预设。" : "请填写 XML 的完整路径。");
  acquisitionSetBusy(true); try { const result = await api("/api/xml/read", { method: "POST", body: preset ? { preset: id } : { path } }); $(role + "-xml-text").value = result.text || ""; $(role + "-xml-path").value = result.path || path; if (acquisitionDraft.profile?.xml?.[role]) delete acquisitionDraft.profile.xml[role].imported_name; $(role + "-xml-status").textContent = `${result.tags?.length ?? 0} 个包标签 · ${(result.sha256 || "").slice(0, 8)}`; const tag = $(role === "event" ? "profile-event-tag" : "profile-hk-tag"); if (result.tags?.length === 1) tag.value = result.tags[0]; acquisitionMarkChanged(); } finally { acquisitionSetBusy(false); }
}
async function acquisitionImportXmlFile(role) {
  const input = $(role + "-xml-file"), file = input.files?.[0]; if (!file) return;
  try {
    if (!acquisitionDraft.profile) throw new Error("请先载入读取配置。");
    if (!/\.xml$/i.test(file.name)) throw new Error("请选择 .xml 文件。");
    if (file.size > 1024 * 1024) throw new Error("XML 文件超过 1 MiB，未导入。原草稿保持不变。");
    acquisitionSetBusy(true);
    let text; try { text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); } catch { throw new Error("XML 无法以 UTF-8 读取，未导入。原草稿保持不变。"); }
    $(role + "-xml-text").value = text; $(role + "-xml-path").value = "";
    acquisitionDraft.profile.xml[role].source_path = null; acquisitionDraft.profile.xml[role].imported_name = file.name;
    $(role + "-xml-status").textContent = `浏览器导入 · ${file.name}`;
    acquisitionMarkChanged(); acquisitionStatus(`已导入 ${file.name}（${number(file.size / 1024, 1)} KiB）。仅读取文件内容，请预览校验包结构后保存。`);
  } finally { input.value = ""; acquisitionSetBusy(false); }
}
function acquisitionDescribeIssue(issue) { if (typeof issue === "string") return issue; if (!issue) return ""; return [issue.event || issue.path || issue.file || issue.key, issue.message || issue.error || issue.reason || issue.detail || (Array.isArray(issue.paths) ? `${issue.role || "输入"} 匹配 ${issue.paths.length} 个文件：${issue.paths.join(", ")}` : "")].filter(Boolean).join(" · ") || JSON.stringify(issue); }
function acquisitionRenderPreview(result) {
  const messages = clear($("acquisition-preview-messages")), errors = result.errors || [], warnings = result.warnings || [];
  for (const error of errors) messages.append(node("p", "profile-preview-error", acquisitionDescribeIssue(error)));
  for (const warning of warnings) messages.append(node("p", "profile-preview-warning", acquisitionDescribeIssue(warning)));
  for (const [key, label] of [["ambiguous", "存在多重匹配"], ["unmatched", "未配对文件"]]) { const items = result[key]; if (Array.isArray(items) && items.length) { const details = node("details", "profile-advanced"), summary = node("summary", "", `${label} · ${items.length}`); details.append(summary); for (const item of items.slice(0, 80)) details.append(node("p", "profile-preview-warning", acquisitionDescribeIssue(item))); messages.append(details); } }
  const rows = result.rows || []; $("acquisition-preview-summary").textContent = `${rows.length} 组配对 · ${errors.length} 项错误 · ${warnings.length} 项提示`;
  if (!errors.length) messages.prepend(node("p", "profile-preview-ok", rows.length ? "配置校验通过。请核对文件、能线与工况来源后保存。" : "配置校验通过；当前目录未发现可计算配对，请核对数据目录。"));
  const holder = clear($("acquisition-preview-results")); if (!rows.length) { holder.append(node("p", "empty-list", "当前规则没有匹配的事件 / HK 配对。")); return; }
  const table = node("table", "profile-preview-table"), head = node("tr"), conditions = acquisitionReadProfile().parser.conditions; ["输入文件 / HK", "类别 / 输出名称", "能量 / 通道", "源 / 曝光", "标称工况", "计算工况来源"].forEach((label) => head.append(node("th", "", label))); table.append(head);
  for (const item of rows.slice(0, 500)) {
    const metadata = item.metadata || item.nominal || {}, get = (key) => item[key] ?? metadata[key], row = node("tr"), files = node("td"), event = get("event") || item.event_path || "—", hk = get("hk") || item.hk_path || "—";
    for (const path of [event, hk]) { const text = node("span", "profile-preview-file", String(path).split(/[\\/]/).pop()); text.title = String(path); files.append(text); }
    const kind = { tb: "TB 工况", xray: "X 射线", source: "放射源", background: "本底" }[get("kind")] || get("kind") || "—", category = node("td"); category.append(node("strong", "", kind), node("span", "profile-preview-file", get("stem") || get("output_stem") || ""));
    const energy = get("energy_keV") ?? get("energy"), peakEnergies = get("peak_energies_keV"), energyLabel = energy !== undefined && energy !== null ? `${number(energy, 3)} keV` : Array.isArray(peakEnergies) && peakEnergies.length ? `${peakEnergies.map((value) => number(value, 3)).join(" / ")} keV` : "—", channel = get("channel"), source = get("source"), exposure = get("exposure_minutes"), temp = get("temp") ?? get("temperature"), bias = get("bias");
    const actualMode = item.condition_mode || item.conditions?.mode || conditions.mode, modeText = { hk: "HK 实测 · L0 解包后获得", filename: "显式采用文件名标称值", manifest: "显式采用映射表标称值", fixed: `固定 ${number(conditions.temp, 2)} °C / ${number(conditions.bias, 3)} V` }[actualMode] || actualMode;
    row.append(files, category, node("td", "", `${energyLabel}${channel !== undefined && channel !== null ? ` · CH ${channel}` : ""}`), node("td", "", `${source || "—"}${exposure !== undefined && exposure !== null ? ` · ${number(exposure, 3)} min` : ""}`), node("td", "", temp !== undefined && temp !== null || bias !== undefined && bias !== null ? `${number(temp, 2)} °C / ${number(bias, 3)} V` : "未提供"), node("td", "", modeText)); table.append(row);
  }
  holder.append(table); if (rows.length > 500) holder.append(node("p", "muted-text", `预览前 500 / ${rows.length} 组，保存使用全部匹配。`));
}
async function acquisitionPreview() {
  const profile = acquisitionReadProfile(), fingerprint = JSON.stringify(profile); acquisitionSetBusy(true); acquisitionStatus("正在验证 XML 并扫描真实文件配对…"); acquisitionShowTab("preview");
  try { const result = await api(`/api/runs/${encodeURIComponent(acquisitionDraft.runId)}/acquisition/preview`, { method: "POST", body: { profile } }); acquisitionRenderPreview(result); const current = JSON.stringify(acquisitionReadProfile()); acquisitionDraft.previewFingerprint = fingerprint; acquisitionDraft.previewValid = !(result.errors || []).length && current === fingerprint; acquisitionStatus(acquisitionDraft.previewValid ? "当前草稿已校验，可保存为本试算的读取配置版本。" : current !== fingerprint ? "校验期间草稿已修改，请重新预览。" : "校验发现错误，请修改配置后重新预览。", !acquisitionDraft.previewValid); }
  finally { acquisitionSetBusy(false); }
}
async function acquisitionSave() {
  const profile = acquisitionReadProfile(); if (!acquisitionDraft.previewValid || JSON.stringify(profile) !== acquisitionDraft.previewFingerprint) { acquisitionMarkChanged(); throw new Error("当前草稿尚未校验，请先预览与校验。"); }
  acquisitionSetBusy(true); acquisitionStatus("正在保存读取配置与 XML 版本快照…");
  try { const result = await api(`/api/runs/${encodeURIComponent(acquisitionDraft.runId)}/acquisition`, { method: "PATCH", body: { profile, reason: $("acquisition-reason").value.trim() || "载荷协议与物理标签读取配置修订" } }); if (state.run.id === acquisitionDraft.runId) { state.run = result.run || result; state.surface = null; state.ecOverview = null; state.spectrum = null; state.point = null; renderRun(); await loadSurfaceSafely(); } acquisitionDraft.changed = false; $("acquisition-dialog").close(); notify("读取配置已保存，重新解包后应用。", "success"); }
  finally { acquisitionSetBusy(false); }
}
function acquisitionParseDelimited(text) {
  const delimiter = text.split(/\r?\n/, 1)[0].includes("\t") ? "\t" : ",", rows = [], row = []; let field = "", quoted = false;
  for (let i = 0; i <= text.length; i++) { const c = text[i]; if (c === '"') { if (quoted && text[i + 1] === '"') { field += '"'; i++; } else quoted = !quoted; } else if (!quoted && (c === delimiter || c === "\n" || c === undefined)) { row.push(field.trim()); field = ""; if (c !== delimiter) { if (row.some(Boolean)) rows.push([...row]); row.length = 0; } } else if (c !== "\r" || quoted) field += c; }
  if (quoted) throw new Error("映射表存在未闭合的引号。"); if (rows.length < 2) throw new Error("请粘贴含列名和至少一行数据的映射表。"); const headers = rows.shift(); if (!headers.includes("event") || !headers.includes("hk") || !headers.includes("kind")) throw new Error("映射表需要 event、hk、kind 列。");
  return rows.map((values) => Object.fromEntries(headers.map((key, index) => [key, values[index] || ""])));
}
$("acquisition-settings").addEventListener("click", () => acquisitionOpen().catch(acquisitionError));
for (const id of ["close-acquisition", "cancel-acquisition"]) $(id).addEventListener("click", () => { if (!acquisitionDraft.busy) $("acquisition-dialog").close(); });
$("acquisition-dialog").addEventListener("cancel", (event) => { if (acquisitionDraft.busy) event.preventDefault(); });
$("acquisition-dialog").addEventListener("input", (event) => { if (event.target.type !== "file" && event.target.id !== "acquisition-reason" && event.target.id !== "profile-manifest-import") acquisitionMarkChanged(); });
$("acquisition-dialog").addEventListener("change", (event) => { if (["profile-condition-mode", "profile-discovery-mode", "profile-temp-mode", "profile-bias-mode", "profile-hk-selection"].includes(event.target.id)) acquisitionUpdateModes(); if (event.target.type !== "file" && event.target.id !== "acquisition-reason") acquisitionMarkChanged(); });
for (const tab of ["xml", "metadata", "preview"]) { const button = $("acquisition-tab-" + tab); button.addEventListener("click", () => acquisitionShowTab(tab)); button.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const tabs = ["xml", "metadata", "preview"], index = event.key === "Home" ? 0 : event.key === "End" ? 2 : (tabs.indexOf(tab) + (event.key === "ArrowLeft" ? 2 : 1)) % 3; acquisitionShowTab(tabs[index]); $("acquisition-tab-" + tabs[index]).focus(); }); }
for (const role of ["event", "hk"]) { $("load-" + role + "-xml").addEventListener("click", () => acquisitionLoadXml(role).catch(acquisitionError)); $("use-" + role + "-xml-preset").addEventListener("click", () => acquisitionLoadXml(role, true).catch(acquisitionError)); $("choose-" + role + "-xml-file").addEventListener("click", () => $(role + "-xml-file").click()); $(role + "-xml-file").addEventListener("change", () => acquisitionImportXmlFile(role).catch(acquisitionError)); }
$("load-acquisition-preset").addEventListener("click", () => acquisitionLoadPreset().catch(acquisitionError));
$("preview-acquisition").addEventListener("click", () => acquisitionPreview().catch(acquisitionError)); $("save-acquisition").addEventListener("click", () => acquisitionSave().catch(acquisitionError));
$("profile-add-manifest").addEventListener("click", () => { acquisitionAppendManifest(); acquisitionMarkChanged(); }); $("profile-add-rule").addEventListener("click", () => { acquisitionAppendRule(); acquisitionMarkChanged(); }); $("profile-add-source").addEventListener("click", () => { acquisitionAppendSource(); acquisitionMarkChanged(); });
$("profile-import-manifest").addEventListener("click", () => { try { const rows = acquisitionParseDelimited($("profile-manifest-import").value); clear($("profile-manifest-rows")); for (const row of rows) acquisitionAppendManifest(row); acquisitionMarkChanged(); acquisitionStatus(`已导入 ${rows.length} 组文件映射，请预览后保存。`); } catch (error) { acquisitionError(error); } });
$("unpack-data").addEventListener("click", () => openCreateDialog());
$("unpack-settings").addEventListener("click", () => acquisitionOpen().catch(acquisitionError));
$("unpack-search").addEventListener("input", renderUnpackWorkspace);
$("unpack-file").addEventListener("change", handleError(async (event) => { const point = unpackInputs().find(item => (item.id || item.stem) === event.target.value); if (point) await selectPoint(point); }));
$("unpack-selected").addEventListener("click", handleError(() => runUnpack(false)));
$("unpack-all").addEventListener("click", () => { $("unpack-batch-summary").textContent = `${state.run.payload} · ${state.layer} · 共 ${unpackRawInputs().length} 个原始文件（所有通道，不受文件筛选影响）。`; $("start-unpack-batch").textContent = `解包全部 ${unpackRawInputs().length} 个文件`; $("unpack-batch-dialog").showModal(); });
for (const id of ["close-unpack-batch", "cancel-unpack-batch"]) $(id).addEventListener("click", () => $("unpack-batch-dialog").close());
$("start-unpack-batch").addEventListener("click", handleError(async () => { $("unpack-batch-dialog").close(); await runUnpack(true); }));
$("use-default-directories").addEventListener("click", () => { $("dataset-raw-dir").value = ""; $("dataset-product-dir").value = ""; datasetPathsChanged(); scanDatasets().catch(datasetError); });
$("new-run").addEventListener("click", () => openCreateDialog());
$("dataset-settings").addEventListener("click", () => openCreateDialog());
$("create-first-run").addEventListener("click", () => openCreateDialog());
$("close-dialog").addEventListener("click", () => $("new-run-dialog").close());
$("cancel-new-run").addEventListener("click", () => $("new-run-dialog").close());
$("new-run-form").addEventListener("submit", createRun);
$("new-run-form").addEventListener("input", () => { $("dataset-create-error").hidden=true; });
$("run-select").addEventListener("change", handleError((event) => openRun(event.target.value)));
$("payload").addEventListener("change", handleError(async (event) => { const payload = event.target.value; const remembered = localStorage.getItem(`grid-workbench-payload:${payload}`); const runs = (state.bootstrap.runs || []).filter(item => item.payload === payload); const run = runs.find(item => item.id === remembered) || runs[0]; if (run) await openRun(run.id); else openCreateDialog(payload); }));
$("channel").addEventListener("change", handleError(event => selectChannel(Number(event.target.value))));
$("l1-file-search").addEventListener("input", renderL1Workspace);
$("l1-file-filter").addEventListener("change", renderL1Workspace);
$("l1-prev").addEventListener("click", handleError(() => moveL1File(-1)));
$("l1-next").addEventListener("click", handleError(() => moveL1File(1)));
$("l1-file-list").addEventListener("keydown", handleError(async event => { if (["ArrowUp","ArrowDown"].includes(event.key)) { event.preventDefault(); await moveL1File(event.key === "ArrowUp" ? -1 : 1); $("l1-file-list").querySelector(".active")?.focus(); } }));
$("l1-fit-all-channels").addEventListener("click", handleError(fitAllL1Channels));
$("point-select").addEventListener("change", handleError(async (event) => { const collection = state.layer.startsWith("EC") ? ["EC_L0", "EC_L1"].includes(state.layer) ? state.ecOverview?.files : state.ecOverview?.points : state.surface?.points; const point = collection?.find((p) => (p.id || p.stem) === event.target.value); if (point) await selectPoint(point); }));
$("peak-file-search").addEventListener("input",renderEcReviewFiles);
$("peak-file-prev").addEventListener("click",handleError(()=>moveEcReviewFile(-1)));
$("peak-file-next").addEventListener("click",handleError(()=>moveEcReviewFile(1)));
$("review-next-layer").addEventListener("click",handleError(reviewNextLayer));
for (const id of ["window-lo", "window-hi"]) $(id).addEventListener("input", () => { $(id).dataset.edited = "true"; $(id).title = ""; state.dirty = true; $("window-status").textContent = "窗口已修改 · 未拟合"; fitFeedback(unsupportedSpectralModel() ? "窗口已修改" : "窗口已修改 · 点击“按当前窗口拟合”"); renderRunBadge(); if (window.Plotly && spectrumChart().data) Plotly.relayout(spectrumChart(), { shapes: [...windowShapes(windowValue("window-lo"), windowValue("window-hi")), ...peakPlotDetails(state.spectrum || {}).shapes] }); });
$("save-run").addEventListener("click", handleError(() => saveRevision()));
$("fit-spectrum").addEventListener("click", handleError(fitSpectrum));
$("auto-fit-spectrum").addEventListener("click", handleError(autoFitSpectrum));
$("toggle-excluded").addEventListener("click", handleError(toggleExcluded));
$("refit-tb").addEventListener("click", handleError(async () => { if (unsupportedSpectralModel()) throw new Error(unsupportedSpectralModel()); if (state.dirty) await saveRevision({}, true); await executeJob(state.layer.startsWith("EC") ? "EC_L3" : "TB_L2"); }));
$("run-layer").addEventListener("click", handleError(runCurrentLayer));
$("run-scope").addEventListener("change", () => { updateRunScopeCaption(); setBusy(state.busy); });
$("run-pipeline").textContent="运行流程…"; $("run-pipeline").title="选择输入文件与处理步骤";
document.querySelector(".pipeline-menu p").textContent="选择输入文件与处理步骤";
$("run-pipeline").addEventListener("click", handleError(openPipelineDialog));
$("refresh-run").addEventListener("click", handleError(() => refreshRun()));
$("tab-artifacts").addEventListener("click", () => showTab("artifacts"));
$("tab-jobs").addEventListener("click", () => showTab("jobs"));
$("dismiss-message")?.addEventListener("click", () => { $("message").hidden = true; $("dismiss-message").hidden = true; });
$("surface-mode").addEventListener("change", handleError(async (event) => { state.surfaceMode = event.target.value; await renderTBSurface(); await highlightSelectedPoint(); }));
$("ec-detail-mode").addEventListener("change", handleError(renderEcOverview));
$("dataset-select").addEventListener("change", updateSelectedDataset);
$("scan-datasets").addEventListener("click", () => scanDatasets().catch(datasetError));
for (const id of ["dataset-raw-dir", "dataset-product-dir"]) $(id).addEventListener("input", datasetPathsChanged);
for (const button of document.querySelectorAll("[data-browse-target]")) button.addEventListener("click", handleError(() => browseDirectory(button.dataset.browseTarget)));
$("close-directory-dialog").addEventListener("click", () => $("directory-dialog").close());
$("directory-up").addEventListener("click", handleError(() => loadDirectory(directoryState.parent)));
$("directory-go").addEventListener("click", handleError(() => loadDirectory($("directory-location").value.trim())));
$("directory-location").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadDirectory(event.target.value.trim()).catch(messageError); } });
$("choose-directory").addEventListener("click", handleError(async () => { if (!directoryState) return; $(directoryTarget).value = directoryState.path; $("directory-dialog").close(); if (directoryTarget === "reference-directory") await referenceScan(); else datasetPathsChanged(); }));
$("apply-tb-version").addEventListener("click", handleError(applyTbVersion));
$("tb-version-select").addEventListener("change", () => { const selected = state.tbVersions.find((v) => v.id === $("tb-version-select").value); $("tb-binding-state").textContent = selected ? `SHA-256 ${(selected.sha256 || "").slice(0, 10)} · 点击采用后生效` : ""; });
$("import-tb-version").addEventListener("click", handleError(async () => { const path = $("tb-import-path").value.trim(); if (!path) throw new Error("请填写 TB L2 JSON 的完整路径。"); if (state.dirty) await saveRevision({}, true); const result = await api(runPath("/dependency"), { method: "PATCH", body: { path } }); state.run = result.run || result; $("tb-import-path").value = ""; renderRun(); await loadSurfaceSafely(); notify("TB 参数已载入，可运行 EC L1。", "success"); }));
$("tb-analysis").after($("ec-coverage-details"));
for (const view of ["residual", "comparison"]) {
  $("analysis-tab-" + view)?.addEventListener("click", () => showAnalysisView(view));
  $("analysis-tab-" + view)?.addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "residual" : event.key === "End" ? "comparison" : state.analysisView === "residual" ? "comparison" : "residual"; showAnalysisView(next); $("analysis-tab-" + next)?.focus(); } });
}
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resizeCharts, 100); });
$("records-drawer")?.addEventListener("toggle", () => requestAnimationFrame(resizeCharts));
window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
bootstrap().catch((error) => { $("connection-label").textContent = state.bootstrap ? "服务已连接 · 试算恢复失败" : "本地服务连接失败"; messageError(error); });
