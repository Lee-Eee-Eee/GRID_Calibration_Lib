"""Run storage and serialized processor jobs. All writes stay beneath workspace/runs."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import math
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..common.fit_policy import usable_peak
from ..common.spectral_models import detector_identity, photopeak_channels, unsupported_spectral_model
from .acquisition_store import freeze_profile, load_profile, prepare_profile, presets, profile_digest
from .artifacts import preview as preview_artifact
from .compute import ComputeAdapter
from .datasets import DatasetCatalog, validate_payload
from .dependencies import snapshot, tb_package
from .ec_interactive import ECInspector
from .ec_peaks import catalog as source_peak_catalog
from .ec_peaks import draft_points, normalize_peak_sets
from .presets import PresetRegistry

LAYERS = ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3")
DEPENDENCIES = {
    "TB_L0": [],
    "TB_L1": ["TB_L0"],
    "TB_L2": ["TB_L1"],
    "EC_L0": [],
    "EC_L1": ["EC_L0", "TB_L2"],
    "EC_L2": ["EC_L1"],
    "EC_L3": ["EC_L2"],
}
PURPOSES = {
    "TB_L0": ("原始工况解析", "event.dat + hk", "含 hk_data 的 L0 parquet"),
    "TB_L1": ("单峰与单背景拟合", "L0 脉冲幅度、逐通道窗口", "峰参数 JSON、拟合图"),
    "TB_L2": ("温偏曲面拟合", "L1 峰中心、误差与有效点", "四通道 11 字段 TB 参数、曲面图"),
    "EC_L0": ("能量标定数据解析", "X 射线、放射源与本底原始包", "逐脉冲 T/V 的 L0 parquet"),
    "EC_L1": ("固定 TB 版本矫正", "EC L0 + 指定 TB L2", "amp_corr、tb_correction metadata"),
    "EC_L2": ("能线谱峰拟合", "EC L1 矫正谱", "能线峰参数 JSON、拟合图"),
    "EC_L3": ("能量关系与分辨率", "EC L2 能线峰参数", "EC 系数 JSON、拟合图"),
}


def _response_payload(state):
    return detector_identity(state["payload"], state.get("dataset"), state.get("acquisition_preset"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean(value):
    """Strict JSON, including NumPy scalar/container values from old processors."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "tolist"):
        return clean(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def layer_dir(root, payload, layer):
    pipeline, level = layer.split("_")
    return root / payload / pipeline.lower() / level


def peak_job_settings(layer, spec):
    """Validate scientific peak-search options before creating any job files."""
    supplied = set(spec) & {"automatic_window", "peak_detection"}
    if supplied and layer not in ("TB_L1", "EC_L2"):
        raise ValueError("自动寻峰配置只适用于 TB L1 / EC L2")
    result = {}
    if "bin_width" in spec:
        width = spec["bin_width"]
        if (layer != "EC_L2" or isinstance(width, bool) or not isinstance(width, (int, float))
                or not math.isfinite(width) or width <= 0):
            raise ValueError("EC L2 bin_width 必须为正的有限 ADC 数值")
        result["bin_width"] = float(width)
    if "fit_channels" in spec:
        channels = spec["fit_channels"]
        if (layer not in ("TB_L1", "EC_L2") or not isinstance(channels, list) or not channels
                or any(type(ch) is not int or ch not in range(4) for ch in channels)):
            raise ValueError("fit_channels 只适用于 TB L1 / EC L2，须为 0–3 的通道列表")
        result["fit_channels"] = sorted(set(channels))
    if "automatic_window" in supplied:
        if not isinstance(spec["automatic_window"], bool):
            raise ValueError("automatic_window 必须为布尔值")
        result["automatic_window"] = spec["automatic_window"]
    if "peak_detection" not in supplied:
        return result
    options = spec["peak_detection"]
    allowed = {
        "min_significance",
        "min_sigma_bins",
        "max_resolution",
        "min_resolution",
        "noise_ceiling",
        "expected_center",
        "expected_energy_keV",
        "expected_tolerance",
    }
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError("自动寻峰参数名称无效")
    checked = {}
    for key, value in options.items():
        if value is None and key in ("noise_ceiling", "expected_center"):
            checked[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"自动寻峰参数 {key} 必须为有限数值")
        if value < 0 or (value == 0 and key not in ("noise_ceiling", "min_resolution")):
            raise ValueError(f"自动寻峰参数 {key} 必须为正数")
        if key == "expected_tolerance" and value > 1:
            raise ValueError("expected_tolerance 必须在 (0, 1] 内")
        checked[key] = float(value)
    if "expected_energy_keV" in checked and (layer != "EC_L2" or checked.get("expected_center") is None):
        raise ValueError("能线锚点需要 EC L2 的期望 ADC 峰位")
    result["peak_detection"] = checked
    return result


class Workbench:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.runs = self.workspace / "runs"
        self.runs.mkdir(exist_ok=True)
        self.adapter = ComputeAdapter(self.workspace)
        self.catalog = DatasetCatalog(self.workspace)
        self.preset_registry = PresetRegistry(self.workspace)
        self.lock = threading.RLock()
        # Matplotlib and the legacy parser have process-global state.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calibration")
        self.inventory_cache = {}
        self.peak_catalog_cache = {}
        self._recover()

    def _recover(self):
        for path in self.runs.glob("*/state.json"):
            state = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for job in state.get("jobs", []):
                if job["status"] in ("queued", "running"):
                    job.update(
                        status="failed", error="服务重启中断了本次计算；可重新提交。", finished_at=now()
                    )
                    write(path.parent / "jobs" / job["id"] / "job.json", job)
                    changed = True
            if changed:
                write(path, state)

    def close(self):
        self.executor.shutdown(wait=True)

    def root(self, run_id):
        if (
            not isinstance(run_id, str)
            or not run_id
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id
            )
        ):
            raise ValueError("Invalid run ID")
        root = self.runs / run_id
        if not (root / "state.json").is_file():
            raise FileNotFoundError("试算不存在")
        return root

    def read(self, run_id):
        with self.lock:
            return json.loads((self.root(run_id) / "state.json").read_text(encoding="utf-8"))

    def save(self, state):
        with self.lock:
            write(self.root(state["id"]) / "state.json", state)

    def _saved_operation(self, state, label, paths, layer=None):
        files = [{"name": Path(path).name, "path": str(path).replace("\\", "/"),
                  "kind": Path(path).suffix.lstrip("."),
                  "url": f"/api/runs/{state['id']}/files/{quote(str(path).replace(chr(92), '/'))}"}
                 for path in paths]
        event = {"id": uuid.uuid4().hex[:12], "kind": "config", "label": label,
                 "layer": layer, "created_at": now(), "files": files}
        state.setdefault("operations", []).append(event)
        return files

    def _source_peaks(self, state, adapter=None):
        adapter = adapter or self.adapter_for(state)
        products = self.root(state["id"]) / "products"
        files = []
        for layer, folder, pattern in (("EC_L1", "parquet", "*_corr.l1.parquet"),
                                        ("EC_L2", "json", "*_fit_results.l2.json")):
            for directory in {layer_dir(products, state["payload"], layer),
                              adapter.historical_layer(state["payload"], layer)}:
                for path in (directory / folder).glob(pattern):
                    stat = path.stat()
                    files.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        key = (state["id"], state.get("config_revision"), tuple(sorted(files)))
        if key not in self.peak_catalog_cache:
            result = source_peak_catalog(adapter, state["payload"], products,
                                         profile=adapter.acquisition, config=state["config"])
            if len(self.peak_catalog_cache) >= 16:
                self.peak_catalog_cache.pop(next(iter(self.peak_catalog_cache)))
            self.peak_catalog_cache[key] = result
        return copy.deepcopy(self.peak_catalog_cache[key])

    def _ec_points(self, state, channel, adapter=None):
        adapter = adapter or self.adapter_for(state)
        points = ECInspector(adapter).points(state["payload"], self.root(state["id"]) / "products", channel)
        return draft_points(self._source_peaks(state, adapter), state["payload"], channel, points)

    def _all_ec_points(self, state, adapter=None):
        adapter = adapter or self.adapter_for(state)
        existing = ECInspector(adapter).points(state["payload"], self.root(state["id"]) / "products")
        sources = self._source_peaks(state, adapter)
        return [point for ch in range(4) for point in draft_points(sources, state["payload"], ch, existing)]

    def ec_peak_config(self, run_id):
        state = self.read(run_id)
        result = self._source_peaks(state)
        for source in result["spectra"]:
            for line in source["lines"]:
                line["point_ids"] = {str(ch): f"{state['payload']}:ec:{source['energy_key']}:{ch}:{line['energy_keV']:g}"
                                     for ch in range(4)}
        result.update(revision=state["config_revision"],
                      files=[file for operation in state.get("operations", [])
                             if operation.get("label") == "保存源峰配置" for file in operation["files"]][-4:])
        return clean(result)

    def save_ec_peak_config(self, run_id, body):
        key = body.get("energy_key")
        if not isinstance(key, str) or not key.startswith("src_"):
            raise ValueError("请选择放射源谱")
        updated = normalize_peak_sets({key: {"source_name": body.get("source_name") or key[4:],
                                            "lines": body.get("lines", [])}})[key]
        with self.lock:
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("请等待当前计算完成后再保存能线")
            source = next((item for item in self._source_peaks(state)["spectra"] if item["energy_key"] == key), None)
            if source is None:
                raise ValueError("放射源谱不存在")
            old_lines = {f"{line['energy_keV']:g}": line for line in source["lines"]}
            supported = photopeak_channels(_response_payload(state))
            changed_points, active_points = set(), set()
            for line in updated["lines"]:
                energy = f"{line['energy_keV']:g}"
                previous = old_lines.get(energy)
                for ch in supported:
                    identity = f"{state['payload']}:ec:{key}:{ch}:{energy}"
                    active_points.add(identity)
                    if previous is None or any(previous.get(field, {}).get(str(ch)) != line.get(field, {}).get(str(ch))
                                               for field in ("windows", "centers")):
                        changed_points.add(identity)
            points = {point["channel"]: point for point in self._all_ec_points(state)
                      if point["energy_key"] == key}
            for line in updated["lines"]:
                for ch, window in line["windows"].items():
                    width = float(points.get(int(ch), {}).get("bin_width") or 4.)
                    if window["hi"] - window["lo"] < 8 * width:
                        raise ValueError(f"CH {ch} 的窗口至少需要 8 个 bin（{8 * width:g} ADC）")
            state["config"].setdefault("ec_peak_sets", {})[key] = updated
            prefix = f"{state['payload']}:ec:{key}:"
            # Table edits replace the corresponding older per-point windows.
            state["config"]["ec_windows"] = {identity: window for identity, window in
                                               state["config"].get("ec_windows", {}).items()
                                               if not identity.startswith(prefix)}
            for line in updated["lines"]:
                for ch, window in line["windows"].items():
                    identity = f"{prefix}{ch}:{line['energy_keV']:g}"
                    state["config"]["ec_windows"][identity] = window
            state["pending_ec_windows"] = sorted({identity for identity in state.get("pending_ec_windows", [])
                                                   if not identity.startswith(prefix) or identity in active_points} | changed_points)
            state["config_revision"] += 1
            revision = state["config_revision"]
            relative = f"peak-config/{revision:04d}_{key}.json"
            record = {"revision": revision, "saved_at": now(), "energy_key": key, **updated}
            write(self.root(run_id) / relative, record)
            state["revisions"].append({"revision": revision, "saved_at": record["saved_at"],
                                       "reason": "保存源峰配置", "config": copy.deepcopy(state["config"])})
            files = self._saved_operation(state, "保存源峰配置", [relative], "EC_L2")
            dirty = set(state["dirty_layers"]) | {"EC_L3"}
            if changed_points:
                dirty.add("EC_L2")
            state["dirty_layers"] = sorted(dirty)
            state.setdefault("acquisition_products", {}).setdefault("EC_L2", [])
            retained = (set(state.setdefault("acquisition_ec_l2_points", {}).get(key, [])) & active_points) - changed_points
            state["acquisition_ec_l2_points"][key] = sorted(retained)
            if key in state["acquisition_products"]["EC_L2"] and not active_points <= retained:
                state["acquisition_products"]["EC_L2"].remove(key)
            state["updated_at"] = now()
            self.save(state)
        return {**self.ec_peak_config(run_id), "saved_files": files}

    def artifact_preview(self, run_id, path, *, job_id=None, source=None, layer=None):
        state = self.read(run_id)
        root = self.root(run_id)
        relative = Path(path)
        if relative.is_absolute():
            raise ValueError("请使用产物相对路径")
        if job_id:
            if not any(job["id"] == job_id for job in state["jobs"]):
                raise FileNotFoundError("任务不存在")
            base = root / "jobs" / job_id / "products"
            url = f"/api/runs/{run_id}/files/jobs/{job_id}/products/{quote(relative.as_posix())}"
        elif source in ("history", "historical"):
            if layer not in LAYERS:
                raise ValueError("请选择产物层级")
            base = self.adapter_for(state).historical_layer(state["payload"], layer)
            url = f"/api/runs/{run_id}/history/{layer}/{quote(relative.as_posix())}"
        else:
            base = root / "products" if (root / "products" / relative).is_file() else root
            prefix = "products/" if base == root / "products" else ""
            url = f"/api/runs/{run_id}/files/{prefix}{quote(relative.as_posix())}"
        target, boundary = (base / relative).resolve(), base.resolve()
        if not target.is_relative_to(boundary) or not target.is_file():
            raise FileNotFoundError("产物不存在")
        return clean(preview_artifact(target, relative=relative.as_posix(), url=url))

    def inventory(self, payload):
        if payload not in ("14B", "15B"):
            raise ValueError("payload 必须为 14B 或 15B")
        if payload not in self.inventory_cache:
            self.inventory_cache[payload] = self.adapter.inventory(payload)
        return copy.deepcopy(self.inventory_cache[payload])

    def adapter_for(self, state):
        if state.get("acquisition"):
            return ComputeAdapter(self.workspace, state.get("dataset"), load_profile(state["acquisition"]),
                                  response_payload=_response_payload(state))
        return ComputeAdapter(self.workspace, state["dataset"], response_payload=_response_payload(state)) if state.get("dataset") else self.adapter

    def acquisition_defaults(self, profile=None):
        return {"profile": prepare_profile(self.workspace, payload_profile=profile), "presets": presets()}

    def acquisition(self, run_id):
        state = self.read(run_id)
        reference = state.get("acquisition")
        profile = (
            load_profile(reference)
            if reference
            else prepare_profile(
                self.workspace,
                payload_profile=(state.get("dataset") or {}).get("profile") or state["payload"],
            )
        )
        profile = {key: value for key, value in profile.items() if not key.startswith("_")}
        return {
            "profile": profile,
            "revision": reference["revision"] if reference else 0,
            "sha256": reference["sha256"] if reference else profile_digest(profile),
            "presets": presets(),
            "acquisition_preset": state.get("acquisition_preset"),
        }

    def preview_acquisition(self, run_id, profile):
        state = self.read(run_id)
        prepared = prepare_profile(self.workspace, profile)
        adapter = ComputeAdapter(self.workspace, state.get("dataset"), prepared)
        raw_root = adapter._base_config(state["payload"])[1]
        result = adapter.observations(state["payload"])
        if not raw_root.is_dir():
            result["errors"] = []
        result["sha256"] = profile_digest(prepared)
        result["warnings"] = (
            [] if result["rows"] else ["当前未发现原始数据；可保存规则，计算前需载入匹配文件。"]
        )
        return result

    def save_acquisition(self, run_id, profile, reason=None, *, preset_reference=None):
        with self.lock:
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("计算运行中，无法修改读取配置")
            prepared = prepare_profile(self.workspace, profile)
            preview = self.preview_acquisition(run_id, prepared)
            if preview["errors"]:
                raise ValueError("读取配置校验失败: " + "; ".join(preview["errors"]))
            previous = state.get("acquisition")
            if previous and previous["sha256"] == preview["sha256"]:
                if preset_reference and state.get("acquisition_preset") != preset_reference:
                    state["acquisition_preset"] = copy.deepcopy(preset_reference)
                    self._record_preset_revision(state, reason)
                return self.view(run_id)
            inventory = ComputeAdapter(self.workspace, state.get("dataset"), prepared).inventory(
                state["payload"]
            )
            reference = freeze_profile(
                self.root(run_id), prepared, previous["revision"] + 1 if previous else 1
            )
            state.update(
                acquisition=reference,
                inventory=inventory,
                acquisition_products={},
                acquisition_l1_channels={},
                acquisition_ec_l2_points={},
                ec_l2_provenance={},
                tb_l1_failures={},
                acquisition_pending_layers=list(LAYERS),
                pending_windows=[],
                pending_ec_windows=[],
                pending_ec_l0=[],
                ec_dependency=None,
                ec_scope=None,
            )
            if preset_reference:
                state["acquisition_preset"] = copy.deepcopy(preset_reference)
            elif state.get("acquisition_preset"):
                state["acquisition_preset"]["modified"] = True
            state["dirty_layers"] = sorted(set(state["dirty_layers"]) | set(LAYERS))
            state["config_revision"] += 1
            revision = {
                "revision": state["config_revision"],
                "saved_at": now(),
                "reason": str(reason or "载荷读取配置修订")[:1000],
                "acquisition": reference,
                "acquisition_preset": state.get("acquisition_preset"),
                "config": copy.deepcopy(state["config"]),
            }
            state["revisions"].append(revision)
            state["updated_at"] = now()
            root = self.root(run_id)
            revision_path = f"revisions/{state['config_revision']:04d}.json"
            write(root / revision_path, revision)
            self._saved_operation(state, "保存读取配置", [revision_path,
                Path(reference["path"]).relative_to(root).as_posix(),
                *(Path(item["path"]).relative_to(root).as_posix() for item in reference["xml"])])
            self.save(state)
        return self.view(run_id)

    def _record_preset_revision(self, state, reason):
        state["config_revision"] += 1
        state["updated_at"] = now()
        revision = {
            "revision": state["config_revision"],
            "saved_at": now(),
            "reason": str(reason or "切换载荷读取预设版本")[:1000],
            "acquisition": state.get("acquisition"),
            "acquisition_preset": state.get("acquisition_preset"),
            "config": copy.deepcopy(state["config"]),
        }
        state["revisions"].append(revision)
        root = self.root(state["id"])
        revision_path = f"revisions/{state['config_revision']:04d}.json"
        write(root / revision_path, revision)
        reference = state.get("acquisition")
        paths = [revision_path]
        if reference:
            paths.extend([Path(reference["path"]).relative_to(root).as_posix(),
                          *(Path(item["path"]).relative_to(root).as_posix() for item in reference["xml"])])
        self._saved_operation(state, "切换读取预设", paths)
        self.save(state)

    def acquisition_presets(self, payload=None):
        with self.lock:
            return self.preset_registry.list(payload)

    def acquisition_preset(self, preset_id, version=None):
        with self.lock:
            return self.preset_registry.get(preset_id, version)

    def register_acquisition_preset(self, body):
        with self.lock:
            return self.preset_registry.save(
                **{
                    key: body.get(key)
                    for key in ("profile", "name", "payload", "preset_id", "reason", "bindings")
                }
            )

    def _run_preset_bindings(self, state):
        dataset = state.get("dataset")
        if not dataset:
            cfg = self.adapter_for(state)._config(state["payload"])
            dataset = {
                "raw_dir": str(cfg.raw_root) if cfg.raw_root.is_dir() else None,
                "product_dir": str(self.workspace / "products" / state["payload"]),
            }
        result = []
        for role, key in (("raw", "raw_dir"), ("product", "product_dir")):
            path = dataset.get(key)
            if path and Path(path).is_dir():
                result.append({"role": role, "path": path})
        return result

    def save_acquisition_preset(self, run_id, body):
        with self.lock, self._preset_transaction(run_id):
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("计算运行中，无法发布读取预设")
            profile = body["profile"] if "profile" in body else self.acquisition(run_id)["profile"]
            if not isinstance(profile, dict):
                raise ValueError("profile 必须为读取配置对象；省略时使用当前试算配置")
            prepared = prepare_profile(self.workspace, profile)
            preview = self.preview_acquisition(run_id, prepared)
            if preview["errors"]:
                raise ValueError("读取配置校验失败: " + "; ".join(preview["errors"]))
            # Resolve all inputs before publishing a directory association.
            ComputeAdapter(self.workspace, state.get("dataset"), prepared).inventory(state["payload"])
            bindings = self._run_preset_bindings(state) if body.get("bind_directories", True) else []
            selected = self.preset_registry.save(
                profile=prepared,
                name=body.get("name"),
                payload=body.get("payload") or (None if body.get("preset_id") else state["payload"]),
                preset_id=body.get("preset_id"),
                reason=body.get("reason"),
                bindings=bindings,
            )
            reference = {**selected["reference"], "origin": "published", "modified": False}
            run = self.save_acquisition(
                run_id, prepared, body.get("reason") or "保存载荷读取预设", preset_reference=reference
            )
            self._bind_run_products(run, reference)
            return {"preset": selected["preset"], "reference": reference, "run": run}

    def apply_acquisition_preset(self, run_id, preset_id, version=None, reason=None):
        with self.lock, self._preset_transaction(run_id):
            selected = self.preset_registry.get(preset_id, version)
            reference = {**selected["reference"], "origin": "manual", "modified": False}
            run = self.save_acquisition(
                run_id, selected["profile"], reason or "切换载荷读取预设版本", preset_reference=reference
            )
            self._bind_run_products(run, reference)
            return run

    @contextlib.contextmanager
    def _preset_transaction(self, run_id):
        """Keep registry associations and the run synchronized on failed saves."""
        root = self.root(run_id)
        state_path = root / "state.json"
        registry_path = self.preset_registry.path
        original_state = state_path.read_bytes()
        original_registry = registry_path.read_bytes() if registry_path.exists() else None
        run_snapshots = set((root / "acquisition").glob("*"))
        registry_snapshots = set(self.preset_registry.root.glob("*/acquisition/*"))
        revisions = set((root / "revisions").glob("*.json"))
        try:
            yield
        except Exception:
            for path, content in ((registry_path, original_registry), (state_path, original_state)):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    temporary = path.with_suffix(path.suffix + ".rollback")
                    temporary.write_bytes(content)
                    temporary.replace(path)
            # Delete only snapshots created by this failed operation; all
            # published/previous run snapshots retain their original bytes.
            for base, candidates in (
                (root / "acquisition", set((root / "acquisition").glob("*")) - run_snapshots),
                (
                    self.preset_registry.root,
                    set(self.preset_registry.root.glob("*/acquisition/*")) - registry_snapshots,
                ),
            ):
                base = base.resolve()
                for path in candidates:
                    target = path.resolve()
                    if (
                        target.is_relative_to(base)
                        and target != base
                        and path.is_dir()
                        and not path.is_symlink()
                    ):
                        shutil.rmtree(target)
            for path in set((root / "revisions").glob("*.json")) - revisions:
                if path.resolve().parent == (root / "revisions").resolve():
                    path.unlink()
            raise

    def _bind_run_products(self, state, reference):
        product = self.root(state["id"]) / "products" / state["payload"]
        product.mkdir(parents=True, exist_ok=True)
        self.preset_registry.bind(
            reference["id"],
            reference["version"],
            [{"role": "product", "path": str(product)}],
            "固定试算产品的读取预设来源",
        )

    def _annotate_datasets(self, catalog):
        for dataset in catalog["datasets"]:
            dataset["acquisition_preset_match"] = self.preset_registry.match(dataset)
        return catalog

    def acquisition_coverage(self, state, layer):
        if not state.get("acquisition"):
            return None
        rows = (state["inventory"].get("raw", {}).get("reading_preview") or {}).get("rows", [])
        if layer.startswith("TB_"):
            expected = {row["stem"] for row in rows if row["kind"] == "tb"}
        else:
            expected = {row["stem"] for row in rows if row["kind"] != "tb"}
        if layer == "EC_L2":
            expected = {
                f"{row['energy_keV']:g}keV" if row["kind"] == "xray" else f"src_{row['source']}"
                for row in rows
                if row["kind"] in ("xray", "source")
            }
            expected = {key for key in expected if state.get("config", {}).get("ec_peak_sets", {}).get(key, {}).get("lines") != []}
        if layer in ("TB_L2", "EC_L3"):
            expected = {"aggregate"}
        completed = set(state.get("acquisition_products", {}).get(layer, [])) & expected
        return {
            "completed": len(completed),
            "required": len(expected),
            "missing": sorted(expected - completed),
            "current": bool(expected) and completed == expected,
            "sha256": state["acquisition"]["sha256"],
        }

    def scan_datasets(self, **paths):
        with self.lock:
            return self._annotate_datasets(self.catalog.scan(**paths))

    def directories(self, path=None):
        folder = Path(path or self.workspace).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("目录不存在")
        children = []
        for child in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    children.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
        return {"path": str(folder), "parent": str(folder.parent), "directories": children}

    def bootstrap(self):
        with self.lock:
            return self._bootstrap()

    def _bootstrap(self):
        runs = []
        with self.lock:
            for path in sorted(self.runs.glob("*/state.json"), reverse=True):
                state = json.loads(path.read_text(encoding="utf-8"))
                runs.append({
                    **{k: state.get(k) for k in ("id", "payload", "created_at", "name")},
                    "dataset_id": (state.get("dataset") or {}).get("id"),
                })
        catalog = self.catalog.saved() or self.catalog.scan()
        self._annotate_datasets(catalog)
        return {
            **catalog,
            "payloads": list(dict.fromkeys(item["payload"] for item in catalog["datasets"])),
            "runs": runs,
            "active_run": runs[0]["id"] if runs else None,
            "workspace": str(self.workspace),
            "acquisition_presets": self.acquisition_presets()["presets"],
        }

    def create(self, payload=None, name=None, dataset_id=None, preset_id=None, preset_version=None):
        with self.lock:
            return self._create(payload, name, dataset_id, preset_id, preset_version)

    def _create(self, payload=None, name=None, dataset_id=None, preset_id=None, preset_version=None):
        if preset_version is not None and not preset_id:
            raise ValueError("指定预设版本时必须同时选择预设")
        dataset = None
        if dataset_id:
            self.catalog.scan()
            saved = json.loads(self.catalog.path.read_text(encoding="utf-8"))
            definition = next((item for item in saved["datasets"] if item["id"] == dataset_id), None)
            if not definition:
                raise ValueError("数据集不存在，请重新扫描目录")
            dataset = self.catalog.resolve({**definition, "payload": payload or definition["payload"]})
            payload = dataset["payload"]
            adapter = ComputeAdapter(self.workspace, dataset)
            inventory = adapter.inventory(payload)
        else:
            inventory = self.inventory(payload)
            adapter = self.adapter
        payload = validate_payload(payload)
        match_dataset = dataset or {
            "payload": payload,
            "source_payload": payload,
            "raw_dir": str(adapter._config(payload).raw_root),
            "product_dir": str(self.workspace / "products" / payload),
        }
        match = self.preset_registry.match(match_dataset)
        if not preset_id and match["status"] == "conflict":
            raise ValueError(match["warnings"][0])
        selected_preset = None
        if preset_id:
            selected_preset = self.preset_registry.get(preset_id, preset_version)
            preset_origin = "manual"
        elif match["reference"]:
            selected_preset = self.preset_registry.get(
                match["reference"]["id"], match["reference"]["version"]
            )
            preset_origin = match["reference"]["origin"]
        if selected_preset:
            adapter = ComputeAdapter(self.workspace, dataset, selected_preset["profile"])
            inventory = adapter.inventory(payload)
        required_channels = photopeak_channels(detector_identity(
            payload, dataset, selected_preset["reference"] if selected_preset else None))
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
        root = self.runs / run_id
        root.mkdir()
        imported = []
        # Preserve a small immutable JSON baseline; no history file is ever opened for writing.
        for layer in LAYERS:
            history = adapter.historical_layer(payload, layer)
            for src in history.glob("json/*.json"):
                rel = (
                    Path(payload)
                    / layer.replace("_", "/", 1).replace("TB/", "tb/").replace("EC/", "ec/")
                    / "json"
                    / src.name
                )
                for target in (root / "baseline" / rel, root / "products" / rel):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                imported.append(
                    {
                        "path": rel.as_posix(),
                        "source_path": str(src),
                        "sha256": digest(src),
                        "bytes": src.stat().st_size,
                    }
                )
        tb_files = sorted((root / "baseline" / payload / "tb/L2/json").glob("*_TB*.json"))
        tb = None
        import_warnings = []
        for candidate in reversed(tb_files):
            try:
                tb_package(candidate, required_channels=required_channels)
                tb = candidate
                break
            except (ValueError, TypeError, KeyError, OSError) as exc:
                import_warnings.append(f"未采用无效 TB 参数包 {candidate.name}: {exc}")
        state = {
            "id": run_id,
            "payload": payload,
            "name": str(name or f"{payload} 交互试算")[:120],
            "created_at": now(),
            "updated_at": now(),
            "config_revision": 0,
            "config": {"windows": {}, "excluded": [], "ec_windows": {}, "ec_excluded": []},
            "revisions": [],
            "jobs": [],
            "dirty_layers": [],
            "pending_windows": [],
            "pending_ec_windows": [],
            "pending_ec_l0": [],
            "generated": {},
            "acquisition": None,
            "acquisition_preset": None,
            "acquisition_pending_layers": [],
            "acquisition_products": {},
            "inventory": inventory,
            "import_warnings": import_warnings,
            "tb_version": {
                "filename": tb.name,
                "sha256": digest(tb),
                "path": str(tb),
                "origin": "historical",
            }
            if tb
            else None,
            "tb_binding": "pinned" if dataset else "follow_run",
            "dataset": dataset,
            "ec_dependency": None,
            "ec_scope": None,
            "ec_l2_provenance": {},
        }
        write(root / "imports.json", imported)
        state["selected_tb"] = snapshot(tb_package(tb, required_channels=required_channels), root) if tb else None
        if dataset and state["selected_tb"] and self._imported_ec_matches(state, adapter):
            state["ec_dependency"] = {**state["selected_tb"], "input_origin": "imported_metadata_verified"}
            state["ec_scope"] = "all"
        write(root / "state.json", state)
        if selected_preset:
            reference = {**selected_preset["reference"], "origin": preset_origin, "modified": False}
            state.update(
                acquisition=freeze_profile(root, selected_preset["profile"], 1),
                acquisition_preset=reference,
                acquisition_pending_layers=list(LAYERS),
                dirty_layers=list(LAYERS),
                ec_dependency=None,
                ec_scope=None,
            )
            self.save(state)
            self._bind_run_products(state, reference)
        return self.view(run_id)

    def _imported_ec_matches(self, state, adapter):
        from ..common.utils import read_parquet_metadata
        from ..ec.processors.L1_processor import ECL1Processor

        selected = self.selected_tb(state)
        if not selected:
            return False
        files = list(adapter.historical_layer(state["payload"], "EC_L1").glob("parquet/*_corr.l1.parquet"))
        if not files:
            return False
        expected = ECL1Processor(state["payload"], self.runs, response_payload=_response_payload(state))._load_tb_params(Path(selected["path"]))
        for path in files:
            actual = read_parquet_metadata(path, "tb_correction").get("tb_params_used", {})
            if any(actual.get(str(ch)) != params for ch, params in expected.items()):
                return False
        return True

    def patch(self, run_id, patch):
        with self.lock:
            state = self.read(run_id)
            if any(j["status"] in ("queued", "running") for j in state["jobs"]):
                raise ValueError("计算进行中，请等待完成后再保存修订")
            unknown = set(patch) - {"windows", "excluded", "ec_windows", "ec_excluded", "reason", "name"}
            if unknown:
                raise ValueError(f"不支持的配置字段: {sorted(unknown)}")
            adapter = self.adapter_for(state)
            points = {p["id"] for p in adapter.points(state["payload"], self.root(run_id) / "products")}
            stems = {p.split(":")[1] for p in points}
            changed = set()
            tb_ec_dependents = () if state.get("tb_binding") == "pinned" else ("EC_L1", "EC_L2", "EC_L3")
            if "windows" in patch:
                from .acquisition import tb_histogram_config

                histogram = tb_histogram_config(adapter.acquisition)
                windows = patch["windows"]
                if not isinstance(windows, dict):
                    raise ValueError("windows 必须为对象")
                for stem, channels in windows.items():
                    if stem not in stems or not isinstance(channels, dict):
                        raise ValueError("工况不存在")
                    for ch, bounds in channels.items():
                        if str(ch) not in ("0", "1", "2", "3") or not isinstance(bounds, dict):
                            raise ValueError("通道不存在")
                        lo, hi = float(bounds["lo"]), float(bounds["hi"])
                        if not (
                            math.isfinite(lo)
                            and math.isfinite(hi)
                            and histogram["min"] <= lo < hi <= histogram["max"]
                            and hi - lo >= 8 * histogram["bin_width"]
                        ):
                            raise ValueError(
                                f"窗口须满足 {histogram['min']} ≤ lo < hi ≤ {histogram['max']}，至少 8 个 bin（{8 * histogram['bin_width']:g} ADC）"
                            )
                        new = {"lo": lo, "hi": hi}
                        old = state["config"]["windows"].setdefault(stem, {}).get(str(ch))
                        if old != new:
                            state["config"]["windows"][stem][str(ch)] = new
                            point_id = f"{state['payload']}:{stem}:{ch}"
                            state["pending_windows"] = sorted(
                                set(state.get("pending_windows", [])) | {point_id}
                            )
                            changed.update(("TB_L1", "TB_L2", *tb_ec_dependents))
            if "excluded" in patch:
                excluded = patch["excluded"]
                if not isinstance(excluded, list) or any(p not in points for p in excluded):
                    raise ValueError("屏蔽点 ID 不存在")
                excluded = sorted(set(excluded))
                if excluded != state["config"]["excluded"]:
                    state["config"]["excluded"] = excluded
                    changed.update(("TB_L2", *tb_ec_dependents))
            if "name" in patch:
                state["name"] = str(patch["name"])[:120]
            if "ec_windows" in patch or "ec_excluded" in patch:
                known = {point["id"]: point for point in self._all_ec_points(state, adapter)}
                windows = patch.get("ec_windows", {})
                if not isinstance(windows, dict):
                    raise ValueError("ec_windows 必须为对象")
                for point_id, bounds in windows.items():
                    if point_id not in known or not isinstance(bounds, dict):
                        raise ValueError("EC 能峰不存在")
                    lo, hi = float(bounds["lo"]), float(bounds["hi"])
                    minimum = 8 * float(known[point_id].get("bin_width", 4.))
                    if not (
                        math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi <= 65536 and hi - lo >= minimum
                    ):
                        raise ValueError(f"EC 窗口须满足 0 ≤ lo < hi ≤ 65536，至少 8 个 bin（{minimum:g} ADC）")
                    new = {"lo": lo, "hi": hi}
                    if state["config"].setdefault("ec_windows", {}).get(point_id) != new:
                        state["config"]["ec_windows"][point_id] = new
                        state["pending_ec_windows"] = sorted(
                            set(state.get("pending_ec_windows", [])) | {point_id}
                        )
                        changed.update(("EC_L2", "EC_L3"))
                if "ec_excluded" in patch:
                    excluded = patch["ec_excluded"]
                    if not isinstance(excluded, list) or any(point not in known for point in excluded):
                        raise ValueError("EC 屏蔽点不存在")
                    excluded = sorted(set(excluded))
                    if excluded != state["config"].get("ec_excluded", []):
                        state["config"]["ec_excluded"] = excluded
                        changed.add("EC_L3")
            state["config_revision"] += 1
            revision = {
                "revision": state["config_revision"],
                "saved_at": now(),
                "reason": str(patch.get("reason", "交互修订"))[:1000],
                "config": copy.deepcopy(state["config"]),
            }
            state["revisions"].append(revision)
            write(self.root(run_id) / "revisions" / f"{state['config_revision']:04d}.json", revision)
            saved_files = self._saved_operation(
                state, "保存窗口配置" if "windows" in patch or "ec_windows" in patch else "保存修订配置",
                [f"revisions/{state['config_revision']:04d}.json"],
                "EC_L2" if "ec_windows" in patch or "ec_excluded" in patch else "TB_L1",
            )
            state["dirty_layers"] = sorted(set(state["dirty_layers"]) | changed)
            state["updated_at"] = now()
            self.save(state)
        return {**self.view(run_id, compact=True), "saved_files": saved_files}

    def stale(self, state):
        ec = state.get("ec_dependency")
        selected = self.selected_tb(state)
        return (
            state.get("tb_binding", "follow_run") == "follow_run" and "TB_L2" in state["dirty_layers"]
        ) or bool(ec and (not selected or ec["sha256"] != selected["sha256"]))

    @staticmethod
    def selected_tb(state):
        return state.get("selected_tb") if state.get("tb_binding") == "pinned" else state.get("tb_version")

    @staticmethod
    def verify_tb(package):
        if not package or not Path(package["path"]).is_file():
            raise ValueError("所选 TB 参数包快照不存在，请重新选择有效版本")
        if digest(Path(package["path"])) != package["sha256"]:
            raise ValueError("所选 TB 参数包快照 SHA-256 已变化，已拒绝使用；请重新选择有效版本")

    def tb_versions(self, run_id):
        state = self.read(run_id)
        candidates = [
            (path, "historical")
            for path in self.adapter_for(state)
            .historical_layer(state["payload"], "TB_L2")
            .glob("json/*_TB*.json")
        ]
        # Completed attempts are temporal versions, including attempts from other saved runs of this dataset.
        for state_path in self.runs.glob("*/state.json"):
            other = json.loads(state_path.read_text(encoding="utf-8"))
            source_payload = (state.get("dataset") or {}).get("source_payload", state["payload"])
            other_source = (other.get("dataset") or {}).get("source_payload", other["payload"])
            if source_payload != other_source:
                continue
            if (
                state.get("dataset")
                and other.get("dataset")
                and state["dataset"].get("raw_dir") != other["dataset"].get("raw_dir")
            ):
                continue
            for job in other.get("jobs", []):
                if job["layer"] == "TB_L2" and job["status"] == "succeeded":
                    candidates.extend(
                        (state_path.parent / "jobs" / job["id"] / "products" / artifact["path"], "computed")
                        for artifact in job.get("outputs", [])
                        if artifact["path"].endswith("_TB.l2.json")
                    )
        selected = self.selected_tb(state)
        if selected:
            candidates.append((Path(selected["path"]), selected.get("origin", "selected")))
        versions = {}
        for metadata in (self.root(run_id) / "dependencies").glob("*/_package.json"):
            package = json.loads(metadata.read_text(encoding="utf-8"))
            try:
                if digest(Path(package["path"])) != package["sha256"]:
                    continue
                package["selected"] = bool(selected and package["id"] == selected.get("id"))
                versions[package["id"]] = package
            except OSError:
                continue
        for path, origin in candidates:
            try:
                package = tb_package(path, origin, required_channels=photopeak_channels(_response_payload(state)))
                if selected and digest(path) == selected["sha256"] and str(path) == selected["path"]:
                    package = {**package, **selected}
                package["selected"] = bool(selected and package["id"] == selected.get("id"))
                if selected and "id" not in selected:
                    package["selected"] = (
                        package["sha256"] == selected["sha256"] and str(path) == selected["path"]
                    )
                versions[package["id"]] = package
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return {
            "versions": sorted(versions.values(), key=lambda item: item["modified_at"], reverse=True),
            "binding": state.get("tb_binding", "follow_run"),
        }

    def select_tb(self, run_id, version_id=None, path=None):
        with self.lock:
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("计算进行中，请等待完成后再切换 TB 依赖")
            package = (
                tb_package(Path(path), "external", required_channels=photopeak_channels(_response_payload(state)))
                if path
                else next(
                    (item for item in self.tb_versions(run_id)["versions"] if item["id"] == version_id), None
                )
            )
            if not package:
                raise ValueError("TB 版本不存在，请刷新或导入 JSON 包")
            previous = self.selected_tb(state)
            if previous and previous.get("id"):
                snapshot(previous, self.root(run_id))
            state["selected_tb"] = snapshot(package, self.root(run_id))
            state["tb_binding"] = "pinned"
            if not previous or previous["sha256"] != package["sha256"]:
                state["dirty_layers"] = sorted(set(state["dirty_layers"]) | {"EC_L1", "EC_L2", "EC_L3"})
            state["config_revision"] += 1
            revision = {
                "revision": state["config_revision"],
                "saved_at": now(),
                "reason": "选择 EC 的 TB L2 依赖",
                "selected_tb": state["selected_tb"],
                "config": state["config"],
            }
            state["revisions"].append(revision)
            root = self.root(run_id)
            revision_path = f"revisions/{state['config_revision']:04d}.json"
            write(root / revision_path, revision)
            frozen_path = Path(state["selected_tb"]["path"])
            self._saved_operation(state, "选择 TB 依赖", [revision_path,
                frozen_path.relative_to(root).as_posix(),
                (frozen_path.parent / "_package.json").relative_to(root).as_posix()], "EC_L1")
            state["updated_at"] = now()
            self.save(state)
        return self.view(run_id)

    def ec_coverage(self, state):
        root = self.root(state["id"]) / "products"
        sources = []
        for path in layer_dir(root, state["payload"], "EC_L2").glob("json/*_fit_results.l2.json"):
            rel = path.relative_to(root).as_posix()
            provenance = state.get("ec_l2_provenance", {}).get(rel)
            selected = self.selected_tb(state)
            fresh = bool(provenance and selected and provenance["tb_version"]["sha256"] == selected["sha256"])
            if fresh and "point_versions" in provenance:
                records = json.loads(path.read_text(encoding="utf-8"))
                energy = path.name.removesuffix("_fit_results.l2.json")
                expected = self._expected_ec_points(state, energy, records)
                fresh = bool(expected) and all(
                    provenance["point_versions"].get(point, {}).get("sha256") == selected["sha256"]
                    for point in expected
                )
            sources.append(
                {
                    "file": rel,
                    "sha256": digest(path),
                    "current_tb": fresh,
                    "origin": "computed" if provenance else "historical",
                    "provenance": provenance,
                }
            )
        fresh_count = sum(s["current_tb"] for s in sources)
        mixed = fresh_count < len(sources)
        return {
            "mixed": mixed,
            "computed_files": fresh_count,
            "total": len(sources),
            "sources": sources,
            "description": f"{fresh_count}/{len(sources)} 份能峰文件绑定当前 TB；"
            + ("其余沿用历史或旧版本，L3 使用混合输入。" if mixed else "全部绑定当前 TB。"),
        }

    @staticmethod
    def _expected_ec_points(state, energy, records):
        """The configured input defines coverage, including points not fitted yet."""
        supported = photopeak_channels(_response_payload(state))
        peak_set = state.get("config", {}).get("ec_peak_sets", {}).get(energy)
        if peak_set is not None:
            return {f"{state['payload']}:ec:{energy}:{ch}:{line['energy_keV']:g}"
                    for line in peak_set["lines"] for ch in supported}
        rows = (state["inventory"].get("raw", {}).get("reading_preview") or {}).get("rows", [])
        expected = set()
        for row in rows:
            kind = row.get("kind")
            if kind not in ("xray", "source"):
                continue
            key = f"{row['energy_keV']:g}keV" if kind == "xray" else f"src_{row['source']}"
            if key != energy:
                continue
            energies = [row["energy_keV"]] if kind == "xray" else row.get("peak_energies_keV", [])
            channels = [row["channel"]] if kind == "xray" and row.get("channel") is not None else supported
            expected.update(f"{state['payload']}:ec:{energy}:{ch}:{float(value):g}"
                            for ch in channels if ch in supported for value in energies)
        if not expected:
            # Imported products can predate reading previews. Each recorded
            # energy line still requires the detector's supported channels.
            expected = {f"{state['payload']}:ec:{energy}:{ch}:{float(row['E']):g}"
                        for row in records if not row.get("placeholder") for ch in supported}
        return expected

    def submit(self, run_id, spec):
        with self.lock:
            state = self.read(run_id)
            layer = spec.get("layer")
            if layer not in LAYERS:
                raise ValueError("未知处理层")
            unavailable_reason = self.adapter_for(state).unavailable_reason(layer)
            if unavailable_reason:
                raise ValueError(unavailable_reason)
            if any(j["status"] in ("queued", "running") for j in state["jobs"]):
                raise ValueError("此试算已有计算任务")
            required_layer = {"TB_L2": "TB_L1", "EC_L3": "EC_L2"}.get(layer)
            if required_layer in state.get("acquisition_pending_layers", []):
                coverage = self.acquisition_coverage(state, required_layer)
                if layer != "TB_L2" or self._unreviewed_tb_l1_inputs(state):
                    raise ValueError(
                        f"读取配置尚未覆盖全部 {required_layer} 输入（{coverage['completed']}/{coverage['required']}），请先完成上游重算"
                    )
            if layer == "TB_L2" and "TB_L1" in state["dirty_layers"]:
                unresolved = set(state.get("pending_windows", [])) - set(state["config"].get("excluded", []))
                unresolved = {point for point in unresolved
                              if not unsupported_spectral_model(_response_payload(state), int(point.rsplit(":", 1)[1]))}
                unresolved.difference_update(state.get("tb_l1_failures", {}))
                if unresolved or not (state.get("tb_l1_failures") or state["config"].get("excluded")):
                    raise ValueError("窗口修订尚未拟合，请先执行 TB L1")
            if (
                layer in ("EC_L1", "EC_L2", "EC_L3")
                and state.get("tb_binding", "follow_run") == "follow_run"
                and "TB_L2" in state["dirty_layers"]
            ):
                raise ValueError("TB 依赖已变化，请先重拟合 TB L2，再运行 EC L1")
            selected = self.selected_tb(state)
            if layer in ("EC_L1", "EC_L2") and not selected:
                raise ValueError("请先选择已有 TB L2 JSON 校正包，或计算 TB L2 后选择其结果")
            if layer in ("EC_L1", "EC_L2"):
                self.verify_tb(selected)
            if layer in ("EC_L2", "EC_L3") and self.stale(state):
                raise ValueError("EC 使用的 TB 版本已过期，请重新运行 EC L1")
            if layer == "EC_L2" and state.get("ec_dependency") is None and not (spec.get("point_id") or spec.get("energy_key")):
                raise ValueError("请先运行 EC L1，固定本次 TB 版本")
            if layer == "EC_L3" and "EC_L2" in state["dirty_layers"]:
                raise ValueError("请先运行 EC L2 更新能峰拟合")
            config = copy.deepcopy(state["config"])
            config.update(peak_job_settings(layer, spec))
            config["representative"] = bool(spec.get("representative", True))
            if (
                layer == "EC_L2"
                and not spec.get("point_id")
                and not spec.get("energy_key")
                and not config["representative"]
                and state.get("ec_scope") != "all"
            ):
                raise ValueError("全量 EC L2 需要先执行全量 EC L1；当前仅有代表样例校正")
            if spec.get("energy_key"):
                if layer != "EC_L2" or spec["energy_key"] not in {item["energy_key"] for item in self._source_peaks(state)["spectra"]}:
                    raise ValueError("放射源谱不存在")
                config["energy_key"] = spec["energy_key"]
            if spec.get("stem"):
                adapter = self.adapter_for(state)
                if layer.startswith("TB_"):
                    known = set(adapter.tb_files(state["payload"], self.root(run_id) / "products"))
                    known.update(
                        path.name.removesuffix("_fit_results.l1.json")
                        for path in (
                            layer_dir(self.root(run_id) / "products", state["payload"], "TB_L1") / "json"
                        ).glob("*_fit_results.l1.json")
                    )
                    known.update(
                        path.name.removesuffix(".l0.parquet")
                        for directory in (
                            layer_dir(self.root(run_id) / "products", state["payload"], "TB_L0"),
                            adapter.historical_layer(state["payload"], "TB_L0"),
                        )
                        for path in directory.glob("parquet/*.l0.parquet")
                    )
                    known.update(
                        sample["stem"]
                        for sample in adapter.inventory(state["payload"]).get("raw", {}).get("tb_samples", [])
                    )
                else:
                    input_layer = {"EC_L1": "EC_L0", "EC_L2": "EC_L1"}.get(layer, layer)
                    known = {
                        path.name.removesuffix("_corr.l1.parquet").removesuffix(".l0.parquet")
                        for directory in (
                            layer_dir(self.root(run_id) / "products", state["payload"], input_layer),
                            adapter.historical_layer(state["payload"], input_layer),
                        )
                        for path in directory.glob("parquet/*.parquet")
                    }
                    if layer == "EC_L0":
                        inventory = adapter.inventory(state["payload"])
                        raw = inventory.get("raw", {})
                        known.update(sample["stem"] for sample in raw.get("ec_samples", []))
                        known.update(
                            row["stem"]
                            for row in (raw.get("reading_preview") or {}).get("rows", [])
                            if row["kind"] != "tb"
                        )
                if spec["stem"] not in known:
                    raise ValueError("未知工况")
                config["only"] = [spec["stem"]]
            if spec.get("point_id"):
                if layer != "EC_L2":
                    raise ValueError("能峰局部重拟合只支持 EC L2")
                channel = int(spec.get("channel", 0))
                self._channel(channel)
                point = next((point for point in self._ec_points(state, channel) if point["id"] == spec["point_id"]), None)
                if point is None:
                    raise ValueError("EC 能峰不存在")
                config["ec_point_id"] = spec["point_id"]
                if "bin_width" not in config and point.get("bin_width") is not None:
                    config.update(peak_job_settings(layer, {"bin_width": point["bin_width"]}))
            if layer == "EC_L2" and state.get("pending_ec_l0"):
                pending = state["pending_ec_l0"]
                if config.get("ec_point_id") or config.get("energy_key"):
                    energy = config["ec_point_id"].split(":")[2] if config.get("ec_point_id") else config["energy_key"]
                    affected = [
                        stem
                        for stem in pending
                        if stem.startswith(energy + "_")
                        or (energy.startswith("src_") and stem.startswith("src_bkg_"))
                    ]
                elif config.get("only"):
                    affected = sorted(set(pending) & set(config["only"]))
                    if any(
                        stem.startswith("src_") and not stem.startswith("src_bkg_") for stem in config["only"]
                    ):
                        affected = sorted(
                            set(affected) | {stem for stem in pending if stem.startswith("src_bkg_")}
                        )
                elif config["representative"]:
                    scope = self.adapter_for(state).inventory(state["payload"])["representative"]["ec_stems"]
                    affected = sorted(set(pending) & set(scope))
                else:
                    affected = pending
                if affected:
                    raise ValueError("这些 EC L0 文件已更新，请先执行对应 EC L1 校正: " + ", ".join(affected))
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "layer": layer,
                "status": "queued",
                "created_at": now(),
                "config_revision": state["config_revision"],
                "config": config,
                "scope": "selected"
                if config.get("only") or config.get("ec_point_id") or config.get("energy_key")
                else "representative"
                if config["representative"]
                else "all",
                "tb_version": copy.deepcopy(selected if layer.startswith("EC_") else state["tb_version"]),
                "acquisition": copy.deepcopy(state.get("acquisition")),
                "acquisition_preset": copy.deepcopy(state.get("acquisition_preset")),
                "error": None,
            }
            if layer == "EC_L3":
                job["ec_coverage"] = self.ec_coverage(state)
            state["jobs"].append(job)
            state["updated_at"] = now()
            self.save(state)
            write(self.root(run_id) / "jobs" / job_id / "job.json", job)
            self.executor.submit(self._execute, run_id, job_id)
            return copy.deepcopy(job)

    def _unreviewed_tb_l1_inputs(self, state):
        """Unavailable fits are omitted; untouched reading inputs still wait."""
        excluded = set(state["config"].get("excluded", [])) | set(state.get("tb_l1_failures", {}))
        completed = set(state.get("acquisition_products", {}).get("TB_L1", []))
        coverage = state.get("acquisition_l1_channels", {})
        rows = (state.get("inventory", {}).get("raw", {}).get("reading_preview") or {}).get("rows", [])
        return {
            f"{state['payload']}:{row['stem']}:{channel}"
            for row in rows if row["kind"] == "tb"
            for channel in range(4)
            if not unsupported_spectral_model(_response_payload(state), channel)
            if channel not in coverage.get(row["stem"], [0, 1, 2, 3] if row["stem"] in completed else [])
            and f"{state['payload']}:{row['stem']}:{channel}" not in excluded
        }

    def _record_tb_l1_attempt(self, state, job):
        """Keep failed old fits inspectable, but prevent their reuse in L2."""
        result = job.get("result", {})
        processor = result.get("processor", {})
        requested_channels = [ch for ch in job["config"].get("fit_channels", [0, 1, 2, 3])
                              if not unsupported_spectral_model(_response_payload(state), ch)]
        stems = job["config"].get("only") or result.get("scope", {}).get("only")
        if stems is None:
            if job.get("scope") == "representative":
                stems = state["inventory"]["representative"]["tb_stems"]
            else:
                stems = set(self.adapter_for(state).tb_files(state["payload"], self.root(state["id"]) / "products"))
                preview = state.get("inventory", {}).get("raw", {}).get("reading_preview") or {}
                stems.update(row["stem"] for row in preview.get("rows", []) if row["kind"] == "tb")
        succeeded = {
            f"{state['payload']}:{item['stem']}:{channel}"
            for item in processor.get("outputs", [])
            for channel in item.get("channels_updated", [])
        } if job["status"] in ("succeeded", "partial") else set()
        failures = {}
        for error in result.get("errors", []):
            if isinstance(error, dict) and (error.get("stem") or error.get("file")):
                stem = error.get("stem") or Path(error["file"]).stem.removesuffix(".l0")
                channels = requested_channels if error.get("channel") is None else [error["channel"]]
                for channel in channels:
                    failures[f"{state['payload']}:{stem}:{channel}"] = error.get("error", "Fit failed")
        if job["status"] == "failed":
            for stem in stems or []:
                for channel in requested_channels:
                    point = f"{state['payload']}:{stem}:{channel}"
                    failures.setdefault(point, job.get("error") or "Fit failed")
        failures = {point: error for point, error in failures.items() if point not in succeeded}
        latest = state.setdefault("tb_l1_failures", {})
        for point in list(latest):
            if unsupported_spectral_model(_response_payload(state), int(point.rsplit(":", 1)[1])):
                latest.pop(point)
        for point in succeeded:
            latest.pop(point, None)
        latest.update({point: {"job_id": job["id"], "error": error} for point, error in failures.items()})
        if failures:
            state["dirty_layers"] = sorted(set(state["dirty_layers"]) | {"TB_L1", "TB_L2"})
        if job.get("acquisition"):
            channels = state.setdefault("acquisition_l1_channels", {})
            completed = set(state.setdefault("acquisition_products", {}).get("TB_L1", []))
            for point in failures:
                _, stem, channel = point.split(":")
                previous = set(channels.get(stem, [0, 1, 2, 3] if stem in completed else []))
                previous.discard(int(channel))
                channels[stem] = sorted(previous)
                completed.discard(stem)
            state["acquisition_products"]["TB_L1"] = sorted(completed)
            if failures:
                state["acquisition_pending_layers"] = sorted(set(state.get("acquisition_pending_layers", [])) | {"TB_L1"})
        by_stem = {}
        for point, error in failures.items():
            _, stem, channel = point.split(":")
            by_stem.setdefault(stem, {})[int(channel)] = error
        invalidated = []
        for stem, errors in by_stem.items():
            filename = f"{stem}_fit_results.l1.json"
            path = layer_dir(self.root(state["id"]) / "products", state["payload"], "TB_L1") / "json" / filename
            if not path.is_file():
                continue
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                continue
            changed = [record for record in records if isinstance(record, dict) and record.get("channel") in errors]
            if not changed:
                continue
            job_root = self.root(state["id"]) / "jobs" / job["id"]
            write(job_root / "previous-results" / filename, records)
            for record in changed:
                if (record.get("fit_quality") or {}).get("status") != "fit_failed":
                    record["previous_fit_quality"] = record.get("fit_quality")
                record["fit_quality"] = {**(record.get("fit_quality") or {}), "accepted": False, "status": "fit_failed"}
                record["fit_failure"] = {"job_id": job["id"], "error": errors[record["channel"]]}
                invalidated.append(f"{state['payload']}:{stem}:{record['channel']}")
            write(path, records)
            relative = path.relative_to(self.root(state["id"]) / "products").as_posix()
            write(job_root / "products" / relative, records)
            artifacts = {item["path"]: item for item in job.get("outputs", [])}
            artifacts[relative] = {"path": relative, "sha256": digest(path), "job_id": job["id"]}
            job["outputs"] = list(artifacts.values())
        job["invalidated_l1_points"] = invalidated
        if invalidated:
            job.setdefault("result", {})["invalidated_l1_points"] = invalidated
            write(self.root(state["id"]) / "jobs" / job["id"] / "result.json", job["result"])

    def _execute(self, run_id, job_id):
        root = self.root(run_id)
        with self.lock:
            state = self.read(run_id)
            job = next(j for j in state["jobs"] if j["id"] == job_id)
            job.update(status="running", started_at=now())
            self.save(state)
        job_root = root / "jobs" / job_id
        output = job_root / "products"
        write(job_root / "config.json", job["config"])
        try:
            if job["layer"] in ("EC_L1", "EC_L2"):
                self.verify_tb(job["tb_version"])
            # Each attempt has isolated inputs and outputs, including failed attempts.
            json_layers = {job["layer"]}
            aggregate_input = {"TB_L2": "TB_L1", "EC_L3": "EC_L2"}.get(job["layer"])
            if aggregate_input:
                json_layers.add(aggregate_input)
            for json_layer in json_layers:
                for src in (layer_dir(root / "products", state["payload"], json_layer) / "json").glob(
                    "*.json"
                ):
                    if (job["layer"] == "TB_L1" and job["config"].get("only")
                            and src.name.endswith("_fit_results.l1.json")
                            and src.name.removesuffix("_fit_results.l1.json") not in job["config"]["only"]):
                        continue
                    target = output / src.relative_to(root / "products")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
            input_layer = {"TB_L1": "TB_L0", "EC_L1": "EC_L0", "EC_L2": "EC_L1"}.get(job["layer"])
            if input_layer:
                for src in (layer_dir(root / "products", state["payload"], input_layer) / "parquet").glob(
                    "*.parquet"
                ):
                    if (job["layer"] == "TB_L1" and job["config"].get("only")
                            and src.stem.removesuffix(".l0") not in job["config"]["only"]):
                        continue
                    target = output / src.relative_to(root / "products")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
            target_dir = layer_dir(output, state["payload"], job["layer"])
            before = {str(p): (digest(p), p.stat().st_mtime_ns) for p in target_dir.rglob("*") if p.is_file()}
            with (job_root / "processor.log").open("w", encoding="utf-8") as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    result = self.adapter_for({**state, "acquisition": job.get("acquisition")}).execute(
                        state["payload"],
                        job["layer"],
                        output,
                        job["config"],
                        Path(job["tb_version"]["path"]) if job.get("tb_version") else None,
                    )
            result["acquisition"] = job.get("acquisition")
            if job["layer"] == "EC_L3" and job.get("ec_coverage", {}).get("mixed"):
                result.setdefault("warnings", []).append(job["ec_coverage"]["description"])
                result["ec_coverage"] = job["ec_coverage"]
            write(job_root / "result.json", result)
            job["result"] = clean(result)
            changed = [
                p
                for p in target_dir.rglob("*")
                if p.is_file() and before.get(str(p)) != (digest(p), p.stat().st_mtime_ns)
            ]
            if not result.get("success", False) and not result.get("partial", False):
                raise RuntimeError(json.dumps(clean(result.get("errors") or result), ensure_ascii=False))
            if not changed:
                raise RuntimeError("处理器没有生成本次新产物；历史文件不能作为成功依据")
            validation = self.validate(job["layer"], changed)
            job["validation"] = validation
            write(job_root / "validation.json", validation)
            generated = []
            for src in changed:
                rel = src.relative_to(output)
                target = root / "products" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                generated.append({"path": str(rel.as_posix()), "sha256": digest(src), "job_id": job_id})
            if result.get("partial"):
                counts = result.get("counts", {})
                job.update(status="partial", outputs=generated,
                           error=(f"{counts.get('channels_fitted', 0)} 个通道已保存，{counts.get('channels_failed', 0)} 个通道计算失败"
                                  if job["layer"] == "TB_L1" else
                                  f"已保存可用结果；{len(result.get('errors', []))} 项计算错误，详情见运行记录"))
            else:
                job.update(status="succeeded", outputs=generated)
        except Exception as exc:
            job.update(status="failed", error=str(exc))
            (job_root / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        job["finished_at"] = now()
        with self.lock:
            state = self.read(run_id)
            if job["layer"] == "TB_L1":
                self._record_tb_l1_attempt(state, job)
            if job["status"] in ("succeeded", "partial") or job.get("outputs"):
                layer = job["layer"]
                accumulated = {a["path"]: a for a in state["generated"].get(layer, [])}
                accumulated.update({a["path"]: a for a in job["outputs"]})
                state["generated"][layer] = list(accumulated.values())
                dirty = set(state["dirty_layers"])
                dirty.discard(layer)
                if job.get("acquisition"):
                    suffix = {
                        "TB_L0": ".l0.parquet",
                        "TB_L1": "_fit_results.l1.json",
                        "EC_L0": ".l0.parquet",
                        "EC_L1": "_corr.l1.parquet",
                        "EC_L2": "_fit_results.l2.json",
                    }.get(layer)
                    completed = set(state.setdefault("acquisition_products", {}).get(layer, []))
                    if layer == "TB_L1":
                        supported = {ch for ch in range(4) if not unsupported_spectral_model(_response_payload(state), ch)}
                        channel_coverage = state.setdefault("acquisition_l1_channels", {})
                        # Existing fully completed stems predate channel-level
                        # reruns and already belong to this reading revision.
                        for stem in completed:
                            channel_coverage.setdefault(stem, [0, 1, 2, 3])
                        for item in job.get("result", {}).get("processor", {}).get("outputs", []):
                            channels = set(channel_coverage.get(item["stem"], []))
                            channels.update(item.get("channels_updated", []))
                            channel_coverage[item["stem"]] = sorted(channels)
                        completed = {stem for stem, channels in channel_coverage.items()
                                     if set(channels) >= supported}
                    if layer == "EC_L2":
                        point_coverage = state.setdefault("acquisition_ec_l2_points", {})
                        ready = set()
                        for item in job.get("result", {}).get("processor", {}).get("outputs", []):
                            energy = item["energy"]
                            records = json.loads((target_dir / "json" / item["json"]).read_text(encoding="utf-8"))
                            expected_points = self._expected_ec_points(state, energy, records)
                            prior = set(point_coverage.get(energy, expected_points if energy in completed else []))
                            prior.update(item.get("points_updated", expected_points))
                            point_coverage[energy] = sorted(prior)
                            if expected_points and expected_points <= prior:
                                ready.add(energy)
                            else:
                                completed.discard(energy)
                        completed.update(ready)
                    completed.update(
                        Path(item["path"]).name.removesuffix(suffix)
                        for item in job["outputs"]
                        if suffix and item["path"].endswith(suffix)
                        and layer not in ("TB_L1", "EC_L2")
                    )
                    if layer in ("TB_L2", "EC_L3") and job["status"] == "succeeded":
                        completed.add("aggregate")
                    elif layer in ("TB_L2", "EC_L3"):
                        completed.discard("aggregate")
                    state["acquisition_products"][layer] = sorted(completed)
                    coverage = self.acquisition_coverage(state, layer)
                    pending = set(state.get("acquisition_pending_layers", []))
                    if coverage["current"]:
                        pending.discard(layer)
                    else:
                        dirty.add(layer)
                    state["acquisition_pending_layers"] = sorted(pending)
                    job["acquisition_coverage"] = coverage
                if layer == "TB_L1":
                    fitted_points = {
                        f"{state['payload']}:{item['stem']}:{channel}"
                        for item in job.get("result", {}).get("processor", {}).get("outputs", [])
                        for channel in item.get("channels_updated", range(item.get("channels_fitted", 0)))
                    }
                    state["pending_windows"] = [
                        p for p in state.get("pending_windows", []) if p not in fitted_points
                    ]
                    if state["pending_windows"] or state.get("tb_l1_failures"):
                        dirty.add("TB_L1")
                if layer == "EC_L2":
                    updated_points = {point for item in job.get("result", {}).get("processor", {}).get("outputs", [])
                                      for point in item.get("points_updated", [])}
                    state["pending_ec_windows"] = [
                        point
                        for point in state.get("pending_ec_windows", [])
                        if point not in updated_points
                    ]
                    if state["pending_ec_windows"]:
                        dirty.add("EC_L2")
                downstream = {
                    "TB_L0": ("TB_L1", "TB_L2", "EC_L1", "EC_L2", "EC_L3"),
                    "TB_L1": ("TB_L2", "EC_L1", "EC_L2", "EC_L3"),
                    "TB_L2": ("EC_L1", "EC_L2", "EC_L3"),
                    "EC_L0": ("EC_L1", "EC_L2", "EC_L3"),
                    "EC_L1": ("EC_L2", "EC_L3"),
                    "EC_L2": ("EC_L3",),
                }
                affected = set(downstream.get(layer, ()))
                if layer.startswith("TB_") and state.get("tb_binding") == "pinned":
                    affected.difference_update(("EC_L1", "EC_L2", "EC_L3"))
                dirty.update(affected)
                if job.get("acquisition"):
                    suffix = {
                        "TB_L0": ".l0.parquet",
                        "EC_L0": ".l0.parquet",
                        "EC_L1": "_corr.l1.parquet",
                    }.get(layer)
                    stems = {
                        Path(item["path"]).name.removesuffix(suffix)
                        for item in job["outputs"]
                        if suffix and item["path"].endswith(suffix)
                    }
                    rows = state["inventory"]["raw"]["reading_preview"]["rows"]
                    energy_keys = {
                        f"{row['energy_keV']:g}keV" if row["kind"] == "xray" else f"src_{row['source']}"
                        for row in rows
                        if row["stem"] in stems and row["kind"] in ("xray", "source")
                    }
                    if any(row["kind"] == "background" and row["stem"] in stems for row in rows):
                        energy_keys.update(f"src_{row['source']}" for row in rows if row["kind"] == "source")
                    subsets = {"TB_L1": stems} if layer == "TB_L0" else {}
                    if layer == "TB_L0":
                        channel_coverage = state.setdefault("acquisition_l1_channels", {})
                        for stem in stems:
                            channel_coverage.pop(stem, None)
                    if layer == "EC_L0":
                        subsets["EC_L1"] = stems
                    if layer in ("EC_L0", "EC_L1"):
                        subsets["EC_L2"] = energy_keys
                    pending = set(state.get("acquisition_pending_layers", []))
                    for downstream_layer in affected:
                        completed = set(state["acquisition_products"].get(downstream_layer, []))
                        state["acquisition_products"][downstream_layer] = (
                            sorted(completed - subsets[downstream_layer])
                            if downstream_layer in subsets
                            else []
                        )
                        if not self.acquisition_coverage(state, downstream_layer)["current"]:
                            pending.add(downstream_layer)
                    if "EC_L2" in subsets:
                        for energy in subsets["EC_L2"]:
                            state.setdefault("acquisition_ec_l2_points", {}).pop(energy, None)
                            state.setdefault("ec_l2_provenance", {}).pop(
                                f"{state['payload']}/ec/L2/json/{energy}_fit_results.l2.json", None)
                    state["acquisition_pending_layers"] = sorted(pending)
                state["dirty_layers"] = sorted(dirty)
                if layer == "TB_L2":
                    files = [o for o in job["outputs"] if o["path"].endswith("_TB.l2.json")]
                    if files:
                        f = files[-1]
                        state["tb_version"] = {
                            "filename": Path(f["path"]).name,
                            "sha256": f["sha256"],
                            "path": str(output / f["path"]),
                            "origin": "computed",
                            "job_id": job_id,
                        }
                if layer == "EC_L1":
                    state["ec_dependency"] = copy.deepcopy(job["tb_version"])
                    state["ec_scope"] = job["scope"]
                    corrected = {
                        Path(artifact["path"]).name.removesuffix("_corr.l1.parquet")
                        for artifact in job["outputs"]
                        if artifact["path"].endswith("_corr.l1.parquet")
                    }
                    state["pending_ec_l0"] = [
                        stem for stem in state.get("pending_ec_l0", []) if stem not in corrected
                    ]
                    if state["pending_ec_l0"]:
                        state["dirty_layers"] = sorted(set(state["dirty_layers"]) | {"EC_L1"})
                if layer == "EC_L0":
                    parsed = {
                        Path(artifact["path"]).name.removesuffix(".l0.parquet")
                        for artifact in job["outputs"]
                        if artifact["path"].endswith(".l0.parquet")
                    }
                    state["pending_ec_l0"] = sorted(set(state.get("pending_ec_l0", [])) | parsed)
                if layer == "EC_L2":
                    provenance = state.setdefault("ec_l2_provenance", {})
                    for artifact in job["outputs"]:
                        if artifact["path"].endswith("_fit_results.l2.json"):
                            rel = artifact["path"]
                            energy = Path(rel).name.removesuffix("_fit_results.l2.json")
                            rows = json.loads((root / "products" / rel).read_text(encoding="utf-8"))
                            all_points = {f"{state['payload']}:ec:{energy}:{row['channel']}:{float(row['E']):g}"
                                          for row in rows if not row.get("placeholder")}
                            previous = provenance.get(rel, {})
                            point_versions = dict(previous.get("point_versions", {
                                point: previous["tb_version"] for point in all_points
                            } if previous.get("tb_version") else {}))
                            updates = {point for item in result.get("processor", {}).get("outputs", [])
                                       if item["json"] == Path(rel).name for point in item.get("points_updated", all_points)}
                            point_versions.update({point: job["tb_version"] for point in updates})
                            provenance[rel] = {"job_id": job_id, "tb_version": job["tb_version"],
                                               "point_versions": point_versions}
            state["jobs"] = [job if j["id"] == job_id else j for j in state["jobs"]]
            state["updated_at"] = now()
            write(job_root / "job.json", job)
            self.save(state)

    @staticmethod
    def _compact_job(job):
        result = job.get("result") or {}
        processor = result.get("processor") or {}
        compact = {key: value for key, value in job.items() if key not in ("config", "result", "validation")}
        compact["config"] = {key: value for key, value in job.get("config", {}).items()
                             if key in ("only", "fit_channels", "representative", "ec_point_id", "bin_width", "energy_key")}
        if "result" in job:
            compact["result"] = {key: result[key] for key in ("success", "partial", "summary", "counts", "warnings", "errors") if key in result}
            compact["result"]["processor"] = {key: value for key, value in processor.items()
                                               if key in ("channels_fitted", "channels_accepted", "channels_suspect", "channels_rejected",
                                                          "channels_failed", "quality_policy", "skipped_channels")}
        if job.get("validation"):
            compact["validation"] = {key: value for key, value in job["validation"].items() if key != "files"}
            compact["validation"]["files"] = [{"file": item["file"], "valid": item["valid"]}
                                               for item in job["validation"].get("files", [])]
        return compact

    def job(self, run_id, job_id, *, compact=False):
        state = self.read(run_id)
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if not job:
            raise FileNotFoundError("任务不存在")
        log = self.root(run_id) / "jobs" / job_id / "processor.log"
        if log.exists():
            # Four bytes per Unicode character is enough to retain the same
            # tail without rereading an ever-growing batch log on each poll.
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 48000))
                tail = stream.read().decode("utf-8", errors="replace")
                job["log"] = tail.replace("\r\n", "\n").replace("\r", "\n")[-12000:]
        return self._compact_job(job) if compact else job

    def validate(self, layer, paths):
        """Use the workspace's authoritative checkers read-only, separately from computation."""
        checker = self.workspace / "cali_format" / "src" / "check" / f"{layer}.py"
        if not checker.exists():
            return {"status": "unavailable", "reason": "未找到 cali_format 检查器", "files": []}
        spec = importlib.util.spec_from_file_location(f"workbench_check_{layer}", checker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        scientific = [
            p
            for p in paths
            if (
                p.suffix == ".parquet"
                or p.name.endswith(
                    ("_fit_results.l1.json", "_fit_results.l2.json", "_TB.l2.json", ".l3.json")
                )
            )
        ]
        rows = []
        for path in scientific:
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    valid = bool(module.validate(str(path)))
            except Exception as exc:
                valid = False
                output.write(f"{type(exc).__name__}: {exc}")
            rows.append({"file": path.name, "valid": valid, "details": output.getvalue()})
        return {
            "status": "passed" if rows and all(r["valid"] for r in rows) else "failed",
            "checker": str(checker),
            "checker_sha256": digest(checker),
            "files": rows,
            "passed": sum(r["valid"] for r in rows),
            "total": len(rows),
        }

    def surface(self, run_id, channel=0, layer="TB_L2"):
        self._channel(channel)
        if layer not in ("TB_L0", "TB_L1", "TB_L2"):
            raise ValueError("TB surface layer must be TB_L0, TB_L1 or TB_L2")
        state = self.read(run_id)
        root = self.root(run_id)
        adapter = self.adapter_for(state)
        if layer == "TB_L0":
            empty = {"points": [], "params": None, "grid": None, "diagnostics": {}, "version": None}
            files = {
                stem: {
                    "stem": stem,
                    "path": str(path),
                    "source": "run" if (root / "products") in path.parents else "historical",
                    "fit_status": "parsed",
                }
                for stem, path in adapter.tb_files(state["payload"], root / "products").items()
            }
            for sample in state.get("inventory", {}).get("raw", {}).get("tb_samples", []):
                files[sample["stem"]] = {
                    **sample,
                    "source": "raw",
                    "fit_status": "not_parsed",
                    **files.get(sample["stem"], {}),
                }
            return clean(
                {**empty, "baseline": empty, "files": [files[key] for key in sorted(files)], "dirty": False}
            )
        result = adapter.surface(state["payload"], root / "products", channel)
        result["baseline"] = adapter.surface(state["payload"], root / "baseline", channel)
        result["files"] = [
            {"stem": stem}
            for stem in sorted(
                {
                    path.name.removesuffix(".parquet").removesuffix(".l0")
                    for directory in (
                        layer_dir(root / "products", state["payload"], "TB_L0"),
                        adapter.historical_layer(state["payload"], "TB_L0"),
                    )
                    for path in directory.glob("parquet/*.parquet")
                }
            )
        ]
        for p in result["points"]:
            p["actual_used"] = p.get("used", False)
            p["excluded"] = p["id"] in state["config"]["excluded"]
            p["draft_used"] = usable_peak(p) and not p["excluded"]
            p["used"] = p["actual_used"] and not p["excluded"]
        result["dirty"] = "TB_L2" in state["dirty_layers"]
        return clean(result)

    @staticmethod
    def _channel(channel):
        if channel not in range(4):
            raise ValueError("channel 必须为 0—3")

    def spectrum(self, run_id, stem, channel=0, layer="TB_L1"):
        self._channel(channel)
        state = self.read(run_id)
        result = self.adapter_for(state).spectrum(
            state["payload"], self.root(run_id) / "products", stem, channel, layer=layer
        )
        result["stem"], result["channel"] = stem, channel
        result["draft_window"] = state["config"]["windows"].get(stem, {}).get(str(channel))
        result["excluded"] = f"{state['payload']}:{stem}:{channel}" in state["config"]["excluded"]
        result["fit_source"] = (
            "not_fitted"
            if result["fit"] is None
            else "computed"
            if any(
                a["path"].endswith(f"/{stem}_fit_results.l1.json")
                for a in state["generated"].get("TB_L1", [])
            )
            else "historical"
        )
        attempt = next(
            (
                job
                for job in reversed(state["jobs"])
                if job["layer"] == "TB_L1"
                and channel in job["config"].get("fit_channels", [0, 1, 2, 3])
                and (
                    stem in job["config"].get("only", [])
                    or job.get("scope") == "all"
                    or (
                        job.get("scope") == "representative"
                        and stem in state["inventory"]["representative"]["tb_stems"]
                    )
                )
            ),
            None,
        )
        result["last_fit_attempt"] = (
            {key: attempt.get(key) for key in ("id", "status", "error", "scope", "created_at", "finished_at")}
            if attempt
            else None
        )
        if attempt and attempt.get("result"):
            errors = attempt["result"].get("errors", [])
            matching = [error for error in errors if isinstance(error, dict)
                        and (error.get("stem") == stem or error.get("file") == f"{stem}.l0.parquet")
                        and error.get("channel") in (None, channel)]
            updated = any(item.get("stem") == stem and channel in item.get("channels_updated", [])
                          for item in attempt["result"].get("processor", {}).get("outputs", []))
            if matching:
                result["last_fit_attempt"].update(status="failed", error="; ".join(error["error"] for error in matching))
            elif updated:
                result["last_fit_attempt"].update(status="succeeded", error=None)
            elif attempt["status"] in ("partial", "failed"):
                result["last_fit_attempt"].update(status="not_fitted", error=None)
        return clean(result)

    def ec_overview(self, run_id, channel=0, layer="EC_L2"):
        self._channel(channel)
        state = self.read(run_id)
        adapter = self.adapter_for(state)
        if layer in ("EC_L0", "EC_L1"):
            files = {
                item["stem"]: item
                for item in adapter.ec_files(state["payload"], self.root(run_id) / "products", layer, channel)
            }
            if layer == "EC_L0":
                raw = state.get("inventory", {}).get("raw", {})
                rows = (raw.get("reading_preview") or {}).get("rows", []) or raw.get("ec_samples", [])
                for row in rows:
                    if row.get("kind") == "tb" or row.get("channel") not in (None, channel):
                        continue
                    files[row["stem"]] = {
                        **row,
                        "source": "raw",
                        "fit_status": "not_parsed",
                        "channel": channel,
                        **files.get(row["stem"], {}),
                    }
            empty = {
                "points": [],
                "params": None,
                "curve": {"x": [], "y": []},
                "resolution_curve": {"x": [], "y": []},
                "diagnostics": {},
                "version": None,
            }
            return clean(
                {
                    **empty,
                    "baseline": empty,
                    "files": list(files.values()),
                    "selected_tb": self.selected_tb(state),
                    "ec_dependency": state.get("ec_dependency"),
                }
            )
        result = ECInspector(adapter).overview(state["payload"], self.root(run_id) / "products", channel,
                                               points=self._ec_points(state, channel, adapter))
        for point in result["points"]:
            point["actual_used"] = point.get("used", False)
            point["excluded"] = point["id"] in state["config"].get("ec_excluded", [])
            point["draft_used"] = point.get("quality_pass", False) and not point["excluded"]
            point["used"] = point["actual_used"] and not point["excluded"]
            point["draft_window"] = state["config"].get("ec_windows", {}).get(point["id"]) or point.get("draft_window")
        result["baseline"] = ECInspector(adapter).overview(
            state["payload"], self.root(run_id) / "baseline", channel
        )
        result["selected_tb"] = self.selected_tb(state)
        result["ec_dependency"] = state.get("ec_dependency")
        return clean(result)

    def ec_spectrum(self, run_id, layer, channel=0, stem=None, point_id=None):
        self._channel(channel)
        state = self.read(run_id)
        adapter = self.adapter_for(state)
        if layer in ("EC_L0", "EC_L1"):
            result = adapter.ec_spectrum(
                state["payload"],
                self.root(run_id) / "products",
                layer,
                channel,
                stem=stem,
                preferred_stems=state.get("inventory", {}).get("representative", {}).get("ec_stems", []),
            )
        elif layer in ("EC_L2", "EC_L3") and point_id:
            point = next((item for item in self._ec_points(state, channel, adapter) if item["id"] == point_id), None)
            if point is None:
                raise FileNotFoundError("能线不存在，请刷新源峰配置")
            result = ECInspector(adapter).spectrum(
                state["payload"], self.root(run_id) / "products", point_id, channel, point=point,
            )
            result["draft_window"] = state["config"].get("ec_windows", {}).get(point_id) or point.get("draft_window")
            result["excluded"] = point_id in state["config"].get("ec_excluded", [])
        else:
            raise ValueError("请选择 EC 层及有效能峰")
        if reason := unsupported_spectral_model(_response_payload(state), channel):
            result.update(unsupported_spectral_model=reason)
            if layer in ("EC_L2", "EC_L3"):
                result.update(fit=None, curve=None)
        result["tb_version"] = state.get("ec_dependency") if layer != "EC_L0" else None
        if layer != "EC_L0":
            from ..common.utils import read_parquet_metadata
            from ..ec.processors.L1_processor import ECL1Processor

            selected = self.selected_tb(state)
            expected = (
                ECL1Processor(state["payload"], self.runs, response_payload=_response_payload(state))._load_tb_params(Path(selected["path"]))
                if selected
                else None
            )
            files = []
            for role, key in (("signal", "source_path"), ("background", "background_path")):
                if not result.get(key):
                    continue
                source = Path(result[key])
                metadata = read_parquet_metadata(source, "tb_correction")
                actual = metadata.get("tb_params_used", {})
                matches = (
                    all(actual.get(str(ch)) == params for ch, params in expected.items())
                    if expected
                    else None
                )
                files.append(
                    {
                        "role": role,
                        "filename": source.name,
                        "tb_corrfile": metadata.get("tb_corrfile"),
                        "matches_selected": matches,
                    }
                )
            result["dependency_evidence"] = {
                "selected_tb": selected,
                "files": files,
                "matches_selected": all(item["matches_selected"] for item in files)
                if files and selected
                else None,
            }
        return result

    def view(self, run_id, *, compact=False):
        state = self.read(run_id)
        root = self.root(run_id)
        payload = state["payload"]
        adapter = self.adapter_for(state)
        artifacts = []
        stages = {}
        for layer in LAYERS:
            hist = adapter.historical_layer(payload, layer)
            generated = {a["path"] for a in state["generated"].get(layer, [])}
            files = [p for p in hist.rglob("*") if p.is_file()]
            recent = next((j for j in reversed(state["jobs"]) if j["layer"] == layer), None)
            purpose, input_text, output_text = PURPOSES[layer]
            stages[layer] = {
                "history_count": len(files),
                "current_count": len(generated),
                "status": recent["status"] if recent else "not_run",
                "stale": layer in state["dirty_layers"],
                "error": recent.get("error") if recent else None,
                "scope": recent.get("scope") if recent else None,
                "validation": recent.get("validation") if recent else None,
                "purpose": purpose,
                "input": input_text,
                "output": output_text,
                "dependencies": DEPENDENCIES[layer],
                "acquisition_coverage": self.acquisition_coverage(state, layer),
                "unavailable_reason": adapter.unavailable_reason(layer),
            }
            if layer == "EC_L1":
                stages[layer]["tb_dependency"] = self.selected_tb(state)
            for source, source_root, prefix in (
                ("historical", hist, f"/api/runs/{run_id}/history/{layer}/"),
                ("run", root / "products", f"/api/runs/{run_id}/files/products/"),
            ):
                # History was already enumerated for its count; current
                # outputs are tracked explicitly by successful jobs.
                paths = files if source == "historical" else (source_root / rel for rel in sorted(generated))
                for path in paths:
                    if source == "run" and not path.is_file():
                        continue
                    rel = path.relative_to(source_root).as_posix()
                    computed = source == "run" and rel in generated
                    # Seeded JSON snapshots are inputs, never current computation artifacts.
                    if source == "run" and not computed:
                        continue
                    artifacts.append(
                        {
                            "name": path.name,
                            "path": rel,
                            "layer": layer,
                            "kind": path.suffix.lstrip("."),
                            "source": source,
                            "url": prefix + quote(rel),
                            "computed": computed,
                        }
                    )
        state.update(
            stages=stages,
            artifacts=artifacts,
            dependency_stale=self.stale(state),
            selected_tb=self.selected_tb(state),
            ec_coverage=self.ec_coverage(state),
            scientific_notes=[
                *state.get("import_warnings", []),
                *(
                    [
                        "当前启用试算独立读取标准；历史图表可能包含配置范围外的工况。新 TB L2 / EC L3 聚合只使用当前映射范围的产物，须先完成这些输入的重算。"
                    ]
                    if state.get("acquisition")
                    else []
                ),
                "EC L3 现行处理器采用 E=f(ADC) 全局拟合（low/high 同系数）；wiki 规定分段 ADC=f(E)。本 Demo 保留现行算法，系数不可直接按 wiki 方向使用。",
                "L0 CRC sanity 需单独检查；处理完成不等于 CRC 或 schema 校验通过。",
                "代表样例只重算所示工况/能量；其余点来自明确导入的历史 JSON，EC L3 可能混合历史能峰。",
            ],
        )
        if compact:
            state["jobs"] = [self._compact_job(job) for job in state["jobs"]]
            state["revisions"] = [{key: value for key, value in revision.items() if key != "config"}
                                  for revision in state.get("revisions", [])]
        return clean(state)
