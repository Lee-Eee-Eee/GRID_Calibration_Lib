"""能量标定(EC)处理 - L2层处理器。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel
from scipy.signal import find_peaks, savgol_filter

from ...common.utils import DataLayout, get_timestamp, read_parquet, read_parquet_metadata, write_json


BIN_WIDTH = 4.0
DISPLAY_BIN_MERGE = 3


class ECL2Processor:
    """EC L2层处理器 - 兼容旧 X-ray 流程的背景扣除、拟合与绘图。"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(self, ec_l1_dir: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = config or {}
        bin_width = float(cfg.get("bin_width", BIN_WIDTH))

        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L2",
            "timestamp": get_timestamp(),
            "bin_width": bin_width,
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        parquet_dir = ec_l1_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L1 parquet directory not found: {parquet_dir}")

        grouped_by_energy: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for parquet_file in sorted(parquet_dir.glob("*_corrected.parquet")):
            if "bkg_corrected" in parquet_file.name:
                continue
            try:
                df = read_parquet(parquet_file)
                meta = read_parquet_metadata(parquet_file) or {}
                data_type = meta.get("data_type", "xray")
                
                if data_type == "source":
                    source_name = meta.get("source_name", "unknown")
                    energy = meta.get("energy_keV", "0")
                    energy_key = f"src_{energy}keV_{source_name}"
                    
                    bkg_info = meta.get("background_info", {})
                    bkg_parquet_name = bkg_info.get("bkg_parquet") if isinstance(bkg_info, dict) else None
                    bkg_df = None
                    if bkg_parquet_name:
                        bkg_path = ec_l1_dir / "parquet" / bkg_parquet_name
                        if bkg_path.exists():
                            from ...common.utils import read_parquet as _rq
                            bkg_df = _rq(bkg_path)
                            
                    for ch in df["ch"].unique():
                        df_ch = df[df["ch"] == ch].copy()
                        if len(df_ch) < 10:
                            continue
                        
                        bkg_df_ch = None
                        if bkg_df is not None:
                            bkg_df_ch = bkg_df[bkg_df["ch"] == ch].copy()
                        
                        grouped_by_energy.setdefault(energy_key, {})[ch] = {
                            "stem": f"{source_name}_ch{ch}",
                            "df": df_ch,
                            "bkg_df": bkg_df_ch,
                            "parquet": parquet_file.name,
                            "data_type": "source"
                        }
                else:
                    stem = parquet_file.stem.replace("_corrected", "")
                    parts = stem.rsplit("_ch", 1)
                    if len(parts) == 2:
                        energy_key = parts[0]
                        ch = int(parts[1])
                        if len(df) >= 10:
                            grouped_by_energy.setdefault(energy_key, {})[ch] = {
                                "stem": stem,
                                "df": df,
                                "bkg_df": None,
                                "parquet": parquet_file.name,
                                "data_type": "xray"
                            }
            except Exception as exc:
                results["errors"].append({"file": parquet_file.name, "error": str(exc)})
                print(f"✗ Error: {parquet_file.name} - {exc}")

        for energy_key, ch_map in sorted(grouped_by_energy.items(), key=lambda item: self._energy_sort_key(item[0])):
            if set(ch_map) != {0, 1, 2, 3}:
                print(f"⚠ EC L2: {energy_key} - not all 4 channels present, skipping")
                continue

            fit_results: Dict[str, Dict[str, Any]] = {}
            fit_rows: List[Dict[str, Any]] = []
            plot_rows: List[Dict[str, np.ndarray]] = []

            try:
                for ch in range(4):
                    signal = ch_map[ch]
                    if signal.get("data_type") == "source" and signal.get("bkg_df") is not None:
                        bkg_df = signal["bkg_df"]
                        if len(bkg_df) == 0:
                            bkg_df = signal["df"].iloc[0:0] # empty df with same shape
                        bkg = {"df": bkg_df}
                        bkg_ch = ch
                    else:
                        bkg_ch, bkg = self._choose_background_channel(ch, ch_map)
                        
                    fit, plot_data = self._fit_energy_spectrum(
                        energy_key=energy_key,
                        signal_df=signal["df"],
                        background_df=bkg["df"],
                        channel=ch,
                        background_channel=bkg_ch,
                        bin_width=bin_width,
                    )
                    fit_results[str(ch)] = fit
                    fit_rows.append(fit)
                    plot_rows.append(plot_data)

                    ch_json = self.layout.get_ec_l2_json(f"{energy_key}_ch{ch}_l2_fit_results.json")
                    write_json(
                        ch_json,
                        {
                            "energy": energy_key,
                            "channel": ch,
                            "background_channel": bkg_ch,
                            "bin_width": bin_width,
                            "channels": {str(ch): fit},
                        },
                    )

                fig_path = self.layout.get_ec_l2_figure(f"{energy_key}_all_channels.png")
                self._plot_4channels(energy_key, fit_rows, plot_rows, fig_path)

                json_path = self.layout.get_ec_l2_json(f"{energy_key}_l2_fit_results.json")
                write_json(
                    json_path,
                    {
                        "energy": energy_key,
                        "bin_width": bin_width,
                        "channels": fit_results,
                    },
                )

                results["outputs"].append(
                    {
                        "energy": energy_key,
                        "channels_fitted": 4,
                        "json": json_path.name,
                        "figure": fig_path.name,
                    }
                )
                results["n_processed"] += 1
                print(f"✓ EC L2: {energy_key} (4 channels) -> {fig_path.name}")
            except Exception as exc:
                results["errors"].append({"energy": energy_key, "error": str(exc)})
                print(f"✗ Error in EC L2 {energy_key}: {exc}")

        write_json(self.layout.get_ec_l2_json("L2_manifest.json"), results)
        return results

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
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
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
    ) -> Tuple[np.ndarray, np.ndarray]:
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
        channel_map: Dict[int, Dict[str, Any]],
    ) -> Tuple[int, Dict[str, Any]]:
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
        upper = min(max(upper, 160.0), 5000.0)
        edges = np.arange(0.0, upper + BIN_WIDTH, BIN_WIDTH)
        signal_spec, _, _ = self._build_rate_spectrum(signal_amp, signal_time, edges)

        best_ch: Optional[int] = None
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
    ) -> Tuple[float, float, float]:
        smooth = self._smooth_hist(np.clip(y, a_min=0.0, a_max=None))
        if not np.any(np.isfinite(smooth)) or np.nanmax(smooth) <= 0:
            raise RuntimeError("empty spectrum after background subtraction")

        peaks, props = find_peaks(
            smooth,
            prominence=max(float(np.nanmax(smooth)) * 0.06, np.nanmax(smooth) * 0.02 + 1e-9),
            distance=max(4, int(18 / max(bin_width, 1e-6))),
        )
        peak_idx = int(peaks[np.argmax(props["prominences"])]) if len(peaks) else int(np.nanargmax(smooth))

        threshold = smooth[peak_idx] * 0.18
        left = peak_idx
        right = peak_idx
        while left > 1 and smooth[left] > threshold:
            left -= 1
        while right < len(smooth) - 2 and smooth[right] > threshold:
            right += 1
        left = max(0, left - 4)
        right = min(len(smooth) - 1, right + 6)
        if right - left < 10:
            left = max(0, peak_idx - 8)
            right = min(len(x) - 1, peak_idx + 10)
        return float(x[left]), float(x[right]), float(x[peak_idx])

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
        params["g_sigma"].set(value=max((hi - lo) / 8.0, bin_width), min=0.5, max=max((hi - lo) / 2.0, 2.0))
        params["g_amplitude"].set(
            value=float(np.max(y) * max((hi - lo) / 4.0, bin_width) * np.sqrt(2.0 * np.pi)),
            min=0.0,
        )
        if "b_" in params:
            params["b_intercept"].set(value=float(max(np.percentile(y, 20), 0.0)), min=0.0)
            params["b_slope"].set(value=0.0)
        if "e_" in params:
            params["e_amplitude"].set(value=float(max(np.percentile(y, 25), 1e-9)), min=0.0)
            params["e_decay"].set(value=max((hi - lo) / 3.0, 5.0), min=1.0, max=2000.0)

        floor = np.nanmedian(err[err > 0]) if np.any(err > 0) else 1.0
        weights = 1.0 / np.maximum(err, floor)
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
    ):
        candidates = []
        for mode in ("lin + gaus", "exp + gaus"):
            try:
                result, rsq = self._fit_with_mode(mode, x, y, err, peak_x, lo, hi, bin_width)
                candidates.append((mode, result, rsq, float(result.aic)))
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("all fit modes failed")
        candidates.sort(key=lambda item: (-item[2], item[3]))
        return candidates[0]

    def _fit_energy_spectrum(
        self,
        energy_key: str,
        signal_df,
        background_df,
        channel: int,
        background_channel: int,
        bin_width: float,
    ) -> Tuple[Dict[str, Any], Dict[str, np.ndarray]]:
        signal_amp = signal_df["amp_corr"].to_numpy(dtype=float)
        signal_time = signal_df["utc"].to_numpy(dtype=float)
        bkg_amp = background_df["amp_corr"].to_numpy(dtype=float)
        bkg_time = background_df["utc"].to_numpy(dtype=float)

        upper = max(
            float(np.percentile(signal_amp, 99.9)) if signal_amp.size else 160.0,
            float(np.percentile(bkg_amp, 99.5)) if bkg_amp.size else 160.0,
        ) + 50.0
        upper = min(max(upper, 160.0), 5000.0)
        edges = np.arange(0.0, upper + bin_width, bin_width)

        spec, spec_err, mids = self._build_rate_spectrum(signal_amp, signal_time, edges)
        bkg_spec, bkg_err, _ = self._build_rate_spectrum(bkg_amp, bkg_time, edges)
        net = spec - bkg_spec
        net_err = np.sqrt(spec_err**2 + bkg_err**2)

        lo, hi, peak_x = self._auto_fit_window(mids, net, bin_width)
        mask = (mids >= lo) & (mids <= hi)
        x_fit = mids[mask]
        y_fit = net[mask]
        err_fit = np.maximum(net_err[mask], np.nanmedian(net_err[mask][net_err[mask] > 0]) if np.any(net_err[mask] > 0) else 1.0)

        mode, result, rsquared, _ = self._select_best_fit(x_fit, y_fit, err_fit, peak_x, lo, hi, bin_width)
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
        if "b_" in result.params:
            background_fit += comps.get("b_", 0)
        if "e_" in result.params:
            background_fit += comps.get("e_", 0)

        nominal_energy = self._energy_sort_key(energy_key)
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

    def _plot_4channels(
        self,
        energy_key: str,
        fit_rows: List[Dict[str, Any]],
        plot_rows: List[Dict[str, np.ndarray]],
        fig_path: Path,
    ) -> None:
        fig_path.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.5), sharex=False)
        axes = axes.ravel()

        for ax, row, pdata in zip(axes, fit_rows, plot_rows):
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
