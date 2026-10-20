"""能量标定(EC)处理 - L2层处理器。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel
from scipy.signal import savgol_filter

from ...common.peak_finding import detect_photopeak, fit_quality
from ...common.utils import (
    DataLayout,
    get_timestamp,
    read_json,
    read_parquet,
    read_parquet_metadata,
    validated_events,
    write_json,
)

BIN_WIDTH = 4.0
DISPLAY_BIN_MERGE = 3


class ECL2Processor:
    """EC L2层处理器 - 兼容旧 X-ray 流程的背景扣除、拟合与绘图。"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    @staticmethod
    def input_evidence(path: Path) -> dict[str, Any]:
        """Identify the exact corrected input bytes and their TB metadata."""
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"sha256": digest.hexdigest(),
                "tb_correction": read_parquet_metadata(path, meta_key="tb_correction") or {},
                "acquisition": read_parquet_metadata(path, meta_key="acquisition") or {}}

    def process(self, ec_l1_dir: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = config or {}
        bin_width = float(cfg.get("bin_width", BIN_WIDTH))

        results: dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L2",
            "timestamp": get_timestamp(),
            "bin_width": bin_width,
            "automatic_window": bool(cfg.get("automatic_window")),
            "peak_detection_config": cfg.get("peak_detection") or {},
            "n_processed": 0,
            "outputs": [],
            "errors": [],
            "warnings": [],
        }

        parquet_dir = ec_l1_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L1 parquet directory not found: {parquet_dir}")

        src_windows = self._load_source_windows()
        xray_windows = self._load_xray_windows()

        src_re = re.compile(r"^src_(?P<name>.+)_(?P<min>\d+(?:\.\d+)?)m_corr\.l1\.parquet$")
        bkg_re = re.compile(r"^src_bkg_(?P<min>\d+(?:\.\d+)?)m_corr\.l1\.parquet$")

        bkg_df_global: Any | None = None
        bkg_file_global: str | None = None
        bkg_exposure: float | None = None
        selected_energies = cfg.get("only_energies")
        needs_background = selected_energies is None or any(str(value).startswith("src_") for value in selected_energies)
        if needs_background:
            candidates = [pf for pf in sorted(parquet_dir.glob("src_bkg_*_corr.l1.parquet")) if bkg_re.fullmatch(pf.name)]
            selected_background = cfg.get("background_stem")
            if selected_background is not None:
                if not re.fullmatch(r"src_bkg_\d+(?:\.\d+)?m", str(selected_background)):
                    raise ValueError("Invalid EC background_stem; use src_bkg_<minutes>m")
                candidates = [pf for pf in candidates if pf.name == f"{selected_background}_corr.l1.parquet"]
            if len(candidates) > 1:
                raise ValueError("Multiple EC backgrounds found; choose background_stem before peak fitting")
            if candidates:
                pf = candidates[0]
                bkg_df_global, input_quality = validated_events(read_parquet(pf))
                bkg_df_global.attrs["crc_input_quality"] = input_quality
                bkg_file_global = pf.name
                bkg_exposure = read_parquet_metadata(pf, "tb_correction").get(
                    "exposure_minutes", float(bkg_re.fullmatch(pf.name).group("min")),
                )

        grouped_by_energy: dict[str, dict[int, dict[str, Any]]] = {}
        input_evidence: dict[str, dict[str, Any]] = {}
        selected_stems = set(cfg["only_stems"]) if cfg.get("only_stems") is not None else None
        for parquet_file in sorted(parquet_dir.glob("*_corr.l1.parquet")):
            if bkg_re.fullmatch(parquet_file.name):
                continue
            if selected_stems is not None and parquet_file.name.removesuffix("_corr.l1.parquet") not in selected_stems:
                continue
            try:
                df, input_quality = validated_events(read_parquet(parquet_file))
                df.attrs["crc_input_quality"] = input_quality
                meta = read_parquet_metadata(parquet_file, meta_key="tb_correction") or {}

                src_match = src_re.match(parquet_file.name)
                if src_match:
                    source_name = src_match.group("name")
                    exposure_minutes = meta.get("exposure_minutes", float(src_match.group("min")))
                    peak_energies = meta.get("peak_energies_keV") or []
                    energy_key = f"src_{source_name}"

                    for ch in sorted(df["channel"].unique()):
                        if int(ch) < 0 or int(ch) >= 4:
                            continue
                        df_ch = df[df["channel"] == ch].copy()
                        if len(df_ch) < 10:
                            continue
                        if int(ch) in grouped_by_energy.get(energy_key, {}):
                            raise ValueError(f"Multiple observations of source {source_name}; select one source exposure with only_stems")

                        temp_val = float(df_ch["temp"].mean()) if "temp" in df_ch.columns else float("nan")
                        temp_err = float(df_ch["temp"].std()) if "temp" in df_ch.columns else 0.0
                        bias_val = float(df_ch["bias"].mean()) if "bias" in df_ch.columns else float("nan")
                        bias_err = float(df_ch["bias"].std()) if "bias" in df_ch.columns else 0.0

                        grouped_by_energy.setdefault(energy_key, {})[int(ch)] = {
                            "stem": f"{source_name}_ch{ch}",
                            "df": df_ch,
                            "parquet": parquet_file.name,
                            "data_type": "src",
                            "source_name": source_name,
                            "peak_energies_keV": list(peak_energies),
                            "exposure_minutes": exposure_minutes,
                            "temp": temp_val,
                            "temp_err": temp_err,
                            "bias": bias_val,
                            "bias_err": bias_err,
                        }
                else:
                    stem = parquet_file.stem.replace("_corr.l1", "")
                    parts = stem.rsplit("_ch", 1)
                    if len(parts) == 2:
                        energy_key = parts[0]
                        ch = int(parts[1])
                        if len(df) >= 10:
                            temp_val = float(df["temp"].mean()) if "temp" in df.columns else float("nan")
                            temp_err = float(df["temp"].std()) if "temp" in df.columns else 0.0
                            bias_val = float(df["bias"].mean()) if "bias" in df.columns else float("nan")
                            bias_err = float(df["bias"].std()) if "bias" in df.columns else 0.0

                            grouped_by_energy.setdefault(energy_key, {})[ch] = {
                                "stem": stem,
                                "df": df,
                                "parquet": parquet_file.name,
                                "data_type": "x",
                                "source_name": None,
                                "peak_energies_keV": [float(meta.get("energy_keV", self._energy_sort_key(energy_key)))],
                                "temp": temp_val,
                                "temp_err": temp_err,
                                "bias": bias_val,
                                "bias_err": bias_err,
                            }
            except Exception as exc:
                results["errors"].append({"file": parquet_file.name, "error": str(exc)})
                print(f"✗ Error: {parquet_file.name} - {exc}")

        for energy_key, ch_map in sorted(grouped_by_energy.items(), key=lambda item: self._energy_sort_key(item[0])):
            selected_energies = cfg.get("only_energies")
            if selected_energies is not None and energy_key not in selected_energies:
                continue
            if set(ch_map) != {0, 1, 2, 3}:
                print(f"⚠ EC L2: {energy_key} - not all 4 channels present, skipping")
                results["errors"].append({"energy": energy_key, "error": "Requires all four channels"})
                continue

            records: list[dict[str, Any]] = []
            plot_rows: list[dict[str, Any]] = []

            try:
                first = ch_map[0]
                data_type = first["data_type"]
                source_name = first.get("source_name")
                peak_energies = first.get("peak_energies_keV") or [self._energy_sort_key(energy_key)]
                if data_type == "src":
                    if bkg_df_global is None:
                        raise ValueError(f"Source {source_name} requires an explicitly available corrected background")
                    if not peak_energies or not all(np.isfinite(float(value)) and float(value) > 0 for value in peak_energies):
                        raise ValueError(f"Configure finite positive peak energies for source {source_name}")
                manual_windows = (src_windows.get(source_name, {}) if data_type == "src" and source_name else {})

                for ch in range(4):
                    signal = ch_map[ch]
                    temp_val = signal.get("temp", float("nan"))
                    temp_err = signal.get("temp_err", 0.0)
                    bias_val = signal.get("bias", float("nan"))
                    bias_err = signal.get("bias_err", 0.0)

                    if data_type == "src" and bkg_df_global is not None:
                        bkg_df_ch = bkg_df_global[bkg_df_global["channel"] == ch].copy()
                        bkg_ch = ch
                    else:
                        bkg_df_ch = None
                        bkg_ch, bkg = self._choose_background_channel(ch, ch_map)
                        bkg_df_ch = bkg["df"]

                    peaks_to_fit = sorted(peak_energies) if data_type == "src" else [float(peak_energies[0])]
                    prev_center: float | None = None
                    prev_energy: float | None = None
                    for peak_E in peaks_to_fit:
                        peak_window = None
                        window_source = "auto"
                        if data_type == "src":
                            peak_window = manual_windows.get(str(ch), {}).get(str(peak_E))
                            if peak_window is not None:
                                window_source = "packaged"
                        elif data_type == "x":
                            xw = xray_windows.get(energy_key, {}).get(str(ch))
                            if xw is not None:
                                peak_window = [xw["lo"], xw["hi"]]
                                window_source = "packaged"
                        point_id = f"{self.payload_name}:ec:{energy_key}:{ch}:{float(peak_E):g}"
                        manual_override = cfg.get("ec_windows", {}).get(point_id)
                        if manual_override is not None:
                            lo, hi = float(manual_override["lo"]), float(manual_override["hi"])
                            if not np.isfinite([lo, hi]).all() or hi - lo < 2 * bin_width:
                                raise ValueError(f"Invalid manual EC window for {point_id}: [{lo}, {hi}]")
                            peak_window = [lo, hi]
                            window_source = "manual"
                        if peak_window is None and prev_center is not None and prev_energy is not None:
                            center_guess = prev_center * (peak_E / prev_energy)
                            half_span = max(80.0, center_guess * 0.06)
                            peak_window = [center_guess - half_span, center_guess + half_span]
                            window_source = "energy_ratio"
                        detection_config = dict(cfg.get("peak_detection") or {})
                        if data_type == "src":
                            detection_config.setdefault("infer_noise", False)
                        if cfg.get("automatic_window"):
                            peak_window = None
                            window_source = "auto"
                            manual_override = None
                        if data_type == "src" and detection_config.get("expected_center") is not None:
                            anchor_energy = float(detection_config.pop("expected_energy_keV", min(peaks_to_fit)))
                            detection_config["expected_center"] *= peak_E / anchor_energy
                        elif prev_center is not None and prev_energy is not None:
                            detection_config["expected_center"] = prev_center * (peak_E / prev_energy)
                        try:
                            if (data_type == "src" and len(peaks_to_fit) > 1 and peak_window is None
                                    and detection_config.get("expected_center") is None):
                                raise RuntimeError(
                                    "Multiple source lines require a first-line window or an independently calibrated "
                                    "expected center and anchor energy; a strongest peak cannot identify its energy",
                                )
                            fit, plot_data = self._fit_energy_spectrum(
                                energy_key=energy_key,
                                signal_df=signal["df"],
                                background_df=bkg_df_ch if bkg_df_ch is not None else signal["df"].iloc[0:0],
                                channel=ch,
                                background_channel=bkg_ch,
                                bin_width=bin_width,
                                forced_window=peak_window,
                                forced_peak_energy=peak_E,
                                peak_config=detection_config,
                            )
                            if fit["fit_quality"]["accepted"]:
                                prev_center = float(fit["center"])
                                prev_energy = float(peak_E)
                        except Exception as exc:
                            print(f"⚠ EC L2: {energy_key} ch{ch} peak={peak_E}keV failed: {exc}")
                            results["errors"].append({"energy": energy_key, "channel": ch,
                                                       "peak_keV": peak_E, "error": str(exc),
                                                       "peak_detection": getattr(exc, "diagnostics", None)})
                            continue

                        rec = {
                            "channel": int(ch),
                            "E": float(peak_E),
                            "center": float(fit["center"]),
                            "center_err": float(fit["center_err"]),
                            "sigma": float(fit["sigma"]),
                            "sigma_err": float(fit["sigma_err"]),
                            "amplitude": float(fit["amplitude"]),
                            "amplitude_err": float(fit["amplitude_err"]),
                            "fwhm": float(fit["FWHM"]),
                            "resolution": float(fit["resolution"]),
                            "resolution_err": float(fit["resolution_err"]),
                            "fit_mode": fit["fit_mode"],
                            "source": data_type,
                            "source_name": source_name,
                            "temp": temp_val,
                            "temp_err": temp_err,
                            "bias": bias_val,
                            "bias_err": bias_err,
                            "lo": float(fit["lo"]),
                            "hi": float(fit["hi"]),
                            "rsquared": float(fit["rsquared"]),
                            "n_events": int(fit["n_events"]),
                            "background_channel": int(fit["background_channel"]),
                            "model_parameters": fit["model_parameters"],
                            "peak_detection": fit["peak_detection"],
                            "fit_quality": fit["fit_quality"],
                            "signal_input_quality": fit["signal_input_quality"],
                            "background_input_quality": fit["background_input_quality"],
                            "window_manual": manual_override is not None,
                            "window_source": window_source,
                            "normalization": "utc_duration",
                            "signal_exposure_minutes": signal.get("exposure_minutes"),
                            "background_exposure_minutes": bkg_exposure if data_type == "src" else None,
                            "signal_file": signal["parquet"],
                            "background_file": bkg_file_global if data_type == "src" and bkg_df_global is not None else ch_map[bkg_ch]["parquet"],
                        }
                        records.append(rec)
                        plot_rows.append({"record": rec, "plot": plot_data})

                if data_type == "x" and len(records) >= 2:
                    ch_fits: dict[int, dict[str, Any]] = {}
                    for rec in records:
                        ch_fits[rec["channel"]] = rec
                    consensus = self._xray_consensus_center(ch_fits)
                    if consensus is not None:
                        for rec in list(records):
                            ch = rec["channel"]
                            if rec.get("window_manual"):
                                # A user-selected window must survive automatic
                                # cross-channel consensus recovery unchanged.
                                continue
                            r2 = rec["rsquared"]
                            center = rec["center"]
                            if r2 >= 0.7 and abs(center - consensus) / max(consensus, 1.0) < 0.18:
                                continue
                            signal = ch_map[ch]
                            # X-ray spectra always use a different channel of
                            # that energy. An unrelated gamma-source background
                            # in the same directory must not change the refit.
                            refit_bkg_ch, refit_bkg = self._choose_background_channel(ch, ch_map)
                            refit_bkg_df = refit_bkg["df"]
                            cw = [consensus * 0.80, consensus * 1.22]
                            try:
                                fit2, plot2 = self._fit_energy_spectrum(
                                    energy_key=energy_key,
                                    signal_df=signal["df"],
                                    background_df=refit_bkg_df,
                                    channel=ch,
                                    background_channel=refit_bkg_ch,
                                    bin_width=bin_width,
                                    forced_window=cw,
                                    forced_peak_energy=float(rec["E"]),
                                    peak_config={**(cfg.get("peak_detection") or {}), "expected_center": consensus},
                                )
                                records.remove(rec)
                                new_rec = {
                                    "channel": ch, "E": rec["E"],
                                    "center": float(fit2["center"]), "center_err": float(fit2["center_err"]),
                                    "sigma": float(fit2["sigma"]), "sigma_err": float(fit2["sigma_err"]),
                                    "amplitude": float(fit2["amplitude"]), "amplitude_err": float(fit2["amplitude_err"]),
                                    "fwhm": float(fit2["FWHM"]), "resolution": float(fit2["resolution"]),
                                    "resolution_err": float(fit2["resolution_err"]), "fit_mode": fit2["fit_mode"],
                                    "source": data_type, "source_name": source_name,
                                    "temp": rec["temp"], "temp_err": rec["temp_err"],
                                    "bias": rec["bias"], "bias_err": rec["bias_err"],
                                    "lo": float(fit2["lo"]), "hi": float(fit2["hi"]),
                                    "rsquared": float(fit2["rsquared"]), "n_events": int(fit2["n_events"]),
                                    "background_channel": int(fit2["background_channel"]),
                                    "model_parameters": fit2["model_parameters"],
                                    "peak_detection": fit2["peak_detection"],
                                    "fit_quality": fit2["fit_quality"],
                                    "signal_input_quality": fit2["signal_input_quality"],
                                    "background_input_quality": fit2["background_input_quality"],
                                    "window_manual": False,
                                    "window_source": "consensus",
                                    "normalization": "utc_duration",
                                    "signal_exposure_minutes": None,
                                    "background_exposure_minutes": None,
                                    "signal_file": signal["parquet"],
                                    "background_file": refit_bkg["parquet"],
                                }
                                records.append(new_rec)
                                plot_rows[:] = [pr for pr in plot_rows if pr.get("record", {}).get("channel") != ch]
                                plot_rows.append({"record": new_rec, "plot": plot2})
                            except Exception:
                                pass

                if not records:
                    print(f"⚠ EC L2: {energy_key} - no successful fits")
                    results["errors"].append({"energy": energy_key, "error": "No successful peak fits"})
                    continue

                # Persist the inputs used by these specific peak fits. A later
                # EC L1 rerun can overwrite a same-name parquet; its new bytes
                # must never silently be paired with this older fit curve.
                for rec in records:
                    rec["bin_width"] = bin_width
                    for role in ("signal", "background"):
                        filename = rec.get(f"{role}_file")
                        if filename and filename not in input_evidence:
                            input_evidence[filename] = self.input_evidence(parquet_dir / filename)
                        evidence = input_evidence[filename] if filename else {}
                        rec[f"{role}_sha256"] = evidence.get("sha256")
                        rec[f"{role}_tb_correction"] = evidence.get("tb_correction", {})
                        rec[f"{role}_acquisition"] = evidence.get("acquisition", {})

                auto_count = sum(record.get("window_source") == "auto" for record in records)
                if auto_count:
                    results["warnings"].append(f"{energy_key}: {auto_count} peaks used automatic windows; review the fitted peaks and windows")

                json_name = f"{energy_key}_fit_results.l2.json"
                fig_name = f"{energy_key}_fit.l2.png"

                json_path = self.layout.get_ec_l2_json(json_name)
                write_json(json_path, records)

                fig_path = self.layout.get_ec_l2_figure(fig_name)
                self._plot_energy_file(energy_key, plot_rows, fig_path)

                results["outputs"].append({
                    "energy": energy_key,
                    "data_type": data_type,
                    "n_records": len(records),
                    "json": json_path.name,
                    "figure": fig_path.name,
                })
                results["n_processed"] += 1
                print(f"✓ EC L2: {energy_key} ({len(records)} records) -> {fig_path.name}")
            except Exception as exc:
                results["errors"].append({"energy": energy_key, "error": str(exc)})
                print(f"✗ Error in EC L2 {energy_key}: {exc}")

        write_json(self.layout.get_ec_l2_json("L2_manifest.json"), results)
        return results

    def _load_source_windows(self) -> dict[str, Any]:
        """读取源谱手选窗口配置（每源、每通道、每峰）。

        文件路径：``calibration_lib/resources/ec_source_windows_<payload>.json``
        结构：
        ::
            {
              "Co60": {
                "0": {"1173.2": [lo, hi], "1332.5": [lo, hi]},
                "1": {...}, "2": {...}, "3": {...}
              },
              "Na22": {...}
            }

        缺省时 EC L2 会回退到自动峰检测。
        """
        path = Path(__file__).resolve().parents[2] / "resources" / f"ec_source_windows_{self.payload_name}.json"
        if not path.exists():
            return {}
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_xray_windows(self) -> dict[str, Any]:
        """读取 X-ray 手选窗口配置。

        文件路径：``calibration_lib/resources/ec_xray_windows_<payload>.json``
        结构：``{energy_key: {channel: {lo, hi}}}``
        """
        path = Path(__file__).resolve().parents[2] / "resources" / f"ec_xray_windows_{self.payload_name}.json"
        if not path.exists():
            return {}
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _xray_consensus_center(self, channel_fits: dict[int, dict[str, Any]]) -> float | None:
        centers = []
        for _, fit in channel_fits.items():
            if fit.get("fit_quality", {}).get("accepted") is False:
                continue
            r2 = float(fit.get("rsquared", 0.0))
            center = float(fit.get("center", 0.0))
            center_err = float(abs(fit.get("center_err", float("nan"))))
            if r2 >= 0.6 and center > 0 and np.isfinite(center_err) and center_err < center * 0.5:
                centers.append(center)
        if len(centers) >= 2:
            return float(np.median(centers))
        if len(centers) == 1:
            return float(centers[0])
        return None

    def _energy_sort_key(self, energy_key: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)keV", energy_key)
        return float(match.group(1)) if match else float("inf")

    def _duration_from_time_axis(self, time_axis: np.ndarray) -> float:
        if time_axis.size < 2:
            return 1.0
        dt = float(np.max(time_axis) - np.min(time_axis))
        return dt if dt > 0 else 1.0

    def _build_rate_spectrum(
        self,
        amplitude: np.ndarray,
        time_axis: np.ndarray,
        edges: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        counts, _ = np.histogram(amplitude, bins=edges)
        duration = self._duration_from_time_axis(time_axis)
        rate = counts.astype(float) / duration
        err = np.sqrt(np.maximum(counts, 1.0)) / duration
        mids = 0.5 * (edges[:-1] + edges[1:])
        return rate, err, mids

    def _merge_bins_for_display(
        self,
        x: np.ndarray,
        y: np.ndarray,
        merge: int = DISPLAY_BIN_MERGE,
    ) -> tuple[np.ndarray, np.ndarray]:
        if merge <= 1 or x.size < merge or y.size < merge:
            return x, y
        n = (x.size // merge) * merge
        if n == 0:
            return x, y
        x_view = x[:n].reshape(-1, merge).mean(axis=1)
        y_view = y[:n].reshape(-1, merge).mean(axis=1)
        if n < x.size:
            x_view = np.concatenate([x_view, np.array([np.mean(x[n:])], dtype=float)])
            y_view = np.concatenate([y_view, np.array([np.mean(y[n:])], dtype=float)])
        return x_view, y_view

    def _smooth_hist(self, y: np.ndarray) -> np.ndarray:
        if y.size < 7:
            return y.astype(float)
        window = min(21, y.size if y.size % 2 == 1 else y.size - 1)
        window = max(window, 7)
        return savgol_filter(y.astype(float), window_length=window, polyorder=2)

    def _score_background_candidate(
        self,
        signal_spec: np.ndarray,
        background_spec: np.ndarray,
    ) -> float:
        net = signal_spec - background_spec
        smooth = self._smooth_hist(np.clip(net, a_min=0.0, a_max=None))
        peak = float(np.nanmax(smooth)) if smooth.size else 0.0
        neg_penalty = float(np.sum(np.clip(-net, a_min=0.0, a_max=None)))
        pos_area = float(np.sum(np.clip(net, a_min=0.0, a_max=None)))
        return peak + 0.05 * pos_area - 0.25 * neg_penalty

    def _choose_background_channel(
        self,
        target_channel: int,
        channel_map: dict[int, dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        signal_df = channel_map[target_channel]["df"]
        signal_amp = signal_df["amp_corr"].to_numpy(dtype=float)
        signal_time = signal_df["utc"].to_numpy(dtype=float)

        upper = max(
            float(np.percentile(signal_amp, 99.8)) if signal_amp.size else 160.0,
            *(
                float(np.percentile(channel_map[ch]["df"]["amp_corr"].to_numpy(dtype=float), 99.5))
                if len(channel_map[ch]["df"]) > 0
                else 160.0
                for ch in channel_map
                if ch != target_channel
            ),
        ) + 50.0
        upper = max(upper, 160.0)
        edges = np.arange(0.0, upper + BIN_WIDTH, BIN_WIDTH)
        signal_spec, _, _ = self._build_rate_spectrum(signal_amp, signal_time, edges)

        best_ch: int | None = None
        best_score = -np.inf
        for ch, item in channel_map.items():
            if ch == target_channel:
                continue
            bkg_df = item["df"]
            bkg_spec, _, _ = self._build_rate_spectrum(
                bkg_df["amp_corr"].to_numpy(dtype=float),
                bkg_df["utc"].to_numpy(dtype=float),
                edges,
            )
            score = self._score_background_candidate(signal_spec, bkg_spec)
            if score > best_score:
                best_score = score
                best_ch = ch

        if best_ch is None:
            raise RuntimeError(f"failed to choose background for channel {target_channel}")
        return best_ch, channel_map[best_ch]

    def _auto_fit_window(
        self,
        x: np.ndarray,
        y: np.ndarray,
        bin_width: float,
        min_adc: float | None = None,
    ) -> tuple[float, float, float]:
        settings = {"noise_ceiling": min_adc} if min_adc is not None else {}
        candidate, _ = detect_photopeak(x, y, config=settings)
        return *candidate.window, candidate.center

    def _calculate_rsquared(self, y_data: np.ndarray, y_fit: np.ndarray) -> float:
        ss_res = float(np.sum((y_data - y_fit) ** 2))
        ss_tot = float(np.sum((y_data - np.mean(y_data)) ** 2))
        if ss_tot <= 0:
            return 0.0
        return 1.0 - ss_res / ss_tot

    def _fit_with_mode(
        self,
        mode: str,
        x: np.ndarray,
        y: np.ndarray,
        err: np.ndarray,
        peak_x: float,
        lo: float,
        hi: float,
        bin_width: float,
        sigma_guess: float | None = None,
    ):
        if mode == "gaus":
            model = GaussianModel(prefix="g_")
        elif mode == "lin + gaus":
            model = GaussianModel(prefix="g_") + LinearModel(prefix="b_")
        elif mode == "exp + gaus":
            model = GaussianModel(prefix="g_") + ExponentialModel(prefix="e_")
        else:
            raise ValueError(mode)

        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        sigma_guess = sigma_guess or max((hi - lo) / 8.0, bin_width)
        params["g_sigma"].set(value=sigma_guess, min=max(0.5 * bin_width, sigma_guess / 3), max=3 * sigma_guess)
        params["g_amplitude"].set(
            value=float(np.max(y) * max((hi - lo) / 4.0, bin_width) * np.sqrt(2.0 * np.pi)),
            min=0.0,
        )
        if "b_intercept" in params:
            params["b_intercept"].set(value=float(max(np.percentile(y, 20), 0.0)), min=0.0)
            params["b_slope"].set(value=0.0)
        if "e_amplitude" in params:
            params["e_amplitude"].set(value=float(max(np.percentile(y, 25), 1e-9)), min=0.0)
            params["e_decay"].set(value=max((hi - lo) / 3.0, 5.0), min=1.0, max=2000.0)

        weights = 1.0 / np.maximum(err, np.finfo(float).eps)
        result = model.fit(y, params, x=x, weights=weights)
        if not result.success:
            raise RuntimeError(f"{mode} fit failed")
        return result, self._calculate_rsquared(y, result.best_fit)

    def _select_best_fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        err: np.ndarray,
        peak_x: float,
        lo: float,
        hi: float,
        bin_width: float,
        sigma_guess: float | None = None,
    ):
        candidates = []
        for mode in ("gaus", "lin + gaus", "exp + gaus"):
            try:
                result, rsq = self._fit_with_mode(mode, x, y, err, peak_x, lo, hi, bin_width, sigma_guess)
                criterion = float(np.sum(((y - result.best_fit) / err) ** 2)) + 2 * result.nvarys
                candidates.append((mode, result, rsq, criterion))
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("all fit modes failed")
        candidates.sort(key=lambda item: (
            not all(item[1].params[name].stderr is not None and np.isfinite(item[1].params[name].stderr)
                    and item[1].params[name].stderr > 0 for name in ("g_center", "g_sigma", "g_amplitude")),
            item[3],
        ))
        return candidates[0]

    def _prepare_spectrum(
        self, signal_df, background_df, bin_width: float,
    ) -> dict[str, Any]:
        """Shared rate histogram for fitting and interactive inspection.

        Duration normalization intentionally follows the existing processor's
        UTC-axis rule. Keeping one implementation prevents UI spectra from
        using counts or exposure scaling inconsistent with the actual fit.
        """
        signal_df, signal_quality = validated_events(signal_df)
        background_df, background_quality = validated_events(background_df)
        signal_quality = signal_df.attrs.get("crc_input_quality", signal_quality)
        background_quality = background_df.attrs.get("crc_input_quality", background_quality)
        signal_amp = signal_df["amp_corr"].to_numpy(dtype=float)
        signal_time = signal_df["utc"].to_numpy(dtype=float)
        bkg_amp = background_df["amp_corr"].to_numpy(dtype=float)
        bkg_time = background_df["utc"].to_numpy(dtype=float)
        upper = max(
            float(np.percentile(signal_amp, 99.9)) if signal_amp.size else 160.0,
            float(np.percentile(bkg_amp, 99.5)) if bkg_amp.size else 160.0,
        ) + 50.0
        edges = np.arange(0.0, max(upper, 160.0) + bin_width, bin_width)
        spec, spec_err, mids = self._build_rate_spectrum(signal_amp, signal_time, edges)
        bkg_spec, bkg_err, _ = self._build_rate_spectrum(bkg_amp, bkg_time, edges)
        return {"mids": mids, "spec": spec, "bkg_spec": bkg_spec,
                "net": spec - bkg_spec, "net_err": np.sqrt(spec_err**2 + bkg_err**2),
                "signal_input_quality": signal_quality, "background_input_quality": background_quality}

    def _fit_energy_spectrum(
        self,
        energy_key: str,
        signal_df,
        background_df,
        channel: int,
        background_channel: int,
        bin_width: float,
        forced_window: list[float] | None = None,
        forced_peak_energy: float | None = None,
        peak_config: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        prepared = self._prepare_spectrum(signal_df, background_df, bin_width)
        mids, spec, bkg_spec, net, net_err = (
            prepared[key] for key in ("mids", "spec", "bkg_spec", "net", "net_err")
        )

        candidate, detection = detect_photopeak(mids, net, net_err, window=forced_window, config=peak_config)
        peak_x = candidate.center
        if forced_window is not None:
            lo, hi = map(float, forced_window)
        else:
            lo, hi = candidate.window
            if detection["noise_boundary"] is not None:
                lo = max(lo, detection["noise_boundary"])
        mask = (mids >= lo) & (mids <= hi)
        x_fit = mids[mask]
        y_fit = net[mask]
        err_fit = np.maximum(net_err[mask], np.finfo(float).eps)

        if len(x_fit) < 8:
            raise RuntimeError("Resolved peak has too few fit bins; reduce bin_width or select a wider window")
        mode, result, rsquared, _ = self._select_best_fit(
            x_fit, y_fit, err_fit, peak_x, max(lo, peak_x - 1.5 * candidate.sigma),
            min(hi, peak_x + 1.5 * candidate.sigma), bin_width, candidate.sigma,
        )
        quality = fit_quality(result, x_fit, y_fit, err_fit)
        comps = result.eval_components(x=x_fit)

        center = float(result.params["g_center"].value)
        sigma = float(abs(result.params["g_sigma"].value))
        amplitude = float(result.params["g_amplitude"].value)
        center_err = float(abs(result.params["g_center"].stderr or np.nan))
        sigma_err = float(abs(result.params["g_sigma"].stderr or np.nan))
        amplitude_err = float(abs(result.params["g_amplitude"].stderr or np.nan))
        fwhm = float(2.0 * np.sqrt(2.0 * np.log(2.0)) * sigma)
        fwhm_err = float(2.0 * np.sqrt(2.0 * np.log(2.0)) * sigma_err) if np.isfinite(sigma_err) else float("nan")
        res = float(fwhm / center * 100.0) if center > 0 else 0.0
        res_err = (
            float(
                100.0
                * 2.0
                * np.sqrt(2.0 * np.log(2.0))
                * np.sqrt((sigma_err / center) ** 2 + ((sigma * center_err) / (center**2)) ** 2)
            )
            if np.isfinite(center_err) and np.isfinite(sigma_err) and center != 0
            else float("nan")
        )

        background_fit = np.zeros_like(x_fit)
        if "b_" in comps:
            background_fit += comps.get("b_", 0)
        if "e_" in comps:
            background_fit += comps.get("e_", 0)

        nominal_energy = float(forced_peak_energy) if forced_peak_energy is not None else self._energy_sort_key(energy_key)
        fit = {
            "E": nominal_energy,
            "center": center,
            "center_err": center_err,
            "sigma": sigma,
            "sigma_err": sigma_err,
            "amplitude": amplitude,
            "amplitude_err": amplitude_err,
            "resolution": res,
            "resolution_err": res_err,
            "FWHM": fwhm,
            "FWHM_err": fwhm_err,
            "lo": float(lo),
            "hi": float(hi),
            "fit_mode": mode,
            "rsquared": float(rsquared),
            "n_events": int(len(signal_df)),
            "n_bkg_events": int(len(background_df)),
            "background_channel": background_channel,
            "background_rate_mean": float(np.mean(bkg_spec)),
            "channel": channel,
            "model_parameters": {name: float(param.value) for name, param in result.params.items()},
            "peak_detection": detection,
            "fit_quality": quality,
            "signal_input_quality": prepared["signal_input_quality"],
            "background_input_quality": prepared["background_input_quality"],
        }
        plot_data = {
            "mids": mids,
            "spec": spec,
            "bkg_spec": bkg_spec,
            "net": net,
            "x_fit": x_fit,
            "best_fit": result.best_fit,
            "background_fit": background_fit,
        }
        return fit, plot_data

    def _plot_energy_file(
        self,
        energy_key: str,
        plot_rows: list[dict[str, Any]],
        fig_path: Path,
    ) -> None:
        """每文件绘图：x 射线 4 通道排成 2x2；源谱多峰时按 (channel, peak) 排成网格。"""
        from ...common.plotting import plot_spectrum_fit, style_axes, style_legend, style_title

        fig_path.parent.mkdir(parents=True, exist_ok=True)
        if not plot_rows:
            return

        n = len(plot_rows)
        ncols = 2 if n <= 4 else 2
        nrows = max(1, (n + ncols - 1) // ncols)
        fig, axes = plt.subplots(nrows, ncols, figsize=(12.5, max(4.0, 4.0 * nrows)), sharex=False)
        axes = np.atleast_1d(axes).ravel()

        for ax, item in zip(axes, plot_rows, strict=False):
            row = item["record"]
            pdata = item["plot"]
            mids_view, spec_view = self._merge_bins_for_display(pdata["mids"], pdata["spec"])
            _, bkg_view = self._merge_bins_for_display(pdata["mids"], pdata["bkg_spec"])
            _, net_view = self._merge_bins_for_display(pdata["mids"], pdata["net"])

            plot_spectrum_fit(
                ax,
                mids=mids_view,
                raw=spec_view,
                bkg_spec=bkg_view,
                net_spec=net_view,
                best_fit=pdata["best_fit"],
                background_fit=pdata["background_fit"],
                fit_x=pdata["x_fit"],
                fit_lo=row["lo"],
                fit_hi=row["hi"],
                center=row["center"],
                raw_label=f"raw ({DISPLAY_BIN_MERGE}x bin)",
                bkg_label=f"bkg ch{row['background_channel']}",
                net_label="net",
            )
            style_title(
                ax,
                f"Ch{row['channel']}  E={row['E']:.1f}keV  center={row['center']:.2f}±{row['center_err']:.2f}\n"
                f"mode={row['fit_mode']}, R²={row['rsquared']:.4f}",
            )
            style_axes(ax, xlabel="Corrected ADC", ylabel="Count Rate")

            fit_width = max(row["hi"] - row["lo"], 8.0 * max(row["sigma"], BIN_WIDTH), 60.0)
            x_lo = 0.0
            x_hi = min(float(pdata["mids"][-1]), max(row["hi"], row["center"] + 1.25 * fit_width) + 0.25 * fit_width)
            ax.set_xlim(x_lo, x_hi)
            style_legend(ax)

        for ax in axes[len(plot_rows):]:
            ax.set_visible(False)

        fig.suptitle(f"{self.payload_name}  {energy_key}", fontsize=13)
        fig.subplots_adjust(left=0.07, right=0.98, bottom=0.08, top=0.92, wspace=0.20, hspace=0.30)
        fig.savefig(fig_path, dpi=180)
        plt.close(fig)

    def _plot_4channels(
        self,
        energy_key: str,
        fit_rows: list[dict[str, Any]],
        plot_rows: list[dict[str, np.ndarray]],
        fig_path: Path,
    ) -> None:
        fig_path.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.5), sharex=False)
        axes = axes.ravel()

        for ax, row, pdata in zip(axes, fit_rows, plot_rows, strict=False):
            mids_view, spec_view = self._merge_bins_for_display(pdata["mids"], pdata["spec"])
            _, bkg_view = self._merge_bins_for_display(pdata["mids"], pdata["bkg_spec"])
            _, net_view = self._merge_bins_for_display(pdata["mids"], pdata["net"])

            ax.step(mids_view, spec_view, where="mid", color="#8e9aaf", linewidth=1.0, label=f"raw ({DISPLAY_BIN_MERGE}x bin)")
            ax.step(mids_view, bkg_view, where="mid", color="#d1495b", linewidth=1.0, alpha=0.9, label=f"bkg ch{row['background_channel']}")
            ax.step(mids_view, net_view, where="mid", color="#2b2d42", linewidth=1.15, label="net")
            ax.plot(pdata["x_fit"], pdata["best_fit"], "k--", label="Fit")
            ax.plot(pdata["x_fit"], pdata["background_fit"], "--", label="background")
            ax.vlines([row["lo"], row["hi"]], 0, max(pdata["spec"]), colors="r", linestyles="dashed", label="Fit Range")

            ax.set_title(
                f"Ch{row['channel']}  center={row['center']:.2f} +/- {row['center_err']:.2f}\n"
                f"mode={row['fit_mode']}, R^2={row['rsquared']:.4f}",
                fontsize=10,
            )
            ax.set_xlabel("Corrected ADC")
            ax.set_ylabel("Count Rate")
            ax.grid(alpha=0.2, linewidth=0.5)
            ax.legend(fontsize=8, loc="upper right")

            fit_width = max(row["hi"] - row["lo"], 8.0 * max(row["sigma"], BIN_WIDTH), 60.0)
            x_lo = 0.0
            x_hi = min(float(pdata["mids"][-1]), max(row["hi"], row["center"] + 1.25 * fit_width) + 0.25 * fit_width)
            ax.set_xlim(x_lo, x_hi)

            local_series = [
                spec_view[(mids_view >= x_lo) & (mids_view <= x_hi)],
                bkg_view[(mids_view >= x_lo) & (mids_view <= x_hi)],
                net_view[(mids_view >= x_lo) & (mids_view <= x_hi)],
                pdata["best_fit"],
            ]
            y_max = max(float(np.nanmax(arr)) for arr in local_series if arr.size)
            y_min = min(float(np.nanmin(arr)) for arr in local_series if arr.size)
            y_pad = max(0.08 * (y_max - y_min), 0.03 * max(abs(y_max), 1.0), 1e-3)
            ax.set_ylim(y_min - y_pad, y_max + y_pad)

        fig.suptitle(f"{self.payload_name} X-ray {energy_key}", fontsize=14)
        fig.subplots_adjust(left=0.07, right=0.98, bottom=0.08, top=0.90, wspace=0.20, hspace=0.28)
        fig.savefig(fig_path, dpi=180)
        plt.close(fig)
