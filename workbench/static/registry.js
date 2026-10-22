/* Reusable acquisition profiles and read-only reference-spectrum inspection. */
"use strict";

const payloadRegistry = { presets: [], draftReference: null, openToken: 0, datasetToken: 0, datasetLoading: false, datasetError: null, datasetId: null };
const referenceState = { files: [], spectrum: null, analysis: null, token: 0, busy: false };

function registryReferenceLabel(reference) {
  return reference ? `${reference.name || reference.id} · ${reference.payload || "载荷"} · v${reference.version}` : "本试算尚未关联可复用预设";
}

function registryPreset(id) { return payloadRegistry.presets.find((preset) => preset.id === id); }
function registryVersions(preset) { return [...(preset?.versions || [])].sort((a, b) => Number(b.version) - Number(a.version)); }
function registryFillVersions(selectId, preset, selectedVersion) {
  const select = clear($(selectId));
  for (const version of registryVersions(preset)) {
    const option = new Option(`v${version.version}${Number(version.version) === Number(preset.latest_version) ? " · 最新" : ""}`, String(version.version));
    option.title = `${timestamp(version.created_at)} · ${(version.sha256 || "").slice(0, 12)}${version.reason ? `\n${version.reason}` : ""}`;
    select.append(option);
  }
  if (selectedVersion !== null && selectedVersion !== undefined) select.value = String(selectedVersion);
  if (!select.value && select.options.length) select.selectedIndex = 0;
  select.disabled = !select.options.length;
}

function registryCurrentSummary() {
  const reference = state.run?.acquisition_preset;
  $("payload-preset-current").textContent = `${registryReferenceLabel(reference)}${reference?.modified ? " · 本试算配置已有修改" : reference ? ` · SHA-256 ${(reference.sha256 || "").slice(0, 10)}` : "。校验草稿后可另存为载荷预设。"}`;
}

async function registryFetch() {
  const result = await api("/api/acquisition/presets");
  payloadRegistry.presets = result.presets || [];
  return payloadRegistry.presets;
}

async function registryOpen(acquisitionResponse = {}) {
  const token = ++payloadRegistry.openToken;
  const runId = acquisitionDraft.runId;
  const reference = acquisitionResponse.acquisition_preset || state.run?.acquisition_preset || null;
  payloadRegistry.draftReference = reference;
  $("payload-preset-current").textContent = "正在载入载荷预设…";
  $("payload-preset-name").value = reference?.name || `${state.run?.payload || "载荷"} 读取配置`;
  $("payload-preset-payload").value = reference?.payload || state.run?.payload || "";
  $("payload-preset-bind").checked = true;
  try {
    await registryFetch();
    if (token !== payloadRegistry.openToken || runId !== acquisitionDraft.runId) return;
    const select = clear($("payload-preset-select"));
    select.append(new Option("选择已保存的载荷预设", ""));
    for (const preset of payloadRegistry.presets) select.append(new Option(`${preset.payload} · ${preset.name}`, preset.id));
    select.value = reference?.id || "";
    registryFillVersions("payload-preset-version", registryPreset(select.value), reference?.version);
    registryCurrentSummary();
    registryRefreshButtons();
  } catch (error) {
    if (token === payloadRegistry.openToken) {
      $("payload-preset-current").textContent = `预设列表载入失败：${error.message}`;
      clear($("payload-preset-select")).append(new Option("预设暂不可用", ""));
      registryFillVersions("payload-preset-version", null);
      registryRefreshButtons();
    }
  }
}

function registryValidatedDraft() {
  if (!acquisitionDraft.profile || !acquisitionDraft.previewValid) return false;
  try { return JSON.stringify(acquisitionReadProfile()) === acquisitionDraft.previewFingerprint; } catch { return false; }
}

function registryRefreshButtons() {
  const busy = !!acquisitionDraft.busy || !!state.busy;
  const selected = registryPreset($("payload-preset-select").value);
  const hasVersion = !!selected && !!$("payload-preset-version").value;
  const valid = registryValidatedDraft();
  $("load-payload-preset").disabled = busy || !hasVersion;
  $("apply-payload-preset").disabled = busy || !hasVersion || !state.run;
  $("save-payload-preset-as").disabled = busy || !valid || !$("payload-preset-name").value.trim() || !$("payload-preset-payload").value.trim();
  $("publish-payload-preset-version").disabled = busy || !valid || !selected;
  $("payload-preset-select").disabled = busy;
  $("payload-preset-version").disabled = busy || !hasVersion;
}

async function registrySelected() {
  const id = $("payload-preset-select").value;
  const version = Number($("payload-preset-version").value);
  if (!id || !(version > 0)) throw new Error("请选择载荷预设与版本。");
  return api(`/api/acquisition/presets/${encodeURIComponent(id)}?version=${version}`);
}

async function registryLoadDraft() {
  acquisitionSetBusy(true);
  try {
    const result = await registrySelected();
    acquisitionRenderProfile(result.profile);
    payloadRegistry.draftReference = result.reference;
    $("payload-preset-name").value = result.preset.name;
    $("payload-preset-payload").value = result.preset.payload;
    acquisitionMarkChanged();
    acquisitionShowTab("xml");
    acquisitionStatus(`${registryReferenceLabel(result.reference)} 已载入草稿。预览后可保存；点击“切换试算版本”可直接采用已保存版本。`);
  } finally { acquisitionSetBusy(false); registryRefreshButtons(); }
}

async function registryRefreshRun(result) {
  state.run = result.run || result;
  state.surface = null; state.ecOverview = null; state.spectrum = null; state.point = null;
  renderRun();
  await loadSurfaceSafely();
}

async function registryApply() {
  const id = $("payload-preset-select").value;
  const version = Number($("payload-preset-version").value);
  if (!id || !(version > 0)) throw new Error("请选择载荷预设与版本。");
  if (state.dirty) await saveRevision({}, true);
  acquisitionSetBusy(true);
  try {
    const result = await api(`/api/runs/${encodeURIComponent(acquisitionDraft.runId)}/acquisition/preset`, {
      method: "POST", body: { preset_id: id, version, reason: $("acquisition-reason").value.trim() || "切换载荷读取预设版本" }
    });
    await registryRefreshRun(result);
    const response = await api(runPath("/acquisition"));
    acquisitionDraft.revision = response.revision;
    acquisitionDraft.sha256 = response.sha256;
    acquisitionRenderProfile(response.profile);
    acquisitionDraft.changed = false;
    $("acquisition-version").textContent = `配置版本 ${response.revision} · SHA-256 ${(response.sha256 || "").slice(0, 10)}`;
    await registryOpen(response);
    acquisitionStatus(`本试算已采用 ${registryReferenceLabel(state.run.acquisition_preset)}。相关层的依赖状态已更新。`);
    notify(`载荷读取配置已切换至 v${version}，后续计算使用该版本的 XML、换算、文件规则及源能线。`, "success");
  } finally { acquisitionSetBusy(false); registryRefreshButtons(); }
}

async function registrySave(asVersion) {
  if (!registryValidatedDraft()) throw new Error("请先预览并校验当前草稿，再保存为载荷预设。");
  const presetId = asVersion ? $("payload-preset-select").value : null;
  if (asVersion && !presetId) throw new Error("请选择要增加版本的载荷预设。");
  const name = $("payload-preset-name").value.trim(), payload = $("payload-preset-payload").value.trim();
  if (!name || !payload) throw new Error("请填写预设名称和所属载荷。");
  if (state.dirty) await saveRevision({}, true);
  const profile = acquisitionReadProfile();
  const bindDirectories = $("payload-preset-bind").checked;
  acquisitionSetBusy(true);
  acquisitionStatus(asVersion ? "正在保存载荷预设的新版本…" : "正在另存为载荷预设…");
  try {
    const result = await api(`/api/runs/${encodeURIComponent(acquisitionDraft.runId)}/acquisition/presets`, {
      method: "POST", body: { profile, name, payload, ...(presetId ? { preset_id: presetId } : {}), reason: $("acquisition-reason").value.trim() || (asVersion ? "更新载荷读取预设" : "另存为载荷读取预设"), bind_directories: bindDirectories }
    });
    if (result.run) await registryRefreshRun(result.run);
    const response = await api(runPath("/acquisition"));
    acquisitionDraft.revision = response.revision;
    acquisitionDraft.sha256 = response.sha256;
    acquisitionRenderProfile(response.profile);
    acquisitionDraft.changed = false;
    $("acquisition-version").textContent = `配置版本 ${response.revision} · SHA-256 ${(response.sha256 || "").slice(0, 10)}`;
    await registryOpen(response);
    const reference = result.reference || state.run.acquisition_preset;
    acquisitionStatus(`${registryReferenceLabel(reference)} 已保存${bindDirectories ? "，并关联当前数据与产品目录" : ""}。新建试算可直接复用。`);
    notify(`已保存 ${registryReferenceLabel(reference)}。`, "success");
  } finally { acquisitionSetBusy(false); registryRefreshButtons(); }
}

function registryDatasetSummary() {
  const dataset = state.datasets.find((item) => item.id === $("dataset-select").value);
  const match = dataset?.acquisition_preset_match || {};
  const selected = registryPreset($("dataset-preset").value);
  const version = $("dataset-preset-version").value;
  if (selected) {
    const matched = match.reference?.id === selected.id && Number(match.reference?.version) === Number(version);
    $("dataset-preset-summary").textContent = `${matched ? "目录自动匹配" : "手动选择"}：${selected.name} · ${selected.payload} · v${version}。新试算保存完整配置快照。`;
  } else if (match.status === "conflict") {
    $("dataset-preset-summary").textContent = `${(match.warnings || []).join(" ") || "目录关联了不同的读取预设。"} 请明确选择预设和版本。`;
  } else if (match.status === "matched") {
    $("dataset-preset-summary").textContent = `自动采用 ${registryReferenceLabel(match.reference)}。`;
  } else {
    $("dataset-preset-summary").textContent = "当前目录尚未关联载荷预设。可先使用默认读取配置，在试算中校验后另存为可复用预设。";
  }
  $("dataset-preset-summary").classList.toggle("scan-error", match.status === "conflict" && !selected);
}

async function registryDatasetSelection() {
  const token = ++payloadRegistry.datasetToken;
  const datasetId = $("dataset-select").value;
  payloadRegistry.datasetId = datasetId;
  payloadRegistry.datasetLoading = true;
  payloadRegistry.datasetError = null;
  $("dataset-preset-summary").textContent = "正在匹配载荷读取预设…";
  clear($("dataset-preset")).append(new Option("正在读取预设…", ""));
  registryFillVersions("dataset-preset-version", null);
  $("dataset-preset").disabled = true;
  try {
    await registryFetch();
    if (token !== payloadRegistry.datasetToken || datasetId !== $("dataset-select").value) return;
    const dataset = state.datasets.find((item) => item.id === datasetId);
    const match = dataset?.acquisition_preset_match || {};
    const select = clear($("dataset-preset"));
    select.append(new Option(match.status === "conflict" ? "存在关联冲突，请明确选择" : "按目录自动选择", ""));
    for (const preset of payloadRegistry.presets) select.append(new Option(`${preset.payload} · ${preset.name}`, preset.id));
    if (match.status === "matched") select.value = match.reference?.id || "";
    if (match.status === "matched" && $("new-run-payload").value === dataset.payload && match.reference?.payload) $("new-run-payload").value = match.reference.payload;
    registryFillVersions("dataset-preset-version", registryPreset(select.value), match.reference?.version);
    registryDatasetSummary();
  } catch (error) {
    if (token === payloadRegistry.datasetToken) { payloadRegistry.datasetError = error.message; $("dataset-preset-summary").textContent = `预设匹配失败：${error.message}`; }
  } finally {
    if (token === payloadRegistry.datasetToken) { payloadRegistry.datasetLoading = false; $("dataset-preset").disabled = false; }
  }
}

function registryCreateSpec() {
  if (payloadRegistry.datasetLoading) throw new Error("正在匹配载荷预设，请稍后创建试算。");
  if (payloadRegistry.datasetError) throw new Error(`预设匹配失败，请重新扫描目录：${payloadRegistry.datasetError}`);
  const dataset = state.datasets.find((item) => item.id === $("dataset-select").value);
  const match = dataset?.acquisition_preset_match || {};
  const presetId = $("dataset-preset").value;
  if (presetId) {
    const presetVersion = Number($("dataset-preset-version").value);
    if (!(presetVersion > 0) || !registryPreset(presetId)) throw new Error("请选择有效的载荷预设版本。");
    return { preset_id: presetId, preset_version: presetVersion };
  }
  if (match.status === "conflict") throw new Error("所选数据与产品目录关联的读取预设存在冲突。请先明确选择预设和版本。");
  if (match.status === "matched" && match.reference) return { preset_id: match.reference.id, preset_version: match.reference.version };
  return {};
}

function referenceSetBusy(busy, status) {
  referenceState.busy = busy;
  for (const id of ["scan-reference", "reference-file", "reference-directory"]) $(id).disabled = busy;
  $("analyze-reference").disabled = busy || !referenceState.spectrum;
  $("reference-dialog").setAttribute("aria-busy", String(busy));
  if (status) $("reference-status").textContent = status;
}

function referenceError(error) {
  $("reference-status").textContent = error.message || String(error);
  $("reference-status").classList.add("scan-error");
}

async function referenceOpen() {
  if (!$("reference-dialog").open) $("reference-dialog").showModal();
  await referenceScan();
}

async function referenceScan() {
  const directory = $("reference-directory").value.trim();
  const token = ++referenceState.token;
  referenceState.spectrum = null; referenceState.analysis = null;
  referenceSetBusy(true, "正在读取参考能谱目录…");
  $("reference-status").classList.remove("scan-error");
  clear($("reference-file")); clear($("reference-metadata")); clear($("reference-analysis"));
  chartMessage("reference-chart", "正在载入 TKA 计数谱…");
  try {
    const result = await api(`/api/reference-spectra${directory ? `?directory=${encodeURIComponent(directory)}` : ""}`);
    if (token !== referenceState.token) return;
    referenceState.files = result.files || [];
    $("reference-directory").value = result.directory || directory;
    for (const file of referenceState.files) {
      const invalid = ["error", "invalid", "unsupported"].includes(file.status);
      const option = new Option(`${file.filename}${invalid ? " · 无法读取" : ""}`, file.path);
      option.title = file.path; option.disabled = invalid; $("reference-file").append(option);
    }
    const first = [...$("reference-file").options].find((option) => !option.disabled);
    if (!first) { chartMessage("reference-chart", "此目录没有可读取的 TKA 计数谱。"); $("reference-status").textContent = "没有可读取的谱文件"; return; }
    $("reference-file").value = first.value;
    await referenceLoad(first.value, token);
  } finally { if (token === referenceState.token) referenceSetBusy(false); }
}

function referenceMetadata(spectrum) {
  const metadata = spectrum.metadata || {};
  const holder = clear($("reference-metadata"));
  holder.append(node("p", "", spectrum.filename), node("p", "", `横轴：ADC 通道 · ${spectrum.x?.length || 0} bins`));
  const fields = [
    ["实时间 / s", metadata.real_time_seconds ?? metadata.real_time ?? metadata.real_time_s],
    ["活时间 / s", metadata.live_time_seconds ?? metadata.live_time ?? metadata.live_time_s],
    ["总计数", metadata.total_counts ?? (spectrum.y || []).reduce((sum, value) => sum + Number(value || 0), 0)],
    ["测量时间", metadata.acquired_at || metadata.date || metadata.start_time]
  ];
  for (const [label, value] of fields) if (value !== null && value !== undefined && value !== "") holder.append(node("p", "", `${label}：${typeof value === "number" ? number(value, 3) : value}`));
  if (metadata.source || metadata.source_name) holder.append(node("p", "", `文件标记：${metadata.source || metadata.source_name}`));
  if (metadata.energies_keV?.length) holder.append(node("p", "", `已知能线 / keV：${metadata.energies_keV.map((value) => number(value, 3)).join("、")}`));
  if (metadata.nominal_energy_keV !== undefined) holder.append(node("p", "", `文件标称能量：${number(metadata.nominal_energy_keV, 3)} keV`));
  holder.append(node("p", "mono", `SHA-256 ${(spectrum.sha256 || "").slice(0, 14)}`));
  const details = node("details"), summary = node("summary", "", "文件与读取信息"); details.append(summary, node("p", "", spectrum.path));
  for (const [key, value] of Object.entries(metadata)) details.append(node("p", "", `${key}：${valueText(value)}`));
  holder.append(details);
}

async function referenceLoad(path, scanToken) {
  const token = scanToken ?? ++referenceState.token;
  referenceState.spectrum = null; referenceState.analysis = null;
  referenceSetBusy(true, "正在读取真实计数谱…");
  $("reference-status").classList.remove("scan-error");
  clear($("reference-analysis"));
  try {
    const result = await api(`/api/reference-spectrum?path=${encodeURIComponent(path)}`);
    if (token !== referenceState.token) return;
    referenceState.spectrum = result;
    referenceMetadata(result);
    await referencePlot();
    $("reference-status").textContent = `已读取 ${result.filename} · ${(result.y || []).length} 个计数通道`;
  } finally { if (token === referenceState.token) referenceSetBusy(false); }
}

async function referencePlot() {
  const spectrum = referenceState.spectrum;
  if (!spectrum) return;
  if (!window.Plotly) { chartMessage("reference-chart", "绘图库尚未载入，请重试。"); return; }
  const detection = referenceState.analysis?.peak_detection || {};
  const candidates = detection.candidates || [];
  const traces = [{ type: "scatter", mode: "lines", x: spectrum.x, y: spectrum.y, name: "真实计数谱", line: { color: chartColors.blue, width: 1.15 }, hovertemplate: "%{x:.1f} ADC<br>%{y:.0f} counts<extra></extra>" }];
  if (candidates.length) {
    const closestY = (center) => {
      const x = spectrum.x || [], y = spectrum.y || [];
      if (!x.length) return null;
      const first = x[0], step = x.length > 1 ? (x[x.length - 1] - first) / (x.length - 1) : 1;
      return y[Math.max(0, Math.min(y.length - 1, Math.round((center - first) / Math.max(step, Number.EPSILON))))];
    };
    traces.push({ type: "scatter", mode: "markers", x: candidates.map((item) => item.center), y: candidates.map((item) => closestY(item.center)), name: "寻峰候选", marker: { size: 9, symbol: "circle-open", color: candidates.map((item) => item.noise_candidate ? chartColors.excluded : Math.abs(Number(item.center) - Number(detection.selected_center)) < 1e-8 ? chartColors.fit : chartColors.blue), line: { width: 2 } }, customdata: candidates.map((item) => [item.sigma, item.significance, item.noise_candidate ? "噪声候选" : "峰候选"]), hovertemplate: "%{customdata[2]} · %{x:.2f} ADC<br>σ %{customdata[0]:.2f} ADC · 显著性 %{customdata[1]:.2f}<extra></extra>" });
  }
  const shapes = [];
  if (Number.isFinite(Number(detection.noise_boundary)) && detection.noise_boundary !== null && detection.noise_boundary !== undefined) shapes.push({ type: "line", x0: detection.noise_boundary, x1: detection.noise_boundary, y0: 0, y1: 1, yref: "paper", line: { color: chartColors.excluded, dash: "dot", width: 1.5 } });
  if (detection.selected_center !== null && detection.selected_center !== undefined) shapes.push({ type: "line", x0: detection.selected_center, x1: detection.selected_center, y0: 0, y1: 1, yref: "paper", line: { color: chartColors.fit, dash: "dash", width: 1 } });
  await Plotly.react($("reference-chart"), traces, basicPlotLayout("ADC 通道", "计数 / bin", { uirevision: spectrum.sha256 || spectrum.path, shapes, margin: { l: 70, r: 22, t: 18, b: 52 } }), plotConfig);
}

function referenceRenderAnalysis(result) {
  const detection = result.peak_detection || {}, holder = clear($("reference-analysis"));
  holder.append(node("p", "", `算法：${detection.algorithm || "—"}${detection.selected_center !== undefined && detection.selected_center !== null ? ` · 推荐峰位 ${number(detection.selected_center, 3)} ADC` : ""}`));
  const failure = detection.error || result.error || (result.status === "needs_review" ? result.reason : null);
  if (failure) holder.append(node("p", "peak-evidence-warning", failure));
  for (const warning of detection.warnings || []) holder.append(node("p", "peak-evidence-warning", warning));
  if (detection.candidates?.length) {
    const wrap = node("div", "table-wrap"), table = node("table"), head = node("tr");
    for (const label of ["候选峰位 / ADC", "σ / ADC", "局部显著性", "尺度证据", "判别"]) head.append(node("th", "", label));
    table.append(head);
    for (const item of detection.candidates) {
      const row = node("tr");
      const selected = detection.selected_center !== undefined && Math.abs(Number(item.center) - Number(detection.selected_center)) < 1e-8;
      for (const value of [number(item.center, 3), number(item.sigma, 3), number(item.significance, 2), item.scales ?? "—", item.noise_candidate ? "噪声候选" : selected ? "推荐峰" : "待复核候选"]) row.append(node("td", "", value));
      table.append(row);
    }
    wrap.append(table); holder.append(wrap);
  } else if (!failure) holder.append(node("p", "muted-text", "未报告统计可分辨的候选峰。"));
  if (result.record_path) holder.append(node("p", "muted-text", `分析记录：${result.record_path}`));
}

async function referenceAnalyze() {
  const spectrum = referenceState.spectrum;
  if (!spectrum) throw new Error("请先选择参考能谱。");
  const token = referenceState.token;
  referenceSetBusy(true, "正在分析候选峰、局部背景与计数统计…");
  $("reference-status").classList.remove("scan-error");
  try {
    const result = await api("/api/reference-spectra/analyze", { method: "POST", body: { path: spectrum.path } });
    if (token !== referenceState.token) return;
    referenceState.analysis = result;
    referenceRenderAnalysis(result);
    await referencePlot();
    $("reference-status").textContent = result.peak_detection?.error || result.status === "needs_review" ? "未锁定唯一峰；请查看统计证据" : "寻峰分析完成 · 结果已记录";
  } finally { if (token === referenceState.token) referenceSetBusy(false); }
}

for (const id of ["payload-preset-select", "payload-preset-version", "payload-preset-name", "payload-preset-payload", "payload-preset-bind"]) {
  for (const type of ["input", "change"]) $(id).addEventListener(type, (event) => { event.stopPropagation(); registryRefreshButtons(); });
}
$("payload-preset-select").addEventListener("change", () => { const preset = registryPreset($("payload-preset-select").value); registryFillVersions("payload-preset-version", preset); if (preset) { $("payload-preset-name").value = preset.name; $("payload-preset-payload").value = preset.payload; } registryRefreshButtons(); });
$("load-payload-preset").addEventListener("click", () => registryLoadDraft().catch(acquisitionError));
$("apply-payload-preset").addEventListener("click", () => registryApply().catch(acquisitionError));
$("save-payload-preset-as").addEventListener("click", () => registrySave(false).catch(acquisitionError));
$("publish-payload-preset-version").addEventListener("click", () => registrySave(true).catch(acquisitionError));
$("dataset-preset").addEventListener("change", () => { const preset = registryPreset($("dataset-preset").value); registryFillVersions("dataset-preset-version", preset); registryDatasetSummary(); });
$("dataset-preset-version").addEventListener("change", registryDatasetSummary);
$("reference-spectra").addEventListener("click", () => referenceOpen().catch(referenceError));
$("close-reference").addEventListener("click", () => $("reference-dialog").close());
$("scan-reference").addEventListener("click", () => referenceScan().catch(referenceError));
$("reference-directory").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); referenceScan().catch(referenceError); } });
$("reference-file").addEventListener("change", () => referenceLoad($("reference-file").value).catch(referenceError));
$("analyze-reference").addEventListener("click", () => referenceAnalyze().catch(referenceError));


let indexedSources = [];
let sourceLibraryLoaded = false;
let sourceLibraryRequest = null;
function sourceLibrarySummary() {
  const item = indexedSources.find(item => item.id === $('source-select').value);
  $('source-summary').textContent = item ? `${item.root} · ${number(item.total_bytes / 1e9, 3)} GB · 索引 ${item.indexed_at} · ${item.errors_count || 0} 项读取错误` : indexedSources.length ? `已保存 ${indexedSources.length} 个只读目录索引` : '尚未保存目录索引。填写原始数据目录后可建立索引。';
}
async function sourceLibraryRefresh(force = false) {
  if (sourceLibraryLoaded && !force) { sourceLibrarySummary(); return indexedSources; }
  if (sourceLibraryRequest) {
    if (!force) return sourceLibraryRequest;
    await sourceLibraryRequest;
    return sourceLibraryRefresh(true);
  }
  sourceLibraryRequest = (async () => {
    const result = await api('/api/sources');
    const select = $('source-select'), previousId = select.value;
    indexedSources = result.sources || [];
    clear(select).append(new Option('选择已索引目录', ''));
    for (const item of indexedSources) select.append(new Option(`${item.label} · ${item.file_count} 文件`, item.id));
    select.value = indexedSources.some(item => item.id === previousId) ? previousId : '';
    sourceLibraryLoaded = true;
    sourceLibrarySummary();
    return indexedSources;
  })();
  try { return await sourceLibraryRequest; }
  finally { sourceLibraryRequest = null; }
}
$('source-library').addEventListener('toggle', () => { if ($('source-library').open) sourceLibraryRefresh().catch(datasetError); });
$('source-select').addEventListener('change', sourceLibrarySummary);
$('use-source').addEventListener('click', () => {
  const item = indexedSources.find(item => item.id === $('source-select').value);
  if (!item) { datasetError(new Error('请选择已索引目录。')); return; }
  $('dataset-raw-dir').value = item.root; $('dataset-product-dir').value = '';
  scanDatasets().catch(datasetError);
});
$('index-source').addEventListener('click', async () => {
  const root = $('dataset-raw-dir').value.trim();
  if (!root) { datasetError(new Error('先填写或浏览选择原始数据目录。')); return; }
  $('index-source').disabled = true; $('source-status').textContent = '正在读取文件目录信息…';
  try {
    const result = await api('/api/sources/index', {method:'POST', body:{root}});
    await sourceLibraryRefresh(true); $('source-select').value = result.id; sourceLibrarySummary();
    $('source-status').textContent = `索引已保存 · ${result.file_count} 文件 · ${number(result.total_bytes / 1e9, 3)} GB · ${result.errors_count || 0} 项读取错误`;
  } catch (error) { $('source-status').textContent = error.message; }
  finally { $('index-source').disabled = false; }
});
