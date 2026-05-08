"""温度偏压(TB)标定 - L1层处理器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel
from scipy.signal import find_peaks, savgol_filter

from ...common.utils import (
    DataLayout,
    get_timestamp,
    read_json,
    read_parquet,
    read_parquet_metadata,
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
    amplitude: float
    slope: Optional[float]
    intercept: Optional[float]
    exp_amplitude: Optional[float]
    exp_decay: Optional[float]
    quad_a: Optional[float]
    quad_b: Optional[float]
    quad_c: Optional[float]
    fwhm: float
    height: float
    fit_mode: str
    lo: float
    hi: float
    rsquared: float


class TBL1Processor:
    """TB L1层处理器 - 兼容旧流程拟合与绘图。"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(
        self,
        tb_l0_dir: Path,
        config: Optional[Dict[str, Any]] = None,
        only_stems: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
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
        used_windows: Dict[str, Dict[str, Dict[str, Any]]] = {}

        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "TB_L1",
            "timestamp": get_timestamp(),
            "auto_window": auto_window,
            "fit_windows_config": str(win_path) if win_path else None,
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        parquet_dir = tb_l0_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {parquet_dir}")

        only_set = {s.strip() for s in only_stems if s and s.strip()} if only_stems else None
        partial_run = only_set is not None

        all_parquets = sorted(parquet_dir.glob("*.parquet"))
        if partial_run:
            parquet_files = [p for p in all_parquets if p.stem in only_set]
            missing = only_set - {p.stem for p in parquet_files}
            if missing:
                print(f"⚠ 以下 stem 在 L0 parquet 中找不到: {sorted(missing)}")
            if not parquet_files:
                raise ValueError(f"No matching parquet for stems={sorted(only_set)}")
            print(f"[partial] L1 only re-running {len(parquet_files)} stem(s): "
                  f"{[p.stem for p in parquet_files]}")
        else:
            parquet_files = all_parquets

        for parquet_file in parquet_files:
            try:
                df = read_parquet(parquet_file)
                stem = parquet_file.stem
                meta = read_parquet_metadata(parquet_file)
                channels_meta = meta.get("channels", {}) if isinstance(meta, dict) else {}

                fit_results: Dict[str, Dict[str, Any]] = {}
                legacy_rows = []
                used_windows.setdefault(stem, {})

                fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
                axes_flat = axes.flatten()

                for ch in range(4):
                    amp_data = df[df["ch"] == ch]["amp"].to_numpy(dtype=float)
                    if len(amp_data) < 10:
                        continue

                    fit_res = self._fit_channel(stem, ch, amp_data, manual_windows, auto_window)

                    is_manual = bool(self._is_manual_entry(manual_windows.get(stem, {}).get(str(ch), {})))

                    fit_results[str(ch)] = {
                        "center": float(fit_res.center),
                        "center_err": float(fit_res.center_err) if np.isfinite(fit_res.center_err) else 0.0,
                        "sigma": float(fit_res.sigma),
                        "amplitude": float(fit_res.amplitude),
                        "fwhm": float(fit_res.fwhm),
                        "height": float(fit_res.height),
                        "window_lo": float(fit_res.lo),
                        "window_hi": float(fit_res.hi),
                        "window_manual": is_manual,
                        "fit_mode": fit_res.fit_mode,
                        "rsquared": float(fit_res.rsquared),
                        "n_events": int(len(amp_data)),
                    }
                    if fit_res.slope is not None:
                        fit_results[str(ch)]["slope"] = float(fit_res.slope)
                    if fit_res.intercept is not None:
                        fit_results[str(ch)]["intercept"] = float(fit_res.intercept)
                    if fit_res.exp_amplitude is not None:
                        fit_results[str(ch)]["exp_amplitude"] = float(fit_res.exp_amplitude)
                    if fit_res.exp_decay is not None:
                        fit_results[str(ch)]["exp_decay"] = float(fit_res.exp_decay)
                    if fit_res.quad_a is not None:
                        fit_results[str(ch)]["quad_a"] = float(fit_res.quad_a)
                        fit_results[str(ch)]["quad_b"] = float(fit_res.quad_b)
                        fit_results[str(ch)]["quad_c"] = float(fit_res.quad_c)

                    used_windows[stem][str(ch)] = {
                        "lo": float(fit_res.lo),
                        "hi": float(fit_res.hi),
                        "manual": is_manual,
                    }

                    ch_meta = channels_meta.get(str(ch), {}) if isinstance(channels_meta, dict) else {}
                    ch_temp = ch_meta.get("temp", float("nan"))
                    ch_bias = ch_meta.get("bias", float("nan"))

                    legacy_row = {
                        "amplitude": float(fit_res.amplitude),
                        "center": float(fit_res.center),
                        "center_err": float(fit_res.center_err) if np.isfinite(fit_res.center_err) else 0.0,
                        "sigma": float(fit_res.sigma),
                        "slope": float(fit_res.slope) if fit_res.slope is not None else 0.0,
                        "intercept": float(fit_res.intercept) if fit_res.intercept is not None else 0.0,
                        "fwhm": float(fit_res.fwhm),
                        "height": float(fit_res.height),
                        "channel": ch,
                        "average_temp": float(ch_temp) if isinstance(ch_temp, (int, float)) else float("nan"),
                        "sipm_voltage": float(ch_bias) if isinstance(ch_bias, (int, float)) else float("nan"),
                        "lo": float(fit_res.lo),
                        "hi": float(fit_res.hi),
                        "fit_mode": fit_res.fit_mode,
                        "rsquared": float(fit_res.rsquared),
                    }
                    if fit_res.exp_amplitude is not None:
                        legacy_row["exp_amplitude"] = float(fit_res.exp_amplitude)
                    if fit_res.exp_decay is not None:
                        legacy_row["exp_decay"] = float(fit_res.exp_decay)
                    if fit_res.quad_a is not None:
                        legacy_row["quad_a"] = float(fit_res.quad_a)
                        legacy_row["quad_b"] = float(fit_res.quad_b)
                        legacy_row["quad_c"] = float(fit_res.quad_c)
                    legacy_rows.append(legacy_row)

                    ax = axes_flat[ch]
                    self._plot_channel(ax, ch, fit_res)

                temp_mean, bias_mean = self._extract_tb_file_mean(meta)

                fig.suptitle(f"{stem} | Fit mode: auto-select", fontsize=14)
                fig_path = self.layout.get_tb_l1_figure(f"{stem}_fit.jpg")
                fig.savefig(fig_path, dpi=150, format="jpg")
                plt.close(fig)

                json_path = self.layout.get_tb_l1_json(f"{stem}_fit_results.json")
                write_json(
                    json_path,
                    {
                        "stem": stem,
                        "temp_mean": temp_mean,
                        "bias_mean": bias_mean,
                        "channels": fit_results,
                        "legacy_rows": legacy_rows,
                        "figure": fig_path.name,
                    },
                )

                results["outputs"].append(
                    {
                        "stem": stem,
                        "channels_fitted": len(fit_results),
                        "json": json_path.name,
                        "figure": fig_path.name,
                    }
                )
                results["n_processed"] += 1
                print(f"✓ L1: {stem} ({len(fit_results)} channels)")
            except Exception as exc:
                results["errors"].append({"file": parquet_file.name, "error": str(exc)})
                print(f"✗ Error: {parquet_file.name} - {exc}")

        # partial run 时只合并更新本次涉及的 stem，避免清掉之前其他文件的快照/清单
        snap_path = self.layout.get_tb_l1_json("fit_windows_used.json")
        manifest_path = self.layout.get_tb_l1_json("L1_manifest.json")
        if partial_run:
            existing_snap = self._load_windows(snap_path) if snap_path.exists() else {}
            existing_snap.update(used_windows)
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

    def _is_manual_entry(self, item: Dict[str, Any]) -> bool:
        """判断窗口条目是否为 resource 中提供的拟合窗口。

        legacy 项目里只要 ``lo`` / ``hi`` 同时存在就视为"已配置"，``manual`` 字段
        只在交互式编辑器里做显示标识，不参与逻辑判断。这里沿用 legacy 行为：
        有 lo/hi 就用，否则走自动峰检测；同时 L1 不再回写 resource。
        """
        if not isinstance(item, dict):
            return False
        return "lo" in item and "hi" in item

    def _load_windows(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _extract_tb_file_mean(self, metadata: Dict[str, Any]) -> Tuple[float, float]:
        channels = metadata.get("channels", {}) if isinstance(metadata, dict) else {}
        temp_vals = []
        bias_vals = []
        for ch in range(4):
            item = channels.get(str(ch), {}) if isinstance(channels, dict) else {}
            t = item.get("temp")
            v = item.get("bias")
            if isinstance(t, (int, float)) and np.isfinite(t):
                temp_vals.append(float(t))
            if isinstance(v, (int, float)) and np.isfinite(v):
                bias_vals.append(float(v))
        temp_mean = float(np.mean(temp_vals)) if temp_vals else float("nan")
        bias_mean = float(np.mean(bias_vals)) if bias_vals else float("nan")
        return temp_mean, bias_mean

    def _fit_channel(
        self,
        stem: str,
        ch: int,
        energy: np.ndarray,
        config: Dict[str, Any],
        auto_window: bool,
    ) -> FitResult:
        bin_lo = 0
        bin_hi = int(max(420, np.percentile(energy, 99.8) + 40))
        bin_hi = min(1200, bin_hi)
        bins = np.arange(bin_lo, bin_hi + 1, 1)
        hist, edges = np.histogram(energy, bins=bins)
        mids = (edges[:-1] + edges[1:]) / 2.0

        window = 11 if len(hist) >= 11 else max(5, len(hist) // 2 * 2 + 1)
        hist_smooth = np.asarray(
            savgol_filter(hist.astype(float), window_length=window, polyorder=2),
            dtype=float,
        )

        item = config.get(stem, {}).get(str(ch), {}) if config else {}
        manual = self._is_manual_entry(item)

        if manual:
            lo = float(item["lo"])
            hi = float(item["hi"])

            cut_mask = (mids >= lo) & (mids <= hi)
            if not np.any(cut_mask):
                raise RuntimeError("Manual window out of bounds")

            y_cut_temp = hist_smooth[cut_mask]
            x_cut_temp = mids[cut_mask]
            peak_x = float(x_cut_temp[np.argmax(y_cut_temp)])
        else:
            peaks, props = find_peaks(
                hist_smooth,
                prominence=max(15, float(np.max(hist_smooth)) * 0.03),
                distance=18,
            )
            if len(peaks) == 0:
                raise RuntimeError("No peak found")

            peak_floor = 70
            valid = peaks[mids[peaks] >= peak_floor]
            if len(valid) > 0:
                valid_idx = [int(np.where(peaks == p)[0][0]) for p in valid]
                pick = int(valid[np.argmax(props["prominences"][valid_idx])])
            else:
                pick = int(peaks[np.argmax(props["prominences"])])

            peak_x = float(mids[pick])
            center_idx = int(np.argmin(np.abs(mids - peak_x)))

            left_idx = self._find_valley_left(hist_smooth, center_idx, span=85)
            right_idx = self._find_valley_right(hist_smooth, center_idx, span=110)
            if right_idx - left_idx < 22:
                left_idx = max(0, center_idx - 45)
                right_idx = min(len(mids) - 1, center_idx + 55)

            auto_lo = float(mids[left_idx])
            auto_hi = float(mids[right_idx])

            lo, hi = self._get_fit_window(item, auto_lo, auto_hi, auto_window)

        cut_mask = (mids >= lo) & (mids <= hi)
        x_cut = mids[cut_mask]
        y_cut = hist[cut_mask]
        if x_cut.size < 8 or np.max(y_cut) <= 0:
            raise RuntimeError("Invalid fit window")

        result, comps, fit_mode, rsquared = self._select_best_fit(x_cut, y_cut, peak_x, lo, hi)

        center = float(result.params["g_center"].value)
        center_err = float(result.params["g_center"].stderr or 0.0)
        sigma = float(result.params["g_sigma"].value)
        amplitude = float(result.params["g_amplitude"].value)
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
            amplitude=amplitude,
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
        )

    def _get_fit_window(
        self,
        item: Dict[str, Any],
        auto_lo: float,
        auto_hi: float,
        auto_window: bool,
    ) -> Tuple[float, float]:
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

    def _fit_lin_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float):
        model = GaussianModel(prefix="g_") + LinearModel(prefix="b_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=12.0, min=1.0, max=90.0)
        params["g_amplitude"].set(value=float(np.max(y_cut) * 12.0 * np.sqrt(2.0 * np.pi)), min=0.0)
        params["b_intercept"].set(value=float(max(np.percentile(y_cut, 20), 0.0)), min=0.0)
        params["b_slope"].set(value=0.0)

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_exp_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float):
        model = GaussianModel(prefix="g_") + ExponentialModel(prefix="e_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=12.0, min=1.0, max=90.0)
        params["g_amplitude"].set(value=float(np.max(y_cut) * 12.0 * np.sqrt(2.0 * np.pi)), min=0.0)
        params["e_amplitude"].set(value=float(max(np.percentile(y_cut, 20), 1.0)), min=0.0)
        params["e_decay"].set(value=100.0, min=1.0, max=1000.0)

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_quad_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float):
        model = GaussianModel(prefix="g_") + QuadraticModel(prefix="q_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=12.0, min=1.0, max=90.0)
        params["g_amplitude"].set(value=float(np.max(y_cut) * 12.0 * np.sqrt(2.0 * np.pi)), min=0.0)
        params["q_a"].set(value=0.0)
        params["q_b"].set(value=0.0)
        params["q_c"].set(value=float(max(np.percentile(y_cut, 20), 0.0)))

        weights = 1.0 / np.sqrt(np.maximum(y_cut, 1.0))
        result = model.fit(y_cut, params, x=x_cut, weights=weights)
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_gaus_only(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float):
        model = GaussianModel(prefix="g_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=12.0, min=1.0, max=90.0)
        params["g_amplitude"].set(value=float(np.max(y_cut) * 12.0 * np.sqrt(2.0 * np.pi)), min=0.0)

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
    ):
        """对齐原古项目 calibration_process：单高斯峰 + 4 选 1 单背景。

        候选: gaus | gaus + lin | gaus + exp | gaus + quad
        策略: 同时拟合，按 R² 降序、AIC 升序挑最佳。
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
                result, comps = fit_fn(x_cut, y_cut, peak_x, lo, hi)
                rsq = self._calculate_rsquared(y_cut, result.best_fit)
                aic = float(getattr(result, "aic", np.inf))
                results.append((result, comps, mode, rsq, aic))
            except Exception:
                continue

        if not results:
            raise RuntimeError("All fit modes failed")

        results.sort(key=lambda x: (-x[3], x[4]))
        best = results[0]
        return best[0], best[1], best[2], best[3]

    def _plot_channel(self, ax_spec, ch: int, fit_res: FitResult) -> None:
        ax_spec.step(fit_res.mids, fit_res.hist, where="mid", lw=1.0, color="0.5", label="all spectrum")
        ax_spec.plot(fit_res.x_cut, fit_res.y_cut, color="C1", lw=1.6, label="cut_data")
        ax_spec.plot(fit_res.x_cut, fit_res.best_fit, "k--", label="Fit")
        ax_spec.plot(fit_res.x_cut, fit_res.gauss_fit, "--", label="gaussian")
        ax_spec.plot(fit_res.x_cut, fit_res.background_fit, "--", label="background")

        ax_spec.axvline(fit_res.center, color="r", ls="--", lw=1.0)
        ax_spec.vlines([fit_res.lo, fit_res.hi], 0, max(fit_res.hist), colors="r", linestyles="dashed", label="Fit Range")

        ax_spec.set_title(
            f"ch{ch}: center={fit_res.center:.2f}, sigma={fit_res.sigma:.2f}, mode={fit_res.fit_mode}",
            fontsize=10,
        )
        ax_spec.set_xlabel("ADC unit")
        ax_spec.set_ylabel("counts")

        # 自适应 xlim：从 0 起，到能容纳整个拟合窗口、峰右尾以及最后一个非零计数 bin
        # 的较大者，再留 ~10% 余量。窄峰也能撑满，宽谱也不被截断。
        sigma_safe = max(float(fit_res.sigma), 1.0)
        right_from_peak = float(fit_res.center) + 6.0 * sigma_safe
        right_from_window = float(fit_res.hi) * 1.15
        nonzero_idx = np.flatnonzero(fit_res.hist > 0)
        right_from_data = float(fit_res.mids[nonzero_idx[-1]]) * 1.05 if nonzero_idx.size else right_from_window
        x_max = max(right_from_peak, right_from_window, min(right_from_data, float(np.max(fit_res.mids))))
        # 至少 60 ADC 宽度，避免极窄峰下视图过窄
        x_max = max(x_max, float(fit_res.center) + 30.0)
        ax_spec.set_xlim(0, x_max)
        ax_spec.grid(ls="--", alpha=0.25)
        ax_spec.legend(fontsize=7)
