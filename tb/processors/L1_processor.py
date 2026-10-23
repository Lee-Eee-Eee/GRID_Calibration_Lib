"""温度偏压(TB)标定 - L1层处理器。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel
from scipy.signal import savgol_filter

from ...common.peak_finding import PeakDetectionError, detect_photopeak, fit_quality, propose_photopeaks
from ...common.spectral_models import pending_peak_record, unsupported_spectral_model
from ...common.tb_peak_context import gain_guides
from ...common.utils import (
    DataLayout,
    get_timestamp,
    read_json,
    validated_events,
    write_json,
)

# 单峰高斯 + 单一背景（与原古项目 calibration_process 对齐）
FIT_MODE_GAUS = "gaus"              # 无背景
FIT_MODE_LIN_GAUS = "lin + gaus"    # 线性背景
FIT_MODE_EXP_GAUS = "exp + gaus"    # 指数背景
FIT_MODE_QUAD_GAUS = "quad + gaus"  # 二次背景


@dataclass
class FitResult:
    hist: np.ndarray
    mids: np.ndarray
    hist_smooth: np.ndarray
    x_cut: np.ndarray
    y_cut: np.ndarray
    best_fit: np.ndarray
    gauss_fit: np.ndarray
    background_fit: np.ndarray
    residual: np.ndarray
    center: float
    center_err: float
    sigma: float
    sigma_err: float
    amplitude: float
    amplitude_err: float
    slope: float | None
    intercept: float | None
    exp_amplitude: float | None
    exp_decay: float | None
    quad_a: float | None
    quad_b: float | None
    quad_c: float | None
    fwhm: float
    height: float
    fit_mode: str
    lo: float
    hi: float
    rsquared: float
    peak_detection: dict[str, Any]
    fit_quality: dict[str, Any]
    histogram_config: dict[str, float]


class TBL1Processor:
    """TB L1层处理器 - 兼容旧流程拟合与绘图。"""

    def __init__(self, payload_name: str, product_root: Path, *, response_payload: str | None = None):
        self.payload_name = payload_name
        self.response_payload = response_payload or payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(
        self,
        tb_l0_dir: Path,
        config: dict[str, Any] | None = None,
        only_stems: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """处理 TB L1。

        参数:
            tb_l0_dir: L0 输出根目录（含 parquet/ 子目录）
            config: TB_PROCESS_PARAMS dict
            only_stems: 若给定，只重跑这些 stem 对应的 parquet（用于改完手选窗
                后单点重画）。其他 stem 的 L1 输出保持不变；不会覆盖 manifest /
                fit_windows_used 全局快照。
        """
        cfg = config or {}
        auto_window = bool(cfg.get("auto_window", True))

        win_path = cfg.get("fit_windows_config")
        if win_path is None:
            default_path = Path(__file__).resolve().parents[2] / "resources" / f"legacy_fit_windows_config_{self.payload_name}.json"
            win_path = str(default_path) if default_path.exists() else None

        manual_windows = self._load_windows(Path(win_path)) if win_path else {}
        used_windows: dict[str, dict[str, dict[str, Any]]] = {}

        results: dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "TB_L1",
            "timestamp": get_timestamp(),
            "auto_window": auto_window,
            "automatic_window": bool(cfg.get("automatic_window")),
            "peak_detection_config": cfg.get("peak_detection") or {},
            "fit_windows_config": str(win_path) if win_path else None,
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        parquet_dir = tb_l0_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {parquet_dir}")

        only_set = {s.strip().removesuffix(".l0") for s in only_stems if s and s.strip()} if only_stems else None
        partial_run = only_set is not None

        all_parquets = sorted(parquet_dir.glob("*.parquet"))
        if partial_run:
            parquet_files = [p for p in all_parquets if p.stem.removesuffix(".l0") in only_set]
            missing = only_set - {p.stem.removesuffix(".l0") for p in parquet_files}
            if missing:
                print(f"⚠ 以下 stem 在 L0 parquet 中找不到: {sorted(missing)}")
            if not parquet_files:
                raise ValueError(f"No matching parquet for stems={sorted(only_set)}")
            print(f"[partial] L1 only re-running {len(parquet_files)} stem(s): "
                  f"{[p.stem for p in parquet_files]}")
        else:
            parquet_files = all_parquets

        selected_channels = cfg.get("fit_channels", [0, 1, 2, 3])
        if (not isinstance(selected_channels, (list, tuple)) or not selected_channels
                or any(type(ch) is not int or ch not in range(4) for ch in selected_channels)):
            raise ValueError("fit_channels must contain channel numbers 0–3")
        selected_channels = sorted(set(selected_channels))
        skipped = [ch for ch in selected_channels if unsupported_spectral_model(self.response_payload, ch)]
        results["skipped_channels"] = [{"channel": ch, "reason": unsupported_spectral_model(self.response_payload, ch)}
                                       for ch in skipped]
        selected_channels = [ch for ch in selected_channels if ch not in skipped]
        if not selected_channels:
            raise ValueError(unsupported_spectral_model(self.response_payload, skipped[0]))
        results["fit_channels"] = selected_channels
        guides = {}
        if (not cfg.get("peak_detection", {}).get("expected_center")
                and (cfg.get("automatic_window") or not manual_windows)):
            context_paths = cfg.get("peak_context_l0_paths", all_parquets)
            guide_channels = [ch for ch in range(4) if not unsupported_spectral_model(self.response_payload, ch)]
            guides = self._learn_peak_guides([Path(path) for path in context_paths], cfg, guide_channels)
            results["peak_context"] = str(self.layout.get_tb_l1_json("auto_peak_context.json")) if guides else None
        results["n_files"] = len(parquet_files)
        results["file_outcomes"] = []
        results["channels_fitted"] = 0
        results["channels_failed"] = 0
        results["channels_accepted"] = 0
        results["channels_rejected"] = 0
        for file_index, parquet_file in enumerate(parquet_files, 1):
            started = perf_counter()
            stem = parquet_file.stem.removesuffix(".l0")
            fig = None
            updated = []
            failures = []
            try:
                # Spectral fitting needs amplitude, channel and CRC only. Read
                # one footer for both metadata fields instead of once per channel.
                import pyarrow.parquet as pq

                parquet = pq.ParquetFile(parquet_file)
                columns = [name for name in ("channel", "amp", "crc_check")
                           if name in parquet.schema_arrow.names]
                df, input_quality = validated_events(parquet.read(columns=columns).to_pandas())
                metadata = parquet.schema_arrow.metadata or {}
                meta = json.loads(metadata.get(b"hk_data", b"{}"))
                acquisition = json.loads(metadata.get(b"acquisition", b"{}"))
                histogram = cfg.get("histogram_config") or acquisition.get("fitting", {}).get("tb_histogram")
                windows = {} if cfg.get("automatic_window") else manual_windows
                json_path = self.layout.get_tb_l1_json(f"{stem}_fit_results.l1.json")
                existing = read_json(json_path) if json_path.exists() else []
                records = {row["channel"]: row for row in existing if isinstance(row, dict) and "channel" in row
                           and not unsupported_spectral_model(self.response_payload, row["channel"])}
                fits = {}
                used_windows.setdefault(stem, {})
                amplitudes = df["amp"].to_numpy(dtype=float)
                channels = df["channel"].to_numpy()
                for ch in selected_channels:
                    try:
                        amp_data = amplitudes[channels == ch]
                        if len(amp_data) < 10:
                            raise ValueError(f"Only {len(amp_data)} valid events; at least 10 are required")
                        settings = dict(cfg.get("peak_detection") or {})
                        guide = guides.get(stem, {}).get(str(ch)) if not self._is_manual_entry(windows.get(stem, {}).get(str(ch), {})) else None
                        if guide:
                            settings.update({key: guide[key] for key in ("expected_center", "expected_tolerance")})
                        fit_res = self._fit_channel(stem, ch, amp_data, windows, auto_window,
                                                    settings, histogram)
                        if guide:
                            fit_res.peak_detection["gain_guide"] = guide
                        is_manual = self._is_manual_entry(windows.get(stem, {}).get(str(ch), {}))
                        record = self._fit_record(ch, fit_res, meta, is_manual, len(amp_data), input_quality)
                        record["acquisition_profile_sha256"] = acquisition.get("profile_sha256")
                        records[ch] = record
                        fits[ch] = fit_res
                        results["channels_accepted" if fit_res.fit_quality["accepted"] else "channels_rejected"] += 1
                        updated.append(ch)
                        used_windows[stem][str(ch)] = {
                            "lo": float(fit_res.lo), "hi": float(fit_res.hi), "manual": is_manual,
                        }
                    except Exception as exc:
                        failure = {"file": parquet_file.name, "stem": stem, "channel": ch,
                                   "error": str(exc), "peak_detection": getattr(exc, "diagnostics", None)}
                        failures.append(failure)
                        results["errors"].append(failure)
                if updated:
                    for deferred in range(4):
                        if not unsupported_spectral_model(self.response_payload, deferred):
                            continue
                        empty = pending_peak_record(self.response_payload, deferred)
                        for key in ("temp", "temp_err", "bias", "bias_err"):
                            values = meta.get(key, [])
                            if len(values) > deferred:
                                empty[key] = float(values[deferred])
                        empty.update(window_lo=float((histogram or {}).get("min", 0.)),
                                     window_hi=float((histogram or {}).get("max", 65536.)),
                                     n_events=int(np.count_nonzero(channels == deferred)),
                                     window_manual=False, input_quality=input_quality,
                                     acquisition_profile_sha256=acquisition.get("profile_sha256"))
                        records[deferred] = empty
                    # A channel edit preserves the other saved channel records.
                    # Failed requests retain their previous fit, while errors
                    # and channels_updated explicitly describe this attempt.
                    write_json(json_path, [records[ch] for ch in sorted(records)])
                    if len(selected_channels) == 1:
                        fig, ax = plt.subplots(figsize=(8, 5))
                        axes = {selected_channels[0]: ax}
                        figure_name = f"{stem}_ch{selected_channels[0]}_fit.l1.jpg"
                    else:
                        fig, grid = plt.subplots(2, 2, figsize=(13, 9))
                        axes = dict(enumerate(grid.flat))
                        figure_name = f"{stem}_fit.l1.jpg"
                    for ch, ax in axes.items():
                        if ch in fits:
                            self._plot_channel(ax, ch, fits[ch])
                        else:
                            label = (f"CH{ch}: Compton response, model pending" if unsupported_spectral_model(self.response_payload, ch)
                                     else f"CH{ch}: fit failed" if ch in selected_channels else f"CH{ch}: unchanged")
                            ax.text(0.5, 0.5, label,
                                    ha="center", va="center", transform=ax.transAxes)
                    fig.suptitle(f"{stem} | Fit mode: auto-select", fontsize=14)
                    # Fixed padding avoids constrained-layout's repeated text
                    # measurement on every draw during an interactive refit.
                    fig.subplots_adjust(left=0.08, right=0.98, bottom=0.10, top=0.88, hspace=0.42, wspace=0.25)
                    fig_path = self.layout.get_tb_l1_figure(figure_name)
                    fig.savefig(fig_path, dpi=150, format="jpg")
                    results["outputs"].append({
                        "stem": stem, "channels_fitted": sum(not row.get("placeholder", False) for row in records.values()), "channels_updated": updated,
                        "channels_failed": [failure["channel"] for failure in failures],
                        "json": json_path.name, "figure": fig_path.name, "input_quality": input_quality,
                    })
                    results["n_processed"] += 1
            except Exception as exc:
                failure = {"file": parquet_file.name, "stem": stem, "channel": None,
                           "error": str(exc), "peak_detection": getattr(exc, "diagnostics", None)}
                failures.append(failure)
                results["errors"].append(failure)
            finally:
                if fig is not None:
                    plt.close(fig)
            results["channels_fitted"] += len(updated)
            results["channels_failed"] += len(selected_channels) - len(updated)
            outcome = {"stem": stem, "channels_updated": updated,
                       "channels_failed": [failure["channel"] for failure in failures],
                       "errors": failures, "elapsed_seconds": round(perf_counter() - started, 3)}
            results["file_outcomes"].append(outcome)
            print("L1_PROGRESS " + json.dumps({
                "completed": file_index, "total": len(parquet_files), "stem": stem,
                "fitted": results["channels_fitted"], "failed": results["channels_failed"],
            }), flush=True)
            print(f"[L1 {file_index}/{len(parquet_files)}] {stem}: "
                  f"fitted {len(updated)}/{len(selected_channels)} channels, "
                  f"failed {len(failures)} ({outcome['elapsed_seconds']:.2f}s)", flush=True)
            for failure in failures:
                print(f"  CH{failure['channel']}: {failure['error']}", flush=True)

        # partial run 时只合并更新本次涉及的 stem，避免清掉之前其他文件的快照/清单
        snap_path = self.layout.get_tb_l1_json("fit_windows_used.json")
        manifest_path = self.layout.get_tb_l1_json("L1_manifest.json")
        if partial_run:
            existing_snap = self._load_windows(snap_path) if snap_path.exists() else {}
            for stem, channels in used_windows.items():
                existing_snap.setdefault(stem, {}).update(channels)
            write_json(snap_path, existing_snap)

            existing_manifest = read_json(manifest_path) if manifest_path.exists() else {}
            if isinstance(existing_manifest, dict):
                merged_outputs = {o["stem"]: o for o in existing_manifest.get("outputs", []) if isinstance(o, dict)}
                for o in results["outputs"]:
                    merged_outputs[o["stem"]] = o
                existing_manifest["outputs"] = list(merged_outputs.values())
                existing_manifest["last_partial_run"] = {
                    "timestamp": results["timestamp"],
                    "stems": sorted(only_set or []),
                    "n_processed": results["n_processed"],
                    "errors": results.get("errors", []),
                }
                write_json(manifest_path, existing_manifest)
            else:
                write_json(manifest_path, results)
            results["partial_run_stems"] = sorted(only_set or [])
        else:
            write_json(snap_path, used_windows)
            write_json(manifest_path, results)
        return results

    def _learn_peak_guides(self, paths, config, channels):
        """Cache an independent L0-series search guide in this output run."""
        if len(paths) < 16:
            return {}
        import pyarrow.parquet as pq

        from ...common.peak_finding import ALGORITHM_SHA256
        from ...workbench.acquisition import tb_histogram_config

        unique = {}
        for path in paths:
            unique.setdefault(path.stem.removesuffix(".l0"), path)
        paths = [unique[stem] for stem in sorted(unique)]
        inputs = []
        skipped_inputs = []
        for path in paths:
            try:
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                inputs.append({"stem": path.stem.removesuffix(".l0"), "size": path.stat().st_size,
                               "sha256": digest.hexdigest()})
            except OSError as exc:
                inputs.append({"stem": path.stem.removesuffix(".l0"), "unavailable": type(exc).__name__})
        identity = {"inputs": inputs, "settings": config.get("peak_detection") or {},
                    "histogram": config.get("histogram_config"), "channels": channels,
                    "search_sha256": ALGORITHM_SHA256,
                    "context_sha256": hashlib.sha256(Path(__file__).resolve().parents[2].joinpath(
                        "common/tb_peak_context.py").read_bytes()).hexdigest()}
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        output = self.layout.get_tb_l1_json("auto_peak_context.json")
        try:
            cached = read_json(output) if output.exists() else {}
        except (OSError, ValueError):
            cached = {}
        if cached.get("signature") == signature:
            return cached["guides"]
        spectra, temperatures, biases, stems = [], [], [], []
        reference_profile, histogram = None, None
        for path in paths:
            try:
                parquet = pq.ParquetFile(path)
                metadata = parquet.schema_arrow.metadata or {}
                hk = json.loads(metadata.get(b"hk_data", b"{}"))
                acquisition = json.loads(metadata.get(b"acquisition", b"{}"))
                profile = acquisition.get("profile_sha256")
                if len(hk.get("temp", [])) != 4 or len(hk.get("bias", [])) != 4:
                    continue
                if histogram is None:
                    reference_profile = profile
                    histogram = config.get("histogram_config") or tb_histogram_config(acquisition)
                    edges = np.arange(histogram["min"], histogram["max"] + histogram["bin_width"], histogram["bin_width"])
                if profile != reference_profile:
                    continue
                columns = [name for name in ("amp", "channel", "crc_check") if name in parquet.schema_arrow.names]
                frame, _ = validated_events(parquet.read(columns=columns).to_pandas())
                amplitude, channel = frame["amp"].to_numpy(), frame["channel"].to_numpy()
                spectra.append([np.histogram(amplitude[channel == ch], edges)[0] for ch in range(4)])
                temperatures.append(hk["temp"])
                biases.append(hk["bias"])
                stems.append(path.stem.removesuffix(".l0"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                skipped_inputs.append({"stem": path.stem.removesuffix(".l0"), "error": str(exc)})
        guides = {stem: {} for stem in stems}
        if spectra:
            spectra, temperatures, biases = map(np.asarray, (spectra, temperatures, biases))
            x = (edges[1:] + edges[:-1]) / 2
            for ch in channels:
                for stem, guide in zip(stems, gain_guides(x, spectra[:, ch], temperatures[:, ch], biases[:, ch],
                                                         config.get("peak_detection")), strict=True):
                    if guide:
                        guide["anchor_stems"] = [stems[index] for index in guide.pop("anchor_indices")]
                        guides[stem][str(ch)] = guide
        write_json(output, {**identity, "signature": signature, "guides": guides, "skipped_inputs": skipped_inputs})
        return guides

    def _fit_record(self, ch, fit_res, meta, is_manual, n_events, input_quality):
        temps = meta.get("temp", [float("nan")] * 4) if isinstance(meta, dict) else [float("nan")] * 4
        temp_errs = meta.get("temp_err", [0.0] * 4) if isinstance(meta, dict) else [0.0] * 4
        biases = meta.get("bias", [float("nan")] * 4) if isinstance(meta, dict) else [float("nan")] * 4
        bias_errs = meta.get("bias_err", [0.0] * 4) if isinstance(meta, dict) else [0.0] * 4
        ch_temp = float(temps[ch]) if ch < len(temps) else float("nan")
        ch_temp_err = float(temp_errs[ch]) if ch < len(temp_errs) else 0.0
        ch_bias = float(biases[ch]) if ch < len(biases) else float("nan")
        ch_bias_err = float(bias_errs[ch]) if ch < len(bias_errs) else 0.0

        # 分辨率 R = FWHM / center；误差用一阶传播
        if np.isfinite(fit_res.center) and fit_res.center > 0:
            resolution = float(fit_res.fwhm / fit_res.center)
            sigma_term = (fit_res.fwhm / fit_res.center) * (fit_res.sigma_err / max(fit_res.sigma, 1e-12)) if fit_res.sigma > 0 else 0.0
            center_term = (fit_res.fwhm / fit_res.center**2) * fit_res.center_err
            resolution_err = float(np.sqrt(sigma_term**2 + center_term**2))
        else:
            resolution = float("nan")
            resolution_err = 0.0

        record = {
            "channel": int(ch),
            "center": float(fit_res.center),
            "center_err": float(fit_res.center_err),
            "sigma": float(fit_res.sigma),
            "sigma_err": float(fit_res.sigma_err),
            "amplitude": float(fit_res.amplitude),
            "amplitude_err": float(fit_res.amplitude_err),
            "fwhm": float(fit_res.fwhm),
            "height": float(fit_res.height),
            "resolution": resolution,
            "resolution_err": resolution_err,
            "temp": ch_temp,
            "temp_err": ch_temp_err,
            "bias": ch_bias,
            "bias_err": ch_bias_err,
            "window_lo": float(fit_res.lo),
            "window_hi": float(fit_res.hi),
            "window_manual": is_manual,
            "fit_mode": fit_res.fit_mode,
            "rsquared": float(fit_res.rsquared),
            "n_events": int(n_events),
            "peak_detection": fit_res.peak_detection,
            "fit_quality": fit_res.fit_quality,
            "histogram_config": fit_res.histogram_config,
            "input_quality": input_quality,
        }
        if fit_res.slope is not None:
            record["slope"] = float(fit_res.slope)
        if fit_res.intercept is not None:
            record["intercept"] = float(fit_res.intercept)
        if fit_res.exp_amplitude is not None:
            record["exp_amplitude"] = float(fit_res.exp_amplitude)
        if fit_res.exp_decay is not None:
            record["exp_decay"] = float(fit_res.exp_decay)
        if fit_res.quad_a is not None:
            record["quad_a"] = float(fit_res.quad_a)
            record["quad_b"] = float(fit_res.quad_b)
            record["quad_c"] = float(fit_res.quad_c)
        return record

    def _is_manual_entry(self, item: dict[str, Any]) -> bool:
        """判断窗口条目是否为 resource 中提供的拟合窗口。

        legacy 项目里只要 ``lo`` / ``hi`` 同时存在就视为"已配置"，``manual`` 字段
        只在交互式编辑器里做显示标识，不参与逻辑判断。这里沿用 legacy 行为：
        有 lo/hi 就用，否则走自动峰检测；同时 L1 不再回写 resource。
        """
        if not isinstance(item, dict):
            return False
        return "lo" in item and "hi" in item

    def _load_windows(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _extract_tb_file_mean(self, metadata: dict[str, Any]) -> tuple[float, float]:
        temps = metadata.get("temp", []) if isinstance(metadata, dict) else []
        biases = metadata.get("bias", []) if isinstance(metadata, dict) else []
        valid_t = [float(t) for t in temps if isinstance(t, (int, float)) and np.isfinite(t)]
        valid_b = [float(b) for b in biases if isinstance(b, (int, float)) and np.isfinite(b)]
        temp_mean = float(np.mean(valid_t)) if valid_t else float("nan")
        bias_mean = float(np.mean(valid_b)) if valid_b else float("nan")
        return temp_mean, bias_mean

    def _fit_channel(
        self,
        stem: str,
        ch: int,
        energy: np.ndarray,
        config: dict[str, Any],
        auto_window: bool,
        peak_config: dict[str, Any] | None = None,
        histogram_config: dict[str, float] | None = None,
    ) -> FitResult:
        if histogram_config is not None:
            from ...workbench.acquisition import tb_histogram_config

            histogram = tb_histogram_config({"fitting": {"tb_histogram": histogram_config}})
            bin_lo, bin_hi, bin_width = (histogram[key] for key in ("min", "max", "bin_width"))
        else:
            bin_lo, bin_width = 0, 1
            bin_hi = int(max(420, np.percentile(energy, 99.8) + 40))
            if self.payload_name in ("14B", "15B"):
                bin_hi = min(1200, bin_hi)
            else:
                bin_hi = min(65536, bin_hi)
            histogram = {"min": float(bin_lo), "max": float(bin_hi), "bin_width": float(bin_width)}
        bins = np.arange(bin_lo, bin_hi + bin_width, bin_width)
        if bins[-1] > bin_hi:
            bins[-1] = bin_hi
        if not np.allclose(np.diff(bins), bin_width):
            # Keep equal bin widths required by candidate detection; the final
            # edge may extend by less than one bin beyond the requested bound.
            bins = np.arange(bin_lo, bin_hi + bin_width, bin_width)
        hist, edges = np.histogram(energy, bins=bins)
        mids = (edges[:-1] + edges[1:]) / 2.0

        return self._fit_histogram(stem, ch, mids, hist, config, auto_window, peak_config, histogram)

    def _fit_histogram(
        self, stem: str, ch: int, mids: np.ndarray, hist: np.ndarray,
        config: dict[str, Any], auto_window: bool,
        peak_config: dict[str, Any] | None = None,
        histogram: dict[str, float] | None = None,
    ) -> FitResult:
        """Fit the same original counting histogram used by the event processor.

        Exposing this step permits repeatable spectral studies without expanding
        histogram counts back into artificial pulse events.
        """
        mids, hist = np.asarray(mids, dtype=float), np.asarray(hist, dtype=float)
        if reason := unsupported_spectral_model(self.response_payload, ch):
            raise ValueError(reason)
        if histogram is None:
            width = float(np.median(np.diff(mids)))
            histogram = {"min": float(mids[0] - width / 2),
                         "max": float(mids[-1] + width / 2), "bin_width": width}

        window = 11 if len(hist) >= 11 else max(5, len(hist) // 2 * 2 + 1)
        hist_smooth = np.asarray(
            savgol_filter(hist.astype(float), window_length=window, polyorder=2),
            dtype=float,
        )

        item = config.get(stem, {}).get(str(ch), {}) if config else {}
        manual = self._is_manual_entry(item)

        requested = [float(item["lo"]), float(item["hi"])] if manual else None
        if manual:
            candidate, detection = detect_photopeak(mids, hist, window=requested, config=peak_config)
            lo, hi = requested
            detection["histogram"] = histogram
            return self._fit_candidate(mids, hist, hist_smooth, candidate, detection, lo, hi, histogram)

        # A TB scintillation peak must be resolved at more than the single-bin
        # scale. These dimensionless response bounds are configurable; no
        # payload-specific ADC threshold or historical hand window is used.
        settings = {"min_resolution": .03, "max_resolution": .6, **(peak_config or {})}
        candidates, detection = propose_photopeaks(mids, hist, config=settings)
        detection["histogram"] = histogram
        # Identify the strongest original-count Gaussian contrast before judging
        # model adequacy. A poor fit of that signal must not silently switch to
        # a statistically weaker, distant continuum structure.
        candidate = candidates[0]
        if (len(candidates) > 1 and candidates[1].score > .82 * candidate.score
                and abs(candidates[1].center - candidate.center) > 2 * max(candidate.sigma, candidates[1].sigma)):
            raise PeakDetectionError("Competing photopeaks need an independent gain anchor", detection)
        trials, rejected = [], []
        initial_sigma = candidate.sigma
        for extent in (3., 2.5, 2.):
            lo = max(float(mids[0]), candidate.center - extent * candidate.sigma)
            hi = min(float(mids[-1]), candidate.center + extent * candidate.sigma)
            if settings.get("noise_ceiling") is not None:
                lo = max(lo, float(settings["noise_ceiling"]))
            details = copy.deepcopy(detection)
            details.update(manual_window=False, selected_center=candidate.center,
                           candidate_origin="original_count_model_comparison",
                           window_sigma=extent, response_bounds={
                               "min_resolution": settings["min_resolution"],
                               "max_resolution": settings["max_resolution"],
                           })
            try:
                fit = self._fit_candidate(mids, hist, hist_smooth, candidate, details, lo, hi, histogram)
                fraction = fit.fwhm / max(fit.center, 1e-12)
                if not settings["min_resolution"] <= fraction <= settings["max_resolution"]:
                    fit.fit_quality["accepted"] = False
                    fit.fit_quality["status"] = "rejected"
                    fit.fit_quality["warnings"].append("Fitted width is outside the configured scintillation response range")
                trials.append(fit)
                if len(trials) == 1 and fit.sigma < .7 * initial_sigma:
                    # A coarse-scale shoulder can merge with a neighbour.
                    # Estimate its width from original counts, then refit
                    # a conventional interval in those measured sigma units.
                    candidate.sigma = fit.sigma
                # Keep the widest accepted interval. This is bounded model
                # adequacy testing; it does not shrink to an arbitrary set
                # of bins until an apparently good chi-square is obtained.
                if fit.fit_quality["accepted"]:
                    break
            except (ValueError, RuntimeError) as exc:
                rejected.append({"center": candidate.center, "window_sigma": extent, "error": str(exc)})
        if not trials:
            detection["fit_trials"] = rejected
            raise PeakDetectionError("No resolved photopeak model could be fitted", detection)

        def score(fit):
            quality = fit.fit_quality
            return np.sqrt(max(quality["delta_bic_background"], 0))

        trials.sort(key=lambda fit: (not fit.fit_quality["accepted"], -score(fit)))
        selected = trials[0]
        selected.peak_detection["fit_trials"] = [{
            "center": fit.center, "sigma": fit.sigma, "window": [fit.lo, fit.hi],
            "score": float(score(fit)), "quality": fit.fit_quality,
        } for fit in trials]
        selected.peak_detection["fit_errors"] = rejected
        selected.peak_detection["selection_status"] = "resolved" if selected.fit_quality["accepted"] else "rejected"
        return selected

    def _fit_candidate(self, mids, hist, hist_smooth, candidate, detection, lo, hi, histogram):
        peak_x = candidate.center

        cut_mask = (mids >= lo) & (mids <= hi)
        x_cut = mids[cut_mask]
        y_cut = hist[cut_mask]
        if x_cut.size < 8 or np.max(y_cut) <= 0:
            raise RuntimeError("Invalid fit window")

        center_lo = max(lo, peak_x - 1.5 * candidate.sigma)
        center_hi = min(hi, peak_x + 1.5 * candidate.sigma)
        result, comps, fit_mode, rsquared = self._select_best_fit(
            x_cut, y_cut, peak_x, center_lo, center_hi, candidate.sigma,
        )
        quality = fit_quality(result, x_cut, y_cut, np.sqrt(np.maximum(y_cut, 1.0)))

        center = float(result.params["g_center"].value)
        center_err = float(result.params["g_center"].stderr or 0.0)
        sigma = float(result.params["g_sigma"].value)
        sigma_err = float(result.params["g_sigma"].stderr or 0.0)
        amplitude = float(result.params["g_amplitude"].value)
        amplitude_err = float(result.params["g_amplitude"].stderr or 0.0)
        slope = intercept = None
        exp_amplitude = exp_decay = None
        quad_a = quad_b = quad_c = None

        if fit_mode == FIT_MODE_LIN_GAUS:
            slope = float(result.params["b_slope"].value)
            intercept = float(result.params["b_intercept"].value)
            background_fit = comps["b_"]
        elif fit_mode == FIT_MODE_EXP_GAUS:
            exp_amplitude = float(result.params["e_amplitude"].value)
            exp_decay = float(result.params["e_decay"].value)
            background_fit = comps["e_"]
        elif fit_mode == FIT_MODE_QUAD_GAUS:
            quad_a = float(result.params["q_a"].value)
            quad_b = float(result.params["q_b"].value)
            quad_c = float(result.params["q_c"].value)
            background_fit = comps["q_"]
        else:  # gaus only
            background_fit = np.zeros_like(x_cut, dtype=float)

        fwhm = 2.0 * np.sqrt(2.0 * np.log(2.0)) * sigma
        height = amplitude / (sigma * np.sqrt(2.0 * np.pi)) if sigma > 0 else float("nan")
        residual = y_cut - result.best_fit

        return FitResult(
            hist=hist,
            mids=mids,
            hist_smooth=hist_smooth,
            x_cut=x_cut,
            y_cut=y_cut,
            best_fit=result.best_fit,
            gauss_fit=comps["g_"],
            background_fit=background_fit,
            residual=residual,
            center=center,
            center_err=center_err,
            sigma=sigma,
            sigma_err=sigma_err,
            amplitude=amplitude,
            amplitude_err=amplitude_err,
            slope=slope,
            intercept=intercept,
            exp_amplitude=exp_amplitude,
            exp_decay=exp_decay,
            quad_a=quad_a,
            quad_b=quad_b,
            quad_c=quad_c,
            fwhm=float(fwhm),
            height=float(height),
            fit_mode=fit_mode,
            lo=float(lo),
            hi=float(hi),
            rsquared=float(rsquared),
            peak_detection=detection,
            fit_quality=quality,
            histogram_config=histogram,
        )

    def _get_fit_window(
        self,
        item: dict[str, Any],
        auto_lo: float,
        auto_hi: float,
        auto_window: bool,
    ) -> tuple[float, float]:
        if isinstance(item, dict) and "lo" in item and "hi" in item:
            return float(item["lo"]), float(item["hi"])
        if auto_window:
            return auto_lo, auto_hi
        return float(np.floor(auto_lo)), float(np.ceil(auto_hi))

    def _find_valley_left(self, y: np.ndarray, center_idx: int, span: int = 85) -> int:
        lo = max(0, center_idx - span)
        segment = y[lo : center_idx + 1]
        if segment.size < 5:
            return max(0, center_idx - 55)
        return lo + int(np.argmin(segment))

    def _find_valley_right(self, y: np.ndarray, center_idx: int, span: int = 110) -> int:
        hi = min(len(y) - 1, center_idx + span)
        segment = y[center_idx : hi + 1]
        if segment.size < 5:
            return min(len(y) - 1, center_idx + 55)
        return center_idx + int(np.argmin(segment))

    def _calculate_rsquared(self, y_data: np.ndarray, y_fit: np.ndarray) -> float:
        ss_res = np.sum((y_data - y_fit) ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
        if ss_tot == 0:
            return 0.0
        return float(1.0 - ss_res / ss_tot)

    def _fit_lin_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + LinearModel(prefix="b_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        params["b_intercept"].set(value=float(max(np.percentile(y_cut, 20), 0.0)), min=0.0)
        params["b_slope"].set(value=0.0)

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_exp_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + ExponentialModel(prefix="e_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        params["e_amplitude"].set(value=float(max(np.percentile(y_cut, 20), 1.0)), min=0.0)
        params["e_decay"].set(value=max(float(np.ptp(x_cut)), 1.), min=1.0, max=max(float(np.ptp(x_cut)) * 100, 1000.))

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_quad_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + QuadraticModel(prefix="q_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        params["q_a"].set(value=0.0)
        params["q_b"].set(value=0.0)
        params["q_c"].set(value=float(max(np.percentile(y_cut, 20), 0.0)))

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_gaus_only(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _select_best_fit(
        self,
        x_cut: np.ndarray,
        y_cut: np.ndarray,
        peak_x: float,
        lo: float,
        hi: float,
        sigma_guess: float = 12.0,
    ):
        """对齐原古项目 calibration_process：单高斯峰 + 4 选 1 单背景。

        候选: gaus | gaus + lin | gaus + exp | gaus + quad
        策略: 同时拟合，使用已知计数方差的 χ² + 2k 挑选单背景模型。
        """
        candidates = [
            (self._fit_gaus_only,  FIT_MODE_GAUS),
            (self._fit_lin_gaus,   FIT_MODE_LIN_GAUS),
            (self._fit_exp_gaus,   FIT_MODE_EXP_GAUS),
            (self._fit_quad_gaus,  FIT_MODE_QUAD_GAUS),
        ]
        results = []
        for fit_fn, mode in candidates:
            try:
                result, comps = fit_fn(x_cut, y_cut, peak_x, lo, hi, sigma_guess)
                rsq = self._calculate_rsquared(y_cut, result.best_fit)
                if not result.success or not np.isfinite(result.best_fit).all():
                    continue
                aic = float(np.sum(((y_cut - result.best_fit) / np.sqrt(np.maximum(y_cut, 1.0))) ** 2)) + 2 * result.nvarys
                results.append((result, comps, mode, rsq, aic))
            except Exception:
                continue

        if not results:
            raise RuntimeError("All fit modes failed")

        results.sort(key=lambda item: (
            not all(item[0].params[name].stderr is not None and np.isfinite(item[0].params[name].stderr)
                    and item[0].params[name].stderr > 0 for name in ("g_center", "g_sigma", "g_amplitude")),
            item[4],
        ))
        best = results[0]
        return best[0], best[1], best[2], best[3]

    def _plot_channel(self, ax_spec, ch: int, fit_res: FitResult) -> None:
        from ...common.plotting import (
            adaptive_xlim,
            plot_spectrum_fit,
            style_axes,
            style_legend,
            style_title,
        )
        plot_spectrum_fit(
            ax_spec,
            mids=fit_res.mids,
            raw=fit_res.hist,
            cut_x=fit_res.x_cut,
            cut_y=fit_res.y_cut,
            best_fit=fit_res.best_fit,
            gauss_fit=fit_res.gauss_fit,
            background_fit=fit_res.background_fit,
            fit_lo=fit_res.lo,
            fit_hi=fit_res.hi,
            center=fit_res.center,
        )
        style_title(
            ax_spec,
            f"ch{ch}: center={fit_res.center:.2f}, sigma={fit_res.sigma:.2f}, mode={fit_res.fit_mode}",
        )
        style_axes(ax_spec, xlabel="ADC unit", ylabel="counts")
        adaptive_xlim(ax_spec, fit_res.mids, fit_res.hist, fit_res.center, fit_res.sigma, fit_res.hi)
        style_legend(ax_spec)
