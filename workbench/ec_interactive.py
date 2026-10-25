"""Inspect EC products using the same histograms and models as the processors.

The reader never fits on a GET and never writes to historical products. Peak
window edits are applied by ECL2Processor; point exclusions by ECL3Processor.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel

from ..common.spectral_models import unsupported_spectral_model
from ..common.utils import read_json, validated_events
from ..ec.processors.L2_processor import BIN_WIDTH, ECL2Processor
from ..ec.processors.L3_processor import ECL3Processor, res_fit
from .read_cache import EC_READ_CACHE, file_fingerprint

if TYPE_CHECKING:
    from .compute import ComputeAdapter


def point_id(payload: str, stem: str, channel: int, energy: float) -> str:
    return f"{payload}:ec:{stem}:{int(channel)}:{float(energy):g}"


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "fiub":
            return value.tolist() if np.isfinite(value).all() else np.where(np.isfinite(value), value, None).tolist()
        return _clean(value.tolist())
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Path):
        return str(value)
    return value


class ECInspector:
    def __init__(self, adapter: ComputeAdapter):
        self.adapter = adapter

    def _layers(self, payload: str, root: Path, layer: str) -> list[Path]:
        current = self.adapter._layer_dir(root, payload, layer)
        history = (
            self.adapter.historical_layer(payload, layer)
            if hasattr(self.adapter, "historical_layer")
            else self.adapter._layer_dir(self.adapter.history, payload, layer)
        )
        return list(dict.fromkeys([current, history]))

    def _files(self, payload: str, root: Path, layer: str, kind: str, glob: str) -> list[Path]:
        # Overlay current files onto read-only history by filename; a partial
        # refit retains the other energies for inspection and L3 provenance.
        files = {}
        for directory in reversed(self._layers(payload, root, layer)):
            files.update({path.name: path for path in (directory / kind).glob(glob)})
        return [files[name] for name in sorted(files)]

    @staticmethod
    def _channel(channel: int) -> int:
        if channel not in range(4):
            raise ValueError("Channel must be 0, 1, 2 or 3")
        return channel

    def _parquet(self, payload: str, root: Path, stem: str, channel: int) -> Path | None:
        pattern = (
            f"{stem}_*_corr.l1.parquet" if stem.startswith("src_") else f"{stem}_ch{channel}_corr.l1.parquet"
        )
        for directory in self._layers(payload, root, "EC_L1"):
            paths = sorted((directory / "parquet").glob(pattern))
            if paths:
                return paths[-1]
        return None

    def _named_parquet(self, payload: str, root: Path, name: str) -> Path | None:
        if Path(name).name != name:
            raise ValueError("EC product filename must not contain a path")
        for directory in self._layers(payload, root, "EC_L1"):
            path = directory / "parquet" / name
            if path.is_file():
                return path
        return None

    @staticmethod
    def _input(path, fingerprint):
        def load():
            import pyarrow.parquet as pq

            names = pq.read_schema(path).names
            columns = [name for name in ("channel", "amp_corr", "utc", "crc_check") if name in names]
            frame = pq.read_table(path, columns=columns).to_pandas()
            frame, quality = validated_events(frame)
            frame.attrs["crc_input_quality"] = quality
            if file_fingerprint(path) != fingerprint:
                raise ValueError("EC 谱文件已更新，请重新读取")
            return frame, quality

        return EC_READ_CACHE.get(("ec-input", fingerprint), load)

    @staticmethod
    def _evidence(path, fingerprint):
        def load():
            result = ECL2Processor.input_evidence(path)
            if file_fingerprint(path) != fingerprint:
                raise ValueError("EC 谱文件已更新，请重新读取")
            return result

        return EC_READ_CACHE.get(("ec-evidence", fingerprint), load)

    def _prepared(self, processor, path, channel, background_path, background_channel, bin_width):
        signal_key = file_fingerprint(path)
        background_key = file_fingerprint(background_path) if background_path else None

        def load():
            frame, signal_quality = self._input(path, signal_key)
            signal = frame.loc[frame["channel"] == channel].copy()
            if signal.empty:
                raise ValueError(f"No events in {path.name} ch{channel}")
            background_frame = (signal.iloc[0:0] if background_path is None else frame
                                if background_key == signal_key else self._input(background_path, background_key)[0])
            background = background_frame.loc[background_frame["channel"] == background_channel].copy()
            prepared = processor._prepare_spectrum(signal, background, bin_width)
            if (file_fingerprint(path) != signal_key
                    or background_path and file_fingerprint(background_path) != background_key):
                raise ValueError("EC 谱文件已更新，请重新读取")
            return prepared, len(signal), signal_quality

        key = ("ec-prepared", signal_key, background_key, channel, background_channel, float(bin_width))
        return (*EC_READ_CACHE.get(key, load), signal_key, background_key)

    def points(self, payload: str, product_root: Path, channel: int | None = None) -> list[dict]:
        if channel is not None:
            self._channel(channel)
        rows = []
        parquet_files = {path.name: path for path in self._files(payload, product_root, "EC_L1", "parquet", "*_corr.l1.parquet")}
        priority = {directory / "parquet": rank for rank, directory in enumerate(self._layers(payload, product_root, "EC_L1"))}
        resolved_root = Path(product_root).resolve()
        for path in self._files(payload, product_root, "EC_L2", "json", "*_fit_results.l2.json"):
            stem = path.name.removesuffix("_fit_results.l2.json")
            records = read_json(path)
            if not isinstance(records, list):
                continue
            origin = "run" if resolved_root in path.resolve().parents else "historical"
            for record in records:
                ch = record.get("channel")
                if ch not in range(4) or channel is not None and ch != channel:
                    continue
                try:
                    energy = float(record.get("E", 0))
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(energy) or energy <= 0:
                    continue
                if record.get("signal_file"):
                    name = record["signal_file"]
                    if Path(name).name != name:
                        raise ValueError("EC product filename must not contain a path")
                    parquet = parquet_files.get(name)
                elif stem.startswith("src_"):
                    names = sorted(name for name in parquet_files if fnmatch.fnmatchcase(name, f"{stem}_*_corr.l1.parquet"))
                    rank = min((priority[parquet_files[name].parent] for name in names), default=None)
                    preferred = [name for name in names if priority[parquet_files[name].parent] == rank]
                    parquet = parquet_files[preferred[-1]] if preferred else None
                else:
                    parquet = parquet_files.get(f"{stem}_ch{ch}_corr.l1.parquet")
                reasons = self.quality_reasons(record)
                rows.append(
                    {
                        **record,
                        "id": point_id(payload, stem, ch, energy),
                        "stem": stem,
                        "energy_key": stem,
                        "payload": payload,
                        "l2_path": str(path),
                        "source_path": str(parquet) if parquet else None,
                        "l1_stem": parquet.name.removesuffix("_corr.l1.parquet") if parquet else None,
                        "quality_pass": ECL3Processor.usable_record(record) and not unsupported_spectral_model(
                            self.adapter.detector_payload(payload) if hasattr(self.adapter, "detector_payload") else payload, ch),
                        "quality_suspect": bool(reasons),
                        "quality_reasons": reasons,
                        "source_origin": origin,
                    }
                )
        return _clean(sorted(rows, key=lambda row: (row["E"], row["channel"], row["stem"])))

    @staticmethod
    def quality_reasons(record: dict) -> list[str]:
        """Expose warning annotations using the processor's own diagnostics."""
        return ECL3Processor.quality_reasons(record)

    def overview(self, payload: str, product_root: Path, channel: int, *, points: list[dict] | None = None) -> dict:
        self._channel(channel)
        rows = self.points(payload, product_root, channel) if points is None else [dict(point) for point in points]
        for row in rows:
            pending = row.get("fit_status") == "not_fitted" or row.get("center") is None
            row.setdefault("quality_pass", ECL3Processor.usable_record(row) and not unsupported_spectral_model(
                self.adapter.detector_payload(payload) if hasattr(self.adapter, "detector_payload") else payload, channel))
            row.setdefault("quality_reasons", [] if pending else self.quality_reasons(row))
            row.setdefault("quality_suspect", not pending and bool(row["quality_reasons"]))
        paths = self._files(payload, product_root, "EC_L3", "json", "*_EC.l3.json")
        current = self.adapter._layer_dir(product_root, payload, "EC_L3") / "json"
        current_paths = sorted(current.glob("*_EC.l3.json"))
        path = current_paths[-1] if current_paths else paths[-1] if paths else None
        summary = read_json(path) if path else {}
        params = summary.get("calibration_params", {}).get(str(channel))
        split = float(summary.get("energy_split", 49.0))
        manifests = self._files(payload, product_root, "EC_L3", "json", "L3_manifest.json")
        manifest = read_json(manifests[-1]) if manifests else {}
        excluded = set(manifest.get("excluded_points", []))
        available = [row for row in rows if row["quality_pass"] and row["id"] not in excluded]
        kept = ECL3Processor._filter_monotonic(available)
        kept_ids = {row["id"] for row in kept}
        coefficients = (params or {}).get("EC_low")
        for row in rows:
            row["excluded"] = row["id"] in excluded
            row["used"] = row["id"] in kept_ids
            for reason in row.get("quality_warnings", []):
                if reason not in row["quality_reasons"]:
                    row["quality_reasons"].append(reason)
            row["quality_suspect"] = bool(row["quality_reasons"])
            row["predicted_energy"] = (
                float(np.polyval(coefficients, row["center"])) if coefficients and row["quality_pass"] else None
            )
            row["residual_keV"] = (
                row["E"] - row["predicted_energy"] if row["predicted_energy"] is not None else None
            )
        curve = {"x": [], "y": []}
        resolution_curve = {"x": [], "y": []}
        numeric = [row for row in rows if row["quality_pass"]]
        if numeric and coefficients:
            adc = np.linspace(min(row["center"] for row in numeric), max(row["center"] for row in numeric), 250)
            curve = {"x": adc, "y": np.polyval(coefficients, adc)}
        if numeric and params:
            energies = np.geomspace(
                max(min(row["E"] for row in numeric), 0.01), max(row["E"] for row in numeric), 250
            )
            resolution = []
            for energy in energies:
                coeff = params.get("resolution_low" if energy <= split else "resolution_high")
                resolution.append(float(res_fit(energy, *coeff)) if coeff else None)
            resolution_curve = {"x": energies, "y": resolution}
        residuals = [row["residual_keV"] for row in rows if row["used"] and row["residual_keV"] is not None]
        return _clean(
            {
                "points": rows,
                "params": params,
                "version": path.name if path else None,
                "source_path": path,
                "curve": curve,
                "resolution_curve": resolution_curve,
                "energy_split": split,
                "diagnostics": {
                    "n_points": len(rows),
                    "n_used": len(kept_ids),
                    "n_excluded": sum(row["excluded"] for row in rows),
                    "rmse_keV": np.sqrt(np.mean(np.square(residuals))) if residuals else None,
                },
                "model_direction": "E(ADC); existing processor uses shared low/high coefficients",
            }
        )

    def spectrum(
        self, payload: str, product_root: Path, selected_id: str, channel: int | None = None,
        *, point: dict | None = None,
    ) -> dict:
        if point is None:
            rows = self.points(payload, product_root, channel)
            point = next((row for row in rows if row["id"] == selected_id), None)
        elif point.get("id") != selected_id or channel is not None and point.get("channel") != channel:
            raise ValueError("EC 能线与所选通道不匹配")
        if point is None:
            raise FileNotFoundError(f"No EC L2 point for {selected_id}")
        point = dict(point)
        ch, stem = point["channel"], point["stem"]
        self._channel(ch)
        path = (
            self._named_parquet(payload, product_root, point["signal_file"])
            if point.get("signal_file")
            else self._parquet(payload, product_root, stem, ch)
        )
        if path is None:
            raise FileNotFoundError(f"No corrected EC L1 spectrum for {stem} ch{ch}")
        background_ch = int(point.get("background_channel", ch))
        if "background_file" in point:
            background_path = (
                self._named_parquet(payload, product_root, point["background_file"])
                if point["background_file"]
                else None
            )
            if point["background_file"] and background_path is None:
                raise FileNotFoundError(f"No recorded EC background input {point['background_file']}")
        elif stem.startswith("src_"):
            background_paths = []
            for directory in self._layers(payload, product_root, "EC_L1"):
                background_paths = sorted((directory / "parquet").glob("src_bkg_*_corr.l1.parquet"))
                if background_paths:
                    break
            background_path = background_paths[-1] if background_paths and background_ch == ch else path
        else:
            background_path = self._parquet(payload, product_root, stem, background_ch)
        if background_path is None and "background_file" not in point:
            raise FileNotFoundError(f"No EC background spectrum for {stem} ch{background_ch}")
        processor = ECL2Processor(payload, product_root)
        bin_width = float(point.get("bin_width", BIN_WIDTH))
        prepared, n_events, signal_quality, signal_key, background_key = self._prepared(
            processor, path, ch, background_path, background_ch, bin_width)
        input_evidence = {}
        for role, input_path in (("signal", path), ("background", background_path)):
            actual = (
                self._evidence(input_path, signal_key if role == "signal" else background_key)
                if input_path else {"sha256": None, "tb_correction": {}}
            )
            expected = point.get(f"{role}_sha256")
            recorded_empty = role == "background" and f"{role}_sha256" in point and input_path is None
            verified = actual["sha256"] == expected if expected else True if recorded_empty else None
            input_evidence[role] = {
                "path": input_path,
                **actual,
                "expected_sha256": expected,
                "fit_tb_correction": point.get(f"{role}_tb_correction"),
                "verified": verified,
                "empty": input_path is None,
            }
        checks = [item["verified"] for item in input_evidence.values()]
        fit_input_verified = (
            False if False in checks else True if all(item is True for item in checks) else None
        )
        fit_input_note = (
            "Stored fit inputs match the displayed signal and background bytes."
            if fit_input_verified is True
            else "Signal or background bytes changed after this fit. The saved curve belongs to older inputs; rerun EC L2."
            if fit_input_verified is False
            else "Historical fit lacks input hashes. Its relationship to the displayed L1 spectrum is unverified; rerun EC L2 to establish provenance."
        )
        x = prepared["mids"]
        lo, hi = (float(point["lo"]) if point.get("lo") is not None else 0.,
                  float(point["hi"]) if point.get("hi") is not None else float(x[-1] + bin_width / 2))
        fit_x = x[(x >= lo) & (x <= hi)]
        pending = (point.get("placeholder") or unsupported_spectral_model(
            self.adapter.detector_payload(payload) if hasattr(self.adapter, "detector_payload") else payload, ch)
            or point.get("fit_status") == "not_fitted"
            or any(point.get(key) is None or not np.isfinite(point[key]) for key in ("center", "sigma", "amplitude"))
            or point.get("sigma", 0) <= 0)
        gaussian = (np.zeros_like(fit_x) if pending else GaussianModel().eval(
            x=fit_x, amplitude=point["amplitude"], center=point["center"], sigma=point["sigma"]))
        fitted_background = np.zeros_like(fit_x)
        model_params = point.get("model_parameters", {})
        mode = point.get("fit_mode", "gaus")
        exact_curve = mode == "gaus" or bool(model_params)
        if model_params and mode == "lin + gaus":
            fitted_background = LinearModel().eval(
                x=fit_x, slope=model_params["b_slope"], intercept=model_params["b_intercept"]
            )
        elif model_params and mode == "exp + gaus":
            fitted_background = ExponentialModel().eval(
                x=fit_x, amplitude=model_params["e_amplitude"], decay=model_params["e_decay"]
            )
        elif model_params and mode == "quad + gaus":
            fitted_background = QuadraticModel().eval(
                x=fit_x, a=model_params["b_a"], b=model_params["b_b"], c=model_params["b_c"]
            )
        metadata = input_evidence["signal"]["tb_correction"]
        curve_notes = []
        if not exact_curve:
            curve_notes.append(
                "Historical record omitted background parameters; Gaussian component only. Refit to save the full curve."
            )
        if fit_input_verified is not True:
            curve_notes.append(fit_input_note)
        return _clean(
            {
                "id": selected_id,
                "stem": stem,
                "channel": ch,
                "point": point,
                "fit": None if pending else point,
                "x": x,
                "y": prepared["net"],
                "signal": prepared["spec"],
                "background": prepared["bkg_spec"],
                "yerr": prepared["net_err"],
                "curve": None if pending else {
                    "x": fit_x,
                    "y": gaussian + fitted_background if exact_curve else [],
                    "gaussian": gaussian,
                    "background": fitted_background if exact_curve else [],
                    "exact": exact_curve,
                    "input_verified": fit_input_verified,
                    "note": " ".join(curve_notes) if curve_notes else None,
                },
                "source_path": path,
                "background_path": background_path,
                "fit_input_verified": None if pending else fit_input_verified,
                "fit_input_note": None if pending else fit_input_note,
                "input_evidence": input_evidence,
                "source": "run" if Path(product_root).resolve() in path.resolve().parents else "historical",
                "n_events": n_events,
                "input_quality": signal_quality,
                "tb_correction": metadata,
                "tb_corrfile": metadata.get("tb_corrfile"),
                "lo": lo,
                "hi": hi,
                "range": [max(0, lo - (hi - lo) * 0.6), hi + (hi - lo) * 0.6],
                "bin_width": bin_width,
                "window_bounds": {"min": 0., "max": max(float(x[-1] + bin_width / 2), hi),
                                  "minimum_width": 8 * bin_width},
                "y_label": f"Count rate / {bin_width:g} ADC bin (s⁻¹)",
                "normalization": "Processor UTC-axis acquisition duration; signal and background normalized independently",
            }
        )
