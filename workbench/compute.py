"""Workbench adapters for the library's seven scientific processors.

The workbench stores revisions and provenance separately from the wiki-defined
scientific files. Historical products are read-only inputs; processors write only
inside a workspace run. No peak or surface fitting algorithm is implemented here.
"""

from __future__ import annotations

import copy
import json
import math
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel

from ..common.fit_policy import quality_warnings, unavailable_fit, usable_peak
from ..common.spectral_models import detector_identity, photopeak_channels, unsupported_spectral_model
from ..common.utils import (
    DataLayout,
    read_json,
    read_parquet_metadata,
    validated_events,
    write_json,
)
from ..config.payload_config import (
    EC_PROCESS_PARAMS,
    TB_PROCESS_PARAMS,
    PayloadConfig,
    SourceSpec,
    get_payload_config,
)
from ..ec.processors.L0_processor import ECL0Processor
from ..ec.processors.L1_processor import ECL1Processor
from ..ec.processors.L2_processor import ECL2Processor
from ..ec.processors.L3_processor import ECL3Processor
from ..tb.processors.L0_processor import TBL0Processor
from ..tb.processors.L1_processor import TBL1Processor
from ..tb.processors.L2_processor import TBL2Processor
from .acquisition import discover, tb_histogram_config, validate_profile
from .datasets import DatasetCatalog, validate_payload
from .dependencies import tb_package

LAYERS = ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3")


def _stem(value: str) -> str:
    return str(value).removesuffix(".parquet").removesuffix("_corr.l1").removesuffix(".l0")


def _clean(value: Any) -> Any:
    """JSON has no NaN/Infinity: missing numerical diagnostics become null."""
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Path):
        return str(value)
    return value


class ComputeAdapter:
    def __init__(self, workspace: Path, dataset: dict | None = None, acquisition: dict | None = None,
                 *, response_payload: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.dataset = DatasetCatalog(self.workspace).resolve(dataset) if dataset is not None else None
        self.source_payload = self.dataset["source_payload"] if self.dataset else None
        self.history = (
            Path(self.dataset["product_dir"]).parent
            if self.dataset and self.dataset.get("product_dir")
            else self.workspace / "products"
        )
        self.resources = Path(__file__).resolve().parents[1] / "resources"
        self.acquisition = validate_profile(acquisition) if acquisition is not None else None
        self.response_payload = response_payload

    def detector_payload(self, payload):
        return self.response_payload or detector_identity(payload, self.dataset)

    def unavailable_reason(self, layer):
        capabilities = (self.acquisition or {}).get("processing_capabilities", {})
        capability = capabilities.get(layer, {}) if isinstance(capabilities, dict) else {}
        if isinstance(capability, dict) and capability.get("supported") is False:
            reason = capability.get("reason")
            return (
                reason.strip()
                if isinstance(reason, str) and reason.strip()
                else f"当前读取预设不支持 {layer}"
            )
        return None

    def observations(self, payload):
        return discover(self._base_config(payload)[1], self.acquisition) if self.acquisition else None

    def historical_layer(self, payload: str, layer: str) -> Path:
        """Read-only source layer; an edited output label never renames its input."""
        if self.dataset:
            if self.dataset.get("product_dir"):
                kind, level = layer.split("_")
                return Path(self.dataset["product_dir"]) / kind.lower() / level
            # A raw-only dataset must not silently inherit an unrelated default.
            return self.workspace / "runs" / "_no_historical_products" / payload / layer
        return self._layer_dir(self.history, payload, layer)

    @staticmethod
    def _source_config(raw: Path):
        """Infer known isotope metadata from filenames, keeping fitting in lib."""
        directory = raw / "source"
        peaks = {}
        for label in ("14B", "15B"):
            for spec in get_payload_config(label).source_specs:
                peaks[spec.name.lower()] = (spec.name, list(spec.peak_energies_keV))
        specs, background, issues = [], {}, []
        for event in sorted(directory.glob("*.event.dat")):
            prefix = re.split(r"[_-]", event.name, maxsplit=1)[0].lower()
            if prefix not in peaks and prefix not in ("bkg", "background"):
                issues.append(f"不能从源文件 {event.name} 识别能线；请提供 Na22/Cs137/Co60 名称或显式源规格")
                continue
            exposure = re.search(r"(?:_|-)(\d+)\s*([mh])(?:_|-)", event.name, re.I)
            if not exposure:
                issues.append(f"源文件 {event.name} 缺少曝光时间；请用 _5m_ / _1h_ 命名并配置曝光时间")
                continue
            minutes = int(exposure.group(1)) * (60 if exposure.group(2).lower() == "h" else 1)

            def signature(name):
                stem = re.sub(r"(?:[_-]ecu)?[_-]\d+\.(?:event\.dat|hk)$", "", name, flags=re.I)
                return re.sub(r"[^a-z0-9]", "", stem.lower())

            matching = [
                path for path in directory.glob("*.hk") if signature(path.name) == signature(event.name)
            ]
            if len(matching) != 1:
                issues.append(f"源文件 {event.name} 未能唯一匹配 hk 文件")
                continue
            if prefix in ("bkg", "background"):
                background = {
                    "bkg_event_file": event.name,
                    "bkg_hk_file": matching[0].name,
                    "bkg_exposure_minutes": minutes,
                }
            else:
                name, energies = peaks[prefix]
                specs.append(SourceSpec(name, event.name, matching[0].name, minutes, energies))
        return specs, background, issues

    def _base_config(self, payload: str):
        validate_payload(payload)
        profile = self.dataset.get("profile") if self.dataset else payload
        try:
            cfg = get_payload_config(profile)
        except ValueError:
            cfg = PayloadConfig(payload, payload, Path("."), Path("."), Path("."), Path("."))
        raw = (
            Path(self.dataset["raw_dir"])
            if self.dataset and self.dataset.get("raw_dir")
            else self.workspace / "runs" / "_no_raw_data"
            if self.dataset
            else self.workspace / "14、15B标定数据" / cfg.payload_id
        )
        return cfg, raw

    def _config(self, payload: str, *, observations=None):
        cfg, raw = self._base_config(payload)
        profile = self.dataset.get("profile") if self.dataset else payload
        if self.acquisition:
            found = observations if observations is not None else discover(raw, self.acquisition)
            specs = [
                SourceSpec(
                    row["source"],
                    row["event"],
                    row["hk"],
                    row["exposure_minutes"],
                    list(row["peak_energies_keV"]),
                )
                for row in found["rows"]
                if row["kind"] == "source"
            ]
            backgrounds = [row for row in found["rows"] if row["kind"] == "background"]
            background = backgrounds[0] if backgrounds else None
            return replace(
                cfg,
                payload_name=payload,
                raw_root=raw,
                tb_raw_dir=raw,
                ec_xray_dir=raw,
                ec_source_dir=raw,
                source_specs=specs,
                bkg_event_file=background["event"] if background else "",
                bkg_hk_file=background["hk"] if background else "",
                bkg_exposure_minutes=background["exposure_minutes"] if background else 0,
            )
        xray = raw / (cfg.ec_xray_dir.name if cfg.ec_xray_dir.name in ("X", "X_new") else "X_new")
        if not xray.is_dir():
            xray = next((raw / folder for folder in ("X_new", "X") if (raw / folder).is_dir()), raw / "X")
        specs, background, _issues = self._source_config(raw)
        # A product-only dataset can still apply TB to already parsed source
        # spectra. Their recorded exposure/energies are authoritative inputs.
        manifest_path = self.historical_layer(payload, "EC_L0") / "json" / "L0_manifest.json"
        if not specs and manifest_path.is_file():
            manifest = read_json(manifest_path)
            for record in manifest.get("outputs", []):
                if record.get("data_type") == "source" and record.get("peak_energies_keV"):
                    specs.append(
                        SourceSpec(
                            record["source_name"],
                            "",
                            "",
                            int(record.get("exposure_minutes", 0)),
                            list(record["peak_energies_keV"]),
                        )
                    )
        return replace(
            cfg,
            payload_name=payload,
            raw_root=raw,
            tb_raw_dir=raw / "Temp_Vbias",
            ec_xray_dir=xray,
            ec_source_dir=raw / "source",
            source_specs=specs
            or (copy.deepcopy(cfg.source_specs) if not self.dataset or profile in ("14B", "15B") else []),
            **background,
        )

    def _layer_dir(self, product_root: Path, payload: str, layer: str) -> Path:
        kind, level = layer.split("_")
        return Path(product_root) / payload / kind.lower() / level

    def _assert_output_root(self, product_root: Path) -> None:
        target = Path(product_root).resolve()
        runs = (self.workspace / "runs").resolve()
        if not target.is_relative_to(runs) or target == runs:
            raise ValueError("New computations must write inside workspace runs/<run_id>/")

    def inventory(self, payload: str) -> dict:
        input_scan = self.observations(payload)
        cfg = self._config(payload, observations=input_scan)
        tb = TBL0Processor(payload, self.history)
        ec = ECL0Processor(payload, self.history)
        tb_pairs = (
            []
            if self.acquisition
            else [
                (event, hk)
                for event, hk in tb.match_tb_pairs(cfg.tb_raw_dir)
                if event.stat().st_size and hk.stat().st_size
            ]
        )
        ec_pairs = (
            []
            if self.acquisition
            else [
                (event, hk, energy)
                for event, hk, energy in ec.match_xray_file_pairs(cfg.ec_xray_dir)
                if event.stat().st_size and hk.stat().st_size
            ]
        )
        tb_samples = [
            {
                "stem": event.name.removesuffix(".event.dat"),
                "event": str(event),
                "hk": str(hk),
                "bytes": event.stat().st_size,
            }
            for event, hk in tb_pairs
        ]
        ec_samples = [
            {
                "stem": f"{energy}_{event.name.split('-')[1]}",
                "event": str(event),
                "hk": str(hk),
                "energy": energy,
                "bytes": event.stat().st_size,
            }
            for event, hk, energy in ec_pairs
        ]
        if self.acquisition:

            def sample(row):
                return {
                    **row,
                    "bytes": Path(row["event"]).stat().st_size,
                    "energy": f"{row['energy_keV']:g}keV" if row["kind"] == "xray" else None,
                }

            tb_samples = [sample(row) for row in input_scan["rows"] if row["kind"] == "tb"]
            ec_samples = [sample(row) for row in input_scan["rows"] if row["kind"] == "xray"]
            tb_pairs, ec_pairs = tb_samples, ec_samples
        # Choose a complete four-channel energy, never the zero-byte .cut file.
        energy_groups = {}
        for item in ec_samples:
            energy_groups.setdefault(item["energy"], []).append(item)
        complete = [
            items
            for items in energy_groups.values()
            if {int(item["stem"].rsplit("_ch", 1)[1]) for item in items} == {0, 1, 2, 3}
        ]
        chosen = min(complete, key=lambda items: sum(item["bytes"] for item in items)) if complete else []
        quality_stems = set()
        for item in tb_samples:
            l1_path = self.historical_layer(payload, "TB_L1") / "json" / f"{item['stem']}_fit_results.l1.json"
            if l1_path.exists():
                records = read_json(l1_path)
                if (
                    isinstance(records, list)
                    and len(records) == 4
                    and all(record.get("rsquared", 0) >= 0.9 for record in records)
                ):
                    quality_stems.add(item["stem"])
        candidates = [item for item in tb_samples if item["stem"] in quality_stems] or tb_samples
        representative_tb = min(candidates, key=lambda item: item["bytes"])["stem"] if candidates else None
        historical = {}
        for layer in LAYERS:
            directory = self.historical_layer(payload, layer)
            files = [path for path in directory.rglob("*") if path.is_file()]
            historical[layer] = {
                "files": len(files),
                **{
                    kind: sum(path.parent == directory / kind for path in files)
                    for kind in ("json", "parquet", "figures")
                },
                "manifest": str(directory / "json" / f"{layer.split('_')[1]}_manifest.json"),
            }
        return {
            "payload": payload,
            "dataset": self.dataset,
            "raw": {
                "tb_pairs": len(tb_pairs),
                "ec_xray_pairs": len(ec_pairs),
                "source_files": len(cfg.source_specs),
                "tb_samples": tb_samples,
                "ec_samples": ec_samples,
                "issues": input_scan["errors"] if input_scan else self._source_config(cfg.raw_root)[2],
                "reading_preview": input_scan,
            },
            "historical": historical,
            "representative": {
                "tb_stems": [representative_tb] if representative_tb else [],
                "ec_stems": [item["stem"] for item in chosen],
                "ec_energies": [chosen[0]["energy"]] if chosen else [],
            },
        }

    def points(self, payload: str, product_root: Path) -> list:
        directory = self._layer_dir(product_root, payload, "TB_L1") / "json"
        l0_files = self.tb_files(payload, product_root)
        rows = []
        for path in sorted(directory.glob("*_fit_results.l1.json")):
            stem = path.name.removesuffix("_fit_results.l1.json")
            records = read_json(path)
            if not isinstance(records, list):
                continue
            l0_path = l0_files.get(stem)
            seen = set()
            for record in records:
                channel = record.get("channel")
                if channel not in range(4) or channel in seen:
                    continue
                seen.add(channel)
                rows.append(
                    {
                        **record,
                        "id": f"{payload}:{stem}:{channel}",
                        "stem": stem,
                        "payload": payload,
                        "gain": record.get("center"),
                        "l1_path": str(path),
                        "l0_path": str(l0_path) if l0_path else None,
                        "fit_status": "failed" if unavailable_fit(record) else "suspect"
                        if quality_warnings(record, min_rsquared=TBL2Processor.MIN_RSQUARED) else "fitted",
                        "quality_warnings": quality_warnings(record, min_rsquared=TBL2Processor.MIN_RSQUARED),
                        "quality_policy": "warning_only",
                    }
                )
        existing = {(row["stem"], row["channel"]) for row in rows}
        for stem, path in l0_files.items():
            if path is None or all((stem, channel) in existing for channel in range(4)):
                continue
            metadata = read_parquet_metadata(path, "hk_data") or {}
            acquisition = read_parquet_metadata(path, "acquisition") or {}
            histogram = self._tb_histogram(None, acquisition)
            for channel in range(4):
                if (stem, channel) in existing:
                    continue
                values = {}
                for key in ("temp", "temp_err", "bias", "bias_err"):
                    value = metadata.get(key)
                    if isinstance(value, list):
                        value = value[channel] if channel < len(value) else None
                    elif isinstance(value, dict):
                        value = value.get(str(channel))
                    values[key] = value
                rows.append(
                    {
                        **values,
                        "id": f"{payload}:{stem}:{channel}",
                        "stem": stem,
                        "payload": payload,
                        "channel": channel,
                        "center": None,
                        "gain": None,
                        "center_err": None,
                        "fit_status": "not_fitted",
                        "used": False,
                        "fit_quality": {"accepted": False, "status": "not_fitted"},
                        "l1_path": None,
                        "l0_path": str(path),
                        "window_lo": histogram["min"],
                        "window_hi": histogram["max"],
                        "histogram_config": histogram,
                    }
                )
        for row in rows:
            if reason := unsupported_spectral_model(self.detector_payload(payload), row["channel"]):
                row.update(unsupported_spectral_model=reason, fit_status="model_pending", used=False,
                           center=None, gain=None, fit_quality={"accepted": False, "status": "model_pending"})
        return _clean(rows)

    def tb_files(self, payload: str, product_root: Path) -> dict[str, Path]:
        """Build a request-local L0 overlay once, rather than stat four channels."""
        files = {}
        for directory in (
            self._layer_dir(product_root, payload, "TB_L0") / "parquet",
            self.historical_layer(payload, "TB_L0") / "parquet",
        ):
            for path in sorted(directory.glob("*.parquet")):
                if path.is_file():
                    files.setdefault(_stem(path.name), path)
        return files

    @staticmethod
    def _spectrum_input(path: Path, field: str):
        """Read only plot columns and metadata from the same fresh Parquet file."""
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(path)
        columns = ["channel", field]
        if "crc_check" in parquet.schema_arrow.names:
            columns.append("crc_check")
        frame = parquet.read(columns=columns).to_pandas()
        metadata = {}
        for key in ("hk_data", "acquisition", "tb_correction"):
            raw = (parquet.schema_arrow.metadata or {}).get(key.encode("utf-8"))
            try:
                metadata[key] = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeError):
                metadata[key] = {}
        return frame, metadata

    def _tb_histogram(self, record, acquisition):
        return (record or {}).get("histogram_config") or tb_histogram_config(
            acquisition if acquisition.get("fitting") else self.acquisition
        )

    def _find_l0(self, payload: str, product_root: Path, stem: str) -> Path | None:
        for directory in (
            self._layer_dir(product_root, payload, "TB_L0") / "parquet",
            self.historical_layer(payload, "TB_L0") / "parquet",
        ):
            for name in (f"{stem}.l0.parquet", f"{stem}.parquet"):
                path = directory / name
                if path.is_file():
                    return path
        return None

    def spectrum(
        self, payload: str, product_root: Path, stem: str, channel: int | None = None, layer: str = "TB_L1"
    ) -> dict:
        # Both stable point IDs and the original stem/channel API are accepted.
        if channel in ("TB_L0", "TB_L1"):
            layer, channel = channel, None
        if ":" in stem:
            point_payload, stem, point_channel = stem.rsplit(":", 2)
            if point_payload != payload:
                raise ValueError("工况点不属于当前载荷")
            channel = int(point_channel)
        if channel not in range(4):
            raise ValueError("Channel must be 0, 1, 2 or 3")
        if layer not in ("TB_L0", "TB_L1"):
            raise ValueError("TB spectrum layer must be TB_L0 or TB_L1")
        path = self._find_l0(payload, product_root, stem)
        if path is None:
            raise FileNotFoundError(f"No real L0 spectrum available for {stem}")
        l1_path = self._layer_dir(product_root, payload, "TB_L1") / "json" / f"{stem}_fit_results.l1.json"
        records = read_json(l1_path) if l1_path.is_file() else []
        record = (
            next((row for row in records if row.get("channel") == channel), None)
            if isinstance(records, list)
            else None
        )
        if record is not None:
            record = {
                **record,
                "id": f"{payload}:{stem}:{channel}",
                "stem": stem,
                "payload": payload,
                "gain": record.get("center"),
                "l1_path": str(l1_path),
                "l0_path": str(path),
                "fit_status": "failed" if unavailable_fit(record) else "suspect"
                if quality_warnings(record, min_rsquared=TBL2Processor.MIN_RSQUARED) else "fitted",
                "quality_warnings": quality_warnings(record, min_rsquared=TBL2Processor.MIN_RSQUARED),
                "quality_policy": "warning_only",
            }
        frame, metadata = self._spectrum_input(path, "amp")
        input_quality = {"crc_available": "crc_check" in frame, "input_events": len(frame)}
        if layer != "TB_L0":
            frame, input_quality = validated_events(frame)
        amp = frame.loc[frame["channel"] == channel, "amp"].to_numpy(dtype=float)
        if not amp.size:
            raise ValueError(f"L0 has no events for {stem} ch{channel}")
        acquisition = metadata["acquisition"] or {}
        histogram = self._tb_histogram(record, acquisition)
        # Use the same binning as the saved fit. Historical one-ADC displays
        # retain their compact viewport while configured spectra keep all bins.
        lo, upper, width = histogram["min"], histogram["max"], histogram["bin_width"]
        if (
            not (record or {}).get("histogram_config")
            and not acquisition.get("fitting")
            and not (self.acquisition or {}).get("fitting")
        ):
            upper = min(upper, int(max(420, np.percentile(amp, 99.8) + 40)))
        hist, edges = np.histogram(amp, bins=np.arange(lo, upper + width, width))
        x = (edges[:-1] + edges[1:]) / 2
        available = {}
        for directory, source in (
            (self._layer_dir(product_root, payload, "TB_L0") / "parquet", "run"),
            (self.historical_layer(payload, "TB_L0") / "parquet", "historical"),
        ):
            for item in sorted(directory.glob("*.parquet")):
                item_stem = _stem(item.name)
                available.setdefault(item_stem, {"stem": item_stem, "source": source, "path": str(item)})
        common = {
            "id": f"{payload}:{stem}:{channel}",
            "stem": stem,
            "channel": channel,
            "x": x,
            "y": hist,
            "n_events": len(amp),
            "source_path": str(path),
            "source": "run" if Path(product_root).resolve() in path.resolve().parents else "historical",
            "files": list(available.values()),
            "layer": layer,
            "hk_data": metadata["hk_data"],
            "histogram_config": histogram,
            "input_quality": input_quality,
            "unsupported_spectral_model": unsupported_spectral_model(self.detector_payload(payload), channel),
            "window_bounds": {"min": histogram["min"], "max": histogram["max"], "minimum_width": 8 * width},
            "default_window": {
                "lo": (record or {}).get("window_lo", histogram["min"]),
                "hi": (record or {}).get("window_hi", histogram["max"]),
            },
        }
        if common["unsupported_spectral_model"] and layer == "TB_L1":
            return _clean({**common, "fit": None, "curve": None})
        if layer == "TB_L0" or record is None:
            return _clean({**common, "fit": record, "curve": None})
        if input_quality.get("excluded_crc_count", 0) and not record.get("input_quality"):
            return _clean(
                {
                    **common,
                    "fit": record,
                    "curve": None,
                    "input_note": "CRC 失败事件已排除；历史拟合未记录 CRC 输入筛选，请重拟合以建立对应关系。",
                }
            )
        fit_x = x[(x >= record["window_lo"]) & (x <= record["window_hi"])]
        gaussian = GaussianModel().eval(
            x=fit_x, amplitude=record["amplitude"], center=record["center"], sigma=record["sigma"]
        )
        mode = record["fit_mode"]
        background = np.zeros_like(fit_x)
        if mode == "lin + gaus":
            background = LinearModel().eval(x=fit_x, slope=record["slope"], intercept=record["intercept"])
        elif mode == "exp + gaus":
            background = ExponentialModel().eval(
                x=fit_x, amplitude=record["exp_amplitude"], decay=record["exp_decay"]
            )
        elif mode == "quad + gaus":
            background = QuadraticModel().eval(
                x=fit_x, a=record["quad_a"], b=record["quad_b"], c=record["quad_c"]
            )
        return _clean(
            {
                **common,
                "fit": record,
                "curve": {
                    "x": fit_x,
                    "y": gaussian + background,
                    "gaussian": gaussian,
                    "background": background,
                },
            }
        )

    def surface(self, payload: str, product_root: Path, channel: int) -> dict:
        if channel not in range(4):
            raise ValueError("Channel must be 0, 1, 2 or 3")
        paths = sorted((self._layer_dir(product_root, payload, "TB_L2") / "json").glob("*_TB*.json"))
        rows = [point for point in self.points(payload, product_root) if point["channel"] == channel]
        selected_path, params = None, None
        for path in reversed(paths):
            try:
                tb_package(path, required_channels=photopeak_channels(self.detector_payload(payload)))
                records = read_json(path)
                if isinstance(records, dict):
                    records = records["fit_params"]
                params = next(
                    record
                    for index, record in enumerate(records)
                    if int(record.get("channel", index)) == channel
                )
                selected_path = path
                break
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                continue
        if selected_path is None:
            return {"params": None, "points": rows, "grid": None, "diagnostics": {}, "version": None}
        evaluator = ECL1Processor(payload, product_root, response_payload=self.detector_payload(payload))._gain_temp_bias_model
        manifest_path = self._layer_dir(product_root, payload, "TB_L2") / "json" / "L2_manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        excluded = set(manifest.get("excluded_points", []))
        included = set(manifest["included_points"]) if "included_points" in manifest else None
        legacy = manifest_path.exists() and manifest.get("quality_policy") != "warning_only"
        finite = []
        for point in rows:
            if point.get("temp") is None or point.get("bias") is None or point.get("center") is None:
                continue
            prediction = float(evaluator(np.array([point["temp"]]), np.array([point["bias"]]), params)[0])
            point["predicted"] = prediction
            point["residual"] = point["center"] - prediction
            point["excluded"] = point["id"] in excluded
            point["used"] = (
                not point["excluded"]
                and usable_peak(point)
                and (point["id"] in included if included is not None else
                     not legacy or ((point.get("rsquared") or 0) >= TBL2Processor.MIN_RSQUARED
                                    and (point.get("fit_quality") or {}).get("accepted") is not False))
            )
            point["draft_used"] = not point["excluded"] and usable_peak(point)
            point["quality_warnings"] = quality_warnings(point, min_rsquared=TBL2Processor.MIN_RSQUARED)
            point["suspect"] = bool(point["quality_warnings"])
            finite.append(point)
        if not finite:
            return {
                "params": params,
                "points": rows,
                "grid": None,
                "diagnostics": {},
                "version": selected_path.name,
            }
        temperatures = np.linspace(min(row["temp"] for row in finite), max(row["temp"] for row in finite), 24)
        biases = np.linspace(min(row["bias"] for row in finite), max(row["bias"] for row in finite), 24)
        tt, vv = np.meshgrid(temperatures, biases)
        gain = evaluator(tt, vv, params)
        used = [row for row in finite if row["used"]]
        residuals = np.array([row["residual"] for row in used])
        gains = np.array([row["center"] for row in used])
        return _clean(
            {
                "params": params,
                "points": rows,
                "version": selected_path.name,
                "grid": {"temp": temperatures, "bias": biases, "gain": gain},
                "diagnostics": {
                    "n_points": len(used),
                    "n_excluded": len(finite) - len(used),
                    "n_draft_points": sum(row["draft_used"] for row in finite),
                    "n_suspect": sum(row["suspect"] for row in used),
                    "rmse": np.sqrt(np.mean(residuals**2)) if used else None,
                    "mape_percent": np.mean(np.abs(residuals / gains)) * 100 if used else None,
                    "chisquare": params.get("chisquare"),
                    "dof": max(len(used) - 5, 1),
                    "chi2_reduced": params.get("chisquare", 0) / max(len(used) - 5, 1),
                },
            }
        )

    def ec_files(self, payload: str, product_root: Path, layer: str, channel: int) -> list[dict]:
        """List parsed EC products without loading a spectrum or scanning raw data."""
        if layer not in ("EC_L0", "EC_L1") or channel not in range(4):
            raise ValueError("EC spectrum requires EC_L0/EC_L1 and channel 0..3")
        files = {}
        for directory, source in (
            (self._layer_dir(product_root, payload, layer) / "parquet", "run"),
            (self.historical_layer(payload, layer) / "parquet", "historical"),
        ):
            for candidate in sorted(directory.glob("*.parquet")):
                item_stem = candidate.name.removesuffix(".l0.parquet").removesuffix("_corr.l1.parquet")
                match = re.search(r"_ch(\d+)", item_stem)
                if match and int(match.group(1)) != channel:
                    continue
                files.setdefault(
                    item_stem,
                    {
                        "stem": item_stem,
                        "path": str(candidate),
                        "source": source,
                        "channel": channel,
                        "fit_status": "parsed",
                    },
                )
        return list(files.values())

    def ec_spectrum(
        self,
        payload: str,
        product_root: Path,
        layer: str | int,
        channel: int | str,
        stem: str | None = None,
        *,
        preferred_stems: list[str] | None = None,
    ) -> dict:
        """Read an actual raw/corrected EC spectrum without triggering a fit."""
        if isinstance(layer, int) and isinstance(channel, str):
            layer, channel = channel, layer
        files = {item["stem"]: item for item in self.ec_files(payload, product_root, layer, channel)}
        preferred = stem or next(
            (item for item in preferred_stems or [] if item.endswith(f"_ch{channel}")), None
        )
        if stem and stem not in files:
            raise FileNotFoundError(f"No real {layer} spectrum for {stem} ch{channel}")
        if not files:
            raise FileNotFoundError(f"No real {layer} spectrum for {payload} ch{channel}")
        selected = files.get(preferred) or next(
            (item for item in files.values() if "bkg" not in item["stem"]), next(iter(files.values()))
        )
        path, source = Path(selected["path"]), selected["source"]
        field = "amp" if layer == "EC_L0" else "amp_corr"
        frame, metadata = self._spectrum_input(path, field)
        values = frame.loc[frame["channel"] == channel, field].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if not values.size:
            raise ValueError(f"No finite {field} events in {path.name} ch{channel}")
        lo = min(0.0, float(np.percentile(values, 0.2)))
        hi = max(lo + 4.0, float(np.percentile(values, 99.8)))
        energy = path.name.split("_ch", 1)[0]
        profile = self.dataset.get("profile") if self.dataset else payload
        window = (
            ECL2Processor(profile or payload, product_root)
            ._load_xray_windows()
            .get(energy, {})
            .get(str(channel))
        )
        if window:
            # Focus the display on the existing scientific review window;
            # overflow events remain counted and explicitly reported below.
            lo = 0.0
            hi = min(hi, max(160.0, float(window["hi"]) * 1.5))
        width = max(4.0, (hi - lo) / 400)
        counts, edges = np.histogram(values, bins=np.arange(lo, hi + width, width))
        correction = metadata["tb_correction"] if layer == "EC_L1" else {}
        return _clean(
            {
                "x": (edges[:-1] + edges[1:]) / 2,
                "y": counts,
                "stem": path.name.removesuffix(".l0.parquet").removesuffix("_corr.l1.parquet"),
                "channel": channel,
                "files": list(files.values()),
                "layer": layer,
                "n_events": len(values),
                "source": source,
                "source_path": str(path),
                "path": str(path),
                "field": field,
                "tb_corrfile": correction.get("tb_corrfile"),
                "display_range": [lo, hi],
                "events_in_display": int(counts.sum()),
                "events_outside_display": len(values) - int(counts.sum()),
                "display_note": "Histogram is focused on the configured EC review window; events outside the shown ADC range are counted separately",
            }
        )

    def _hydrate(
        self, payload: str, product_root: Path, layer: str, stems: list[str] | None = None
    ) -> list[str]:
        source = self.historical_layer(payload, layer) / "parquet"
        target = self._layer_dir(product_root, payload, layer) / "parquet"
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for path in sorted(source.glob("*.parquet")):
            if stems is not None and _stem(path.name) not in stems:
                continue
            destination = target / path.name
            if not destination.exists():
                shutil.copy2(path, destination)
                copied.append(str(path))
        return copied

    def _windows(self, payload: str, config: dict, target: Path) -> Path:
        histogram = tb_histogram_config(self.acquisition)
        profile = self.dataset.get("profile") if self.dataset else payload
        path = self.resources / f"legacy_fit_windows_config_{profile}.json"
        windows = copy.deepcopy(read_json(path)) if path.exists() else {}
        # Older hand-picked windows used '-' while 14B raw files use '_'.
        for key, value in list(windows.items()):
            windows.setdefault(key.replace("-", "_"), copy.deepcopy(value))
            windows.setdefault(key.replace("_", "-"), copy.deepcopy(value))
        # A run starts from the exact saved historical windows wherever present.
        directory = self._layer_dir(target, payload, "TB_L1") / "json"
        for fit_path in sorted(directory.glob("*_fit_results.l1.json")):
            records = read_json(fit_path)
            if not isinstance(records, list):
                continue
            stem = fit_path.name.removesuffix("_fit_results.l1.json")
            for record in records:
                if (not isinstance(record, dict) or record.get("channel") not in range(4)
                        or "window_lo" not in record or "window_hi" not in record):
                    continue
                windows.setdefault(stem, {})[str(record["channel"])] = {
                    "lo": record["window_lo"], "hi": record["window_hi"], "manual": True,
                }
        for stem, channels in config.get("windows", {}).items():
            for channel, window in channels.items():
                lo, hi = float(window["lo"]), float(window["hi"])
                if (
                    int(channel) not in range(4)
                    or not all(map(math.isfinite, (lo, hi)))
                    or lo < histogram["min"]
                    or hi > histogram["max"]
                    or hi - lo < 8 * histogram["bin_width"]
                ):
                    raise ValueError(
                        f"Invalid fit window for {stem} ch{channel}: require {histogram['min']} <= lo < hi <= {histogram['max']} and at least 8 bins ({8 * histogram['bin_width']:g} ADC)"
                    )
                windows.setdefault(_stem(stem), {})[str(channel)] = {"lo": lo, "hi": hi, "manual": True}
        output = target.parent / "workbench-fit-windows.json"
        write_json(output, windows)
        return output

    def _verify_acquisition_inputs(self, payload, product_root, layer, selection):
        if not self.acquisition:
            return
        paths = [
            path
            for path in (self._layer_dir(product_root, payload, layer) / "parquet").glob("*.parquet")
            if selection is None or _stem(path.name) in selection
        ]
        if not paths or (selection is not None and len(paths) < len(selection)):
            raise ValueError(f"{layer} 输入不完整，请先按当前读取配置重新运行上游层")
        for path in paths:
            metadata = read_parquet_metadata(path, "acquisition")
            if metadata.get("profile_sha256") != self.acquisition.get("_sha256"):
                raise ValueError(f"{path.name} 使用旧读取标准或缺少配置来源；请先重新运行 {layer}")

    def execute(
        self, payload: str, layer: str, product_root: Path, config: dict, tb_path: Path | None = None
    ) -> dict:
        if layer not in LAYERS:
            raise ValueError(f"Unknown layer: {layer}")
        reason = self.unavailable_reason(layer)
        if reason:
            raise ValueError(reason)
        self._assert_output_root(product_root)
        product_root = Path(product_root)
        if layer in ("TB_L0", "EC_L0") and self.dataset and not self.dataset.get("raw_dir"):
            raise ValueError("当前数据集只载入了产品目录；请先选择对应原始数据目录，再执行 L0 解析")
        layout = DataLayout(product_root, payload)
        representative = bool(config.get("representative", True))
        inventory = self.inventory(payload)
        cfg = self._config(payload, observations=inventory["raw"]["reading_preview"])
        selection = config.get("only")
        if selection is not None:
            selection = [_stem(item) for item in selection]
        elif representative:
            selection = inventory["representative"]["tb_stems" if layer.startswith("TB_") else "ec_stems"]
        if self.acquisition and selection is None and layer not in ("TB_L2", "EC_L3"):
            rows = inventory["raw"]["reading_preview"]["rows"]
            selection = [row["stem"] for row in rows if (row["kind"] == "tb") == layer.startswith("TB_")]
        if (config.get("ec_point_id") or config.get("energy_key")) and layer in ("EC_L1", "EC_L2"):
            if config.get("ec_point_id"):
                pieces = str(config["ec_point_id"]).split(":")
                if len(pieces) != 5 or pieces[0] != payload or pieces[1] != "ec":
                    raise ValueError("EC 能峰点不属于当前载荷")
                energy_key = pieces[2]
            else:
                energy_key = config["energy_key"]
            input_layer = "EC_L0" if layer == "EC_L1" else "EC_L1"
            sources = (
                self._layer_dir(product_root, payload, input_layer) / "parquet",
                self.historical_layer(payload, input_layer) / "parquet",
            )
            selection = sorted(
                {
                    _stem(path.name)
                    for directory in sources
                    for path in directory.glob("*.parquet")
                    if _stem(path.name).startswith(
                        energy_key + ("_" if energy_key.startswith("src_") else "_ch")
                    )
                }
            )
            if energy_key.startswith("src_") and config.get("only"):
                selection = [stem for stem in selection if stem in config["only"]]
            if not selection:
                raise ValueError(f"选中能峰 {energy_key} 没有真实 {input_layer} 输入，请先生成或载入相应产品")
        hydrated = []
        warnings = []
        if layer == "TB_L0":
            processor = (
                TBL0Processor(payload, product_root, input_profile=self.acquisition)
                if self.acquisition
                else TBL0Processor(payload, product_root)
            )
            processor.root_dir = self.workspace
            processor.raw_root = cfg.raw_root
            result = processor.process(cfg.tb_raw_dir, only_stems=selection)
        elif layer == "TB_L1":
            hydrated = self._hydrate(payload, product_root, "TB_L0", selection)
            self._verify_acquisition_inputs(payload, product_root, "TB_L0", selection)
            windows = self._windows(payload, config, product_root)
            result = TBL1Processor(payload, product_root, response_payload=self.detector_payload(payload)).process(
                layout.get_tb_l0_dir(),
                {
                    **TB_PROCESS_PARAMS,
                    "fit_windows_config": str(windows),
                    "automatic_window": config.get("automatic_window", False),
                    "peak_detection": config.get("peak_detection", {}),
                    "fit_channels": config.get("fit_channels", [0, 1, 2, 3]),
                    "peak_context_l0_paths": [str(path) for path in self.tb_files(payload, product_root).values()
                                              if path is not None],
                },
                only_stems=[f"{stem}.l0" for stem in selection] if selection is not None else None,
            )
        elif layer == "TB_L2":
            if self.acquisition:
                allowed = {
                    row["stem"] for row in inventory["raw"]["reading_preview"]["rows"] if row["kind"] == "tb"
                }
                for path in (layout.get_tb_l1_dir() / "json").glob("*_fit_results.l1.json"):
                    if path.name.removesuffix("_fit_results.l1.json") not in allowed:
                        path.unlink()
            (layout.get_tb_l0_dir() / "parquet").mkdir(parents=True, exist_ok=True)
            result = TBL2Processor(payload, product_root, response_payload=self.detector_payload(payload)).process(
                layout.get_tb_l1_dir(), layout.get_tb_l0_dir(), excluded_points=config.get("excluded", [])
            )
            selection = None  # A surface always uses the full current L1 snapshot.
        elif layer == "EC_L0":
            issues = (
                inventory["raw"]["reading_preview"]["errors"]
                if self.acquisition
                else self._source_config(cfg.raw_root)[2]
            )
            if issues and not representative and selection is None:
                raise ValueError("源谱输入配置不完整: " + "; ".join(issues))
            processor = (
                ECL0Processor(payload, product_root, input_profile=self.acquisition)
                if self.acquisition
                else ECL0Processor(payload, product_root)
            )
            processor.root_dir = self.workspace
            if self.acquisition:
                if issues:
                    raise ValueError("读取配置的文件配对存在错误: " + "; ".join(issues))
                result = processor.process(cfg, only_stems=selection)
            elif selection is not None and any(item.startswith("src_") for item in selection):
                source_stems = {f"src_{spec.name}_{spec.exposure_minutes}m" for spec in cfg.source_specs}
                if cfg.bkg_event_file and cfg.bkg_exposure_minutes:
                    source_stems.add(f"src_bkg_{cfg.bkg_exposure_minutes}m")
                unknown_sources = [
                    item for item in selection if item.startswith("src_") and item not in source_stems
                ]
                if unknown_sources:
                    raise ValueError(
                        "所选源谱缺少匹配的原始文件/曝光/能线规格: " + ", ".join(unknown_sources)
                    )
                # The existing public processor selects X files, while its
                # source handler owns the actual source/background parsing.
                # Reuse both, with an explicit source subset, for a chosen file.
                result = processor.process(
                    cfg, only_stems=[item for item in selection if not item.startswith("src_")]
                )
                source_names = {
                    match.group(1)
                    for item in selection
                    if (match := re.match(r"^src_([A-Za-z]+\d*)_\d+m$", item))
                }
                processor._process_sources(
                    replace(
                        cfg, source_specs=[spec for spec in cfg.source_specs if spec.name in source_names]
                    ),
                    result,
                )
                write_json(layout.get_ec_l0_json("L0_manifest.json"), result)
            else:
                result = processor.process(cfg, only_stems=selection)
        elif layer == "EC_L1":
            if tb_path is None or not Path(tb_path).is_file():
                raise ValueError(
                    "EC L1 requires an explicitly selected TB L2 JSON version; select an existing package or compute TB L2"
                )
            if selection is not None and any(
                item.startswith("src_") and not item.startswith("src_bkg_") for item in selection
            ):
                backgrounds = {
                    _stem(path.name)
                    for directory in (
                        self._layer_dir(product_root, payload, "EC_L0") / "parquet",
                        self.historical_layer(payload, "EC_L0") / "parquet",
                    )
                    for path in directory.glob("src_bkg_*.l0.parquet")
                    if not self.acquisition
                    or _stem(path.name) == f"src_bkg_{float(cfg.bkg_exposure_minutes):g}m"
                }
                selection = sorted(set(selection) | backgrounds)
            hydrated = self._hydrate(payload, product_root, "EC_L0", selection)
            self._verify_acquisition_inputs(payload, product_root, "EC_L0", selection)
            result = ECL1Processor(payload, product_root, response_payload=self.detector_payload(payload)).process(cfg, Path(tb_path), only_stems=selection)
        elif layer == "EC_L2":
            if tb_path is None or not Path(tb_path).is_file():
                raise ValueError("EC L2 requires the explicitly selected TB L2 dependency")
            if selection is not None and any(
                item.startswith("src_") and not item.startswith("src_bkg_") for item in selection
            ):
                backgrounds = {
                    _stem(path.name)
                    for directory in (
                        self._layer_dir(product_root, payload, "EC_L1") / "parquet",
                        self.historical_layer(payload, "EC_L1") / "parquet",
                    )
                    for path in directory.glob("src_bkg_*_corr.l1.parquet")
                    if not self.acquisition
                    or _stem(path.name) == f"src_bkg_{float(cfg.bkg_exposure_minutes):g}m"
                }
                selection = sorted(set(selection) | backgrounds)
            hydrated = self._hydrate(payload, product_root, "EC_L1", selection)
            self._verify_acquisition_inputs(payload, product_root, "EC_L1", selection)
            selected_files = [
                path
                for path in (layout.get_ec_l1_dir() / "parquet").glob("*_corr.l1.parquet")
                if selection is None or path.name.removesuffix("_corr.l1.parquet") in selection
            ]
            if not selected_files or (selection is not None and len(selected_files) < len(selection)):
                raise ValueError(
                    "EC L1 input is incomplete for this scope; compute EC L1 with the selected TB version first"
                )
            expected = ECL1Processor(payload, product_root, response_payload=self.detector_payload(payload))._load_tb_params(Path(tb_path))
            for path in selected_files:
                actual = read_parquet_metadata(path, "tb_correction").get("tb_params_used", {})
                if any(actual.get(str(ch)) != params for ch, params in expected.items()):
                    raise ValueError(f"Stale TB dependency in {path.name}; rerun EC L1 before EC L2")
            energies = (
                sorted(
                    {
                        re.sub(r"^(src_.+)_\d+(?:\.\d+)?m$", r"\1", stem.rsplit("_ch", 1)[0])
                        for stem in selection
                    }
                )
                if selection is not None
                else None
            )
            processor = ECL2Processor(payload, product_root, response_payload=self.detector_payload(payload))
            profile = self.dataset.get("profile") if self.dataset else payload
            if profile and profile != payload:
                window_source = ECL2Processor(profile, product_root)
                processor._load_xray_windows = window_source._load_xray_windows
                processor._load_source_windows = window_source._load_source_windows
            result = processor.process(
                layout.get_ec_l1_dir(),
                {
                    **EC_PROCESS_PARAMS,
                    **({"bin_width": config["bin_width"]} if "bin_width" in config else {}),
                    "only_energies": energies,
                    "only_stems": selection,
                    "ec_windows": config.get("ec_windows", {}),
                    "ec_peak_sets": config.get("ec_peak_sets", {}),
                    **({"fit_channels": config["fit_channels"]} if "fit_channels" in config else {}),
                    "automatic_window": config.get("automatic_window", False),
                    "peak_detection": config.get("peak_detection", {}),
                    "background_stem": f"src_bkg_{float(cfg.bkg_exposure_minutes):g}m"
                    if cfg.bkg_exposure_minutes
                    else None,
                    "only_point": config.get("ec_point_id"),
                },
            )
        else:
            if self.acquisition:
                allowed = {
                    f"{row['energy_keV']:g}keV" if row["kind"] == "xray" else f"src_{row['source']}"
                    for row in inventory["raw"]["reading_preview"]["rows"]
                    if row["kind"] in ("xray", "source")
                }
                for path in (layout.get_ec_l2_dir() / "json").glob("*_fit_results.l2.json"):
                    if path.name.removesuffix("_fit_results.l2.json") not in allowed:
                        path.unlink()
            result = ECL3Processor(payload, product_root, response_payload=self.detector_payload(payload)).process(
                layout.get_ec_l2_dir(), {**EC_PROCESS_PARAMS, "ec_excluded": config.get("ec_excluded", []),
                                       "ec_peak_sets": config.get("ec_peak_sets", {})}
            )
            selection = None
            if representative:
                warnings.append(
                    "L3 uses the full current L2 snapshot, including historical points outside the representative recomputation scope"
                )

        errors = list(result.get("errors", []))
        warnings.extend(result.get("warnings", []))
        quality = {}
        directory = self._layer_dir(product_root, payload, layer)
        outputs = []
        for item in result.get("outputs", []):
            for key, kind in (("parquet", "parquet"), ("json", "json"), ("figure", "figures")):
                if item.get(key):
                    path = directory / kind / item[key]
                    if path.is_file() and path.stat().st_size:
                        outputs.append(str(path))
                    else:
                        errors.append(f"Processor reported missing output: {path}")
        if result.get("output_json"):
            path = directory / "json" / result["output_json"]
            if path.is_file() and path.stat().st_size:
                outputs.append(str(path))
            else:
                errors.append(f"Processor reported missing output: {path}")
        for name in result.get("output_figures", []):
            path = directory / "figures" / name
            if path.is_file():
                outputs.append(str(path))
        if not outputs:
            errors.append("Processor produced no scientific output for this attempt")
        if layer in ("TB_L0", "EC_L0"):
            import pyarrow.parquet as pq

            crc_files = []
            for path in map(Path, outputs):
                if path.suffix != ".parquet":
                    continue
                table = pq.read_table(path, columns=["crc_check"])
                flags = table.column("crc_check").to_numpy(zero_copy_only=False)
                failed = int(np.count_nonzero(~flags))
                crc_files.append({"file": path.name, "total": len(flags), "failed": failed})
                if failed:
                    warnings.append(
                        f"CRC quality rejection: {path.name} retains {failed}/{len(flags)} failed crc_check flags from the bundled parser; parsing success does not imply schema/quality acceptance"
                    )
            quality = {
                "crc_check": {"accepted": not any(item["failed"] for item in crc_files), "files": crc_files}
            }
        if layer == "TB_L0" and len(result.get("outputs", [])) != result.get("n_files"):
            errors.append("Not every selected raw TB file was parsed successfully")
        if layer == "TB_L1":
            for item in result.get("outputs", []):
                requested = set(result.get("fit_channels", config.get("fit_channels", [0, 1, 2, 3])))
                updated = set(item.get("channels_updated", range(item.get("channels_fitted", 0))))
                if requested - updated and not any(
                    isinstance(error, dict) and error.get("stem") == item["stem"] for error in errors
                ):
                    errors.append({"stem": item["stem"], "error": "Not every requested channel was fitted",
                                   "channels": sorted(requested - updated)})
        if layer == "EC_L2":
            for item in result.get("outputs", []):
                records = read_json(directory / "json" / item["json"])
                if config.get("ec_point_id"):
                    if config["ec_point_id"] not in item.get("points_updated", []):
                        errors.append(f"Requested EC point was not fitted: {config['ec_point_id']}")
                    continue
                expected_channels = set(result.get("fit_channels", [ch for ch in range(4) if not unsupported_spectral_model(self.detector_payload(payload), ch)]))
                updated_channels = set(item.get("channels_updated", [record.get("channel") for record in records if not record.get("placeholder")]))
                if not expected_channels <= updated_channels:
                    errors.append(f"Incomplete EC L2 channels for {item['energy']}")
        if layer == "TB_L2" and any(not params for ch, params in enumerate(result.get("fit_params") or [None] * 4)
                                   if not unsupported_spectral_model(self.detector_payload(payload), ch)):
            errors.append("TB surface fit did not produce all four channels")
        if layer == "EC_L3":
            for channel in range(4):
                if unsupported_spectral_model(self.detector_payload(payload), channel):
                    continue
                record = result.get("calibration_params", {}).get(str(channel))
                if not record or record.get("EC_low") is None:
                    errors.append(f"No valid energy coefficients for channel {channel}")
                elif record.get("resolution_low") is None or record.get("resolution_high") is None:
                    warnings.append(
                        f"Channel {channel}: insufficient usable points for one resolution segment"
                    )
            warnings.append(
                "Existing L3 algorithm fits E(ADC) globally and shares low/high coefficients; wiki describes segmented ADC(E). Scientific direction is preserved and requires review"
            )
        has_partial_output = bool(outputs)
        if layer == "EC_L3":
            has_partial_output = has_partial_output and any(
                params.get("EC_low") is not None for params in result.get("calibration_params", {}).values()
            )
        return _clean(
            {
                "success": not errors,
                "partial": bool(errors and has_partial_output) if layer in ("TB_L1", "EC_L2", "EC_L3") else False,
                "errors": errors,
                "warnings": warnings,
                "layer": layer,
                "quality": quality,
                "payload": payload,
                "outputs": outputs,
                "output_paths": outputs,
                "counts": {
                    "outputs": len(outputs),
                    "processed": result.get(
                        "n_processed", result.get("n_files", len(result.get("outputs", [])))
                    ),
                    **({"files": result.get("n_files", 0),
                        "channels_fitted": result.get("channels_fitted", 0),
                        "channels_failed": result.get("channels_failed", 0)} if layer == "TB_L1" else {}),
                },
                **({"summary": {"files_total": result.get("n_files", 0),
                                 "channels_succeeded": result.get("channels_fitted", 0),
                                 "channels_failed": result.get("channels_failed", 0)}} if layer == "TB_L1" else {}),
                "scope": {"representative": representative, "only": selection},
                "hydrated_historical_inputs": hydrated,
                "tb_dependency": str(tb_path) if layer.startswith("EC_") and tb_path else None,
                "processor": result,
                "processor_class": layer.replace("_", "") + "Processor",
            }
        )
