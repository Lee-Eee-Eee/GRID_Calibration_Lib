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

from .compute import ComputeAdapter
from .datasets import DatasetCatalog, validate_payload
from .dependencies import snapshot, tb_package
from .ec_interactive import ECInspector

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


class Workbench:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.runs = self.workspace / "runs"
        self.runs.mkdir(exist_ok=True)
        self.adapter = ComputeAdapter(self.workspace)
        self.catalog = DatasetCatalog(self.workspace)
        self.lock = threading.RLock()
        # Matplotlib and the legacy parser have process-global state.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calibration")
        self.inventory_cache = {}
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
        return json.loads((self.root(run_id) / "state.json").read_text(encoding="utf-8"))

    def save(self, state):
        write(self.root(state["id"]) / "state.json", state)

    def inventory(self, payload):
        if payload not in ("14B", "15B"):
            raise ValueError("payload 必须为 14B 或 15B")
        if payload not in self.inventory_cache:
            self.inventory_cache[payload] = self.adapter.inventory(payload)
        return copy.deepcopy(self.inventory_cache[payload])

    def adapter_for(self, state):
        return ComputeAdapter(self.workspace, state["dataset"]) if state.get("dataset") else self.adapter

    def scan_datasets(self, **paths):
        with self.lock:
            return self.catalog.scan(**paths)

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
        runs = []
        for path in sorted(self.runs.glob("*/state.json"), reverse=True):
            state = json.loads(path.read_text(encoding="utf-8"))
            runs.append({k: state.get(k) for k in ("id", "payload", "created_at", "name")})
        catalog = self.catalog.scan()
        if self.catalog.path.exists():
            saved = json.loads(self.catalog.path.read_text(encoding="utf-8"))
            catalog["datasets"] = saved.get("datasets", catalog["datasets"])
        return {
            **catalog,
            "payloads": list(dict.fromkeys(item["payload"] for item in catalog["datasets"])),
            "runs": runs,
            "active_run": runs[0]["id"] if runs else None,
            "workspace": str(self.workspace),
        }

    def create(self, payload=None, name=None, dataset_id=None):
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
                tb_package(candidate)
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
        state["selected_tb"] = snapshot(tb_package(tb), root) if tb else None
        if dataset and state["selected_tb"] and self._imported_ec_matches(state, adapter):
            state["ec_dependency"] = {**state["selected_tb"], "input_origin": "imported_metadata_verified"}
            state["ec_scope"] = "all"
        write(root / "state.json", state)
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
        expected = ECL1Processor(state["payload"], self.runs)._load_tb_params(Path(selected["path"]))
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
                            math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi <= 1200 and hi - lo >= 8
                        ):
                            raise ValueError("窗口须满足 0 ≤ lo < hi ≤ 1200，宽度至少 8 ADC")
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
                known = {
                    point["id"]
                    for channel in range(4)
                    for point in ECInspector(adapter).overview(
                        state["payload"], self.root(run_id) / "products", channel
                    )["points"]
                }
                windows = patch.get("ec_windows", {})
                if not isinstance(windows, dict):
                    raise ValueError("ec_windows 必须为对象")
                for point_id, bounds in windows.items():
                    if point_id not in known or not isinstance(bounds, dict):
                        raise ValueError("EC 能峰不存在")
                    lo, hi = float(bounds["lo"]), float(bounds["hi"])
                    if not (
                        math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi <= 65536 and hi - lo >= 8
                    ):
                        raise ValueError("EC 窗口须满足 0 ≤ lo < hi ≤ 65536，宽度至少 8 ADC")
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
            state["dirty_layers"] = sorted(set(state["dirty_layers"]) | changed)
            state["updated_at"] = now()
            self.save(state)
        return self.view(run_id)

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
                package = tb_package(path, origin)
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
                tb_package(Path(path), "external")
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
            write(self.root(run_id) / "revisions" / f"{state['config_revision']:04d}.json", revision)
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

    def submit(self, run_id, spec):
        with self.lock:
            state = self.read(run_id)
            layer = spec.get("layer")
            if layer not in LAYERS:
                raise ValueError("未知处理层")
            if any(j["status"] in ("queued", "running") for j in state["jobs"]):
                raise ValueError("此试算已有计算任务")
            if layer == "TB_L2" and "TB_L1" in state["dirty_layers"]:
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
            if layer == "EC_L2" and state.get("ec_dependency") is None and not spec.get("point_id"):
                raise ValueError("请先运行 EC L1，固定本次 TB 版本")
            if layer == "EC_L3" and "EC_L2" in state["dirty_layers"]:
                raise ValueError("请先运行 EC L2 更新能峰拟合")
            config = copy.deepcopy(state["config"])
            config["representative"] = bool(spec.get("representative", True))
            if (
                layer == "EC_L2"
                and not spec.get("point_id")
                and not config["representative"]
                and state.get("ec_scope") != "all"
            ):
                raise ValueError("全量 EC L2 需要先执行全量 EC L1；当前仅有代表样例校正")
            if spec.get("stem"):
                adapter = self.adapter_for(state)
                known = {p["stem"] for p in adapter.points(state["payload"], self.root(run_id) / "products")}
                if layer.startswith("TB_"):
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
                if spec["stem"] not in known:
                    raise ValueError("未知工况")
                config["only"] = [spec["stem"]]
            if spec.get("point_id"):
                if layer != "EC_L2":
                    raise ValueError("能峰局部重拟合只支持 EC L2")
                channel = int(spec.get("channel", 0))
                self._channel(channel)
                overview = self.ec_overview(run_id, channel)
                if not any(point["id"] == spec["point_id"] for point in overview["points"]):
                    raise ValueError("EC 能峰不存在")
                config["ec_point_id"] = spec["point_id"]
            if layer == "EC_L2" and state.get("pending_ec_l0"):
                pending = state["pending_ec_l0"]
                if config.get("ec_point_id"):
                    energy = config["ec_point_id"].split(":")[2]
                    affected = [
                        stem
                        for stem in pending
                        if stem.startswith(energy + "_")
                        or (energy.startswith("src_") and stem.startswith("src_bkg_"))
                    ]
                elif config.get("only"):
                    affected = sorted(set(pending) & set(config["only"]))
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
                if config.get("only") or config.get("ec_point_id")
                else "representative"
                if config["representative"]
                else "all",
                "tb_version": copy.deepcopy(selected if layer.startswith("EC_") else state["tb_version"]),
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
            for src in (root / "products").glob("*/*/*/json/*.json"):
                target = output / src.relative_to(root / "products")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            input_layer = {"TB_L1": "TB_L0", "EC_L1": "EC_L0", "EC_L2": "EC_L1"}.get(job["layer"])
            if input_layer:
                for src in (layer_dir(root / "products", state["payload"], input_layer) / "parquet").glob(
                    "*.parquet"
                ):
                    target = output / src.relative_to(root / "products")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
            target_dir = layer_dir(output, state["payload"], job["layer"])
            before = {str(p): (digest(p), p.stat().st_mtime_ns) for p in target_dir.rglob("*") if p.is_file()}
            with (job_root / "processor.log").open("w", encoding="utf-8") as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    result = self.adapter_for(state).execute(
                        state["payload"],
                        job["layer"],
                        output,
                        job["config"],
                        Path(job["tb_version"]["path"]) if job.get("tb_version") else None,
                    )
            if job["layer"] == "EC_L3" and job.get("ec_coverage", {}).get("mixed"):
                result.setdefault("warnings", []).append(job["ec_coverage"]["description"])
                result["ec_coverage"] = job["ec_coverage"]
            write(job_root / "result.json", result)
            changed = [
                p
                for p in target_dir.rglob("*")
                if p.is_file() and before.get(str(p)) != (digest(p), p.stat().st_mtime_ns)
            ]
            if not result.get("success", False):
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
            job.update(status="succeeded", result=clean(result), outputs=generated)
        except Exception as exc:
            job.update(status="failed", error=str(exc))
            (job_root / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        job["finished_at"] = now()
        with self.lock:
            state = self.read(run_id)
            if job["status"] == "succeeded":
                layer = job["layer"]
                accumulated = {a["path"]: a for a in state["generated"].get(layer, [])}
                accumulated.update({a["path"]: a for a in job["outputs"]})
                state["generated"][layer] = list(accumulated.values())
                dirty = set(state["dirty_layers"])
                dirty.discard(layer)
                if layer == "TB_L1":
                    fitted_stems = {
                        Path(a["path"]).name.removesuffix("_fit_results.l1.json")
                        for a in job["outputs"]
                        if a["path"].endswith("_fit_results.l1.json")
                    }
                    state["pending_windows"] = [
                        p for p in state.get("pending_windows", []) if p.split(":")[1] not in fitted_stems
                    ]
                    if state["pending_windows"]:
                        dirty.add("TB_L1")
                if layer == "EC_L2":
                    fitted_energies = {
                        Path(a["path"]).name.removesuffix("_fit_results.l2.json")
                        for a in job["outputs"]
                        if a["path"].endswith("_fit_results.l2.json")
                    }
                    state["pending_ec_windows"] = [
                        point
                        for point in state.get("pending_ec_windows", [])
                        if point.split(":")[2] not in fitted_energies
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
                            provenance[artifact["path"]] = {"job_id": job_id, "tb_version": job["tb_version"]}
            state["jobs"] = [job if j["id"] == job_id else j for j in state["jobs"]]
            state["updated_at"] = now()
            write(job_root / "job.json", job)
            self.save(state)

    def job(self, run_id, job_id):
        state = self.read(run_id)
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if not job:
            raise FileNotFoundError("任务不存在")
        log = self.root(run_id) / "jobs" / job_id / "processor.log"
        if log.exists():
            job["log"] = log.read_text(encoding="utf-8")[-12000:]
        return job

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

    def surface(self, run_id, channel=0):
        self._channel(channel)
        state = self.read(run_id)
        root = self.root(run_id)
        adapter = self.adapter_for(state)
        result = adapter.surface(state["payload"], root / "products", channel)
        result["baseline"] = adapter.surface(state["payload"], root / "baseline", channel)
        result["files"] = [
            {"stem": stem}
            for stem in sorted(
                {
                    path.name.removesuffix(".l0.parquet")
                    for directory in (
                        layer_dir(root / "products", state["payload"], "TB_L0"),
                        adapter.historical_layer(state["payload"], "TB_L0"),
                    )
                    for path in directory.glob("parquet/*.l0.parquet")
                }
            )
        ]
        for p in result["points"]:
            p["excluded"] = p["id"] in state["config"]["excluded"]
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
            "computed"
            if any(
                a["path"].endswith(f"/{stem}_fit_results.l1.json")
                for a in state["generated"].get("TB_L1", [])
            )
            else "historical"
        )
        return clean(result)

    def ec_overview(self, run_id, channel=0, layer="EC_L2"):
        self._channel(channel)
        state = self.read(run_id)
        adapter = self.adapter_for(state)
        result = ECInspector(adapter).overview(state["payload"], self.root(run_id) / "products", channel)
        for point in result["points"]:
            point["actual_used"] = point.get("used", False)
            point["excluded"] = point["id"] in state["config"].get("ec_excluded", [])
            point["draft_used"] = point.get("quality_pass", False) and not point["excluded"]
            point["used"] = point["actual_used"] and not point["excluded"]
            point["draft_window"] = state["config"].get("ec_windows", {}).get(point["id"])
        result["baseline"] = ECInspector(adapter).overview(
            state["payload"], self.root(run_id) / "baseline", channel
        )
        if layer in ("EC_L0", "EC_L1"):
            # Return available files even when the selected channel has no representative energy.
            try:
                result["files"] = adapter.ec_spectrum(
                    state["payload"], self.root(run_id) / "products", layer, channel
                ).get("files", [])
            except FileNotFoundError:
                result["files"] = []
        result["selected_tb"] = self.selected_tb(state)
        result["ec_dependency"] = state.get("ec_dependency")
        return clean(result)

    def ec_spectrum(self, run_id, layer, channel=0, stem=None, point_id=None):
        self._channel(channel)
        state = self.read(run_id)
        adapter = self.adapter_for(state)
        if layer in ("EC_L0", "EC_L1"):
            result = adapter.ec_spectrum(
                state["payload"], self.root(run_id) / "products", layer, channel, stem=stem
            )
        elif layer in ("EC_L2", "EC_L3") and point_id:
            result = ECInspector(adapter).spectrum(
                state["payload"], self.root(run_id) / "products", point_id, channel
            )
            result["draft_window"] = state["config"].get("ec_windows", {}).get(point_id)
            result["excluded"] = point_id in state["config"].get("ec_excluded", [])
        else:
            raise ValueError("请选择 EC 层及有效能峰")
        result["tb_version"] = state.get("ec_dependency") if layer != "EC_L0" else None
        if layer != "EC_L0":
            from ..common.utils import read_parquet_metadata
            from ..ec.processors.L1_processor import ECL1Processor

            selected = self.selected_tb(state)
            expected = (
                ECL1Processor(state["payload"], self.runs)._load_tb_params(Path(selected["path"]))
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
        return clean(result)

    def view(self, run_id):
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
            }
            if layer == "EC_L1":
                stages[layer]["tb_dependency"] = self.selected_tb(state)
            for source, source_root, prefix in (
                ("historical", hist, f"/api/runs/{run_id}/history/{layer}/"),
                ("run", root / "products", f"/api/runs/{run_id}/files/products/"),
            ):
                directory = source_root if source == "historical" else layer_dir(source_root, payload, layer)
                for path in directory.rglob("*"):
                    if not path.is_file():
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
                "EC L3 现行处理器采用 E=f(ADC) 全局拟合（low/high 同系数）；wiki 规定分段 ADC=f(E)。本 Demo 保留现行算法，系数不可直接按 wiki 方向使用。",
                "L0 CRC sanity 需单独检查；处理完成不等于 CRC 或 schema 校验通过。",
                "代表样例只重算所示工况/能量；其余点来自明确导入的历史 JSON，EC L3 可能混合历史能峰。",
            ],
        )
        return clean(state)
