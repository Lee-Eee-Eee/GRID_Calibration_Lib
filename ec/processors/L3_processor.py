"""能量标定(EC)处理 - L3层处理器。"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import lmfit
import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import QuadraticModel

from ...common.utils import DataLayout, get_timestamp, read_json, write_json


def res_fit(E, a, b, c):
    """Resolution model: R(E) = sqrt(a*E + b*E^2 + c) / E"""
    return np.sqrt(np.abs(a * E + b * E**2 + c)) / E


class ECL3Processor:
    """EC L3层处理器 - 能量标定与分辨率拟合。每个通道独立。"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(self, ec_l2_dir: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = config or {}
        e_cut = float(cfg.get("energy_split_low", 50.2))

        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L3",
            "timestamp": get_timestamp(),
            "energy_split": e_cut,
            "calibration_params": {},
            "output_json": None,
            "output_figures": [],
            "errors": [],
        }

        try:
            fit_data_all = self._collect_l2_results(ec_l2_dir)
            if not fit_data_all:
                raise ValueError("No L2 fit data collected")

            ts = get_timestamp()

            for ch in range(4):
                ch_data = [d for d in fit_data_all if d["channel"] == ch]
                if not ch_data:
                    results["calibration_params"][str(ch)] = None
                    continue

                ch_result = {"channel": ch}

                # --- EC quadratic fit (Energy vs ADC) --- single fit, no split ---
                energies = np.array([d["E"] for d in ch_data], dtype=float)
                centers = np.array([d["center"] for d in ch_data], dtype=float)
                center_errs = np.array([d.get("center_err", 0) for d in ch_data], dtype=float)
                sources = [d.get("source", "x") for d in ch_data]

                if len(ch_data) >= 5:
                    ec_result = self._fit_quadratic_weighted(centers, energies, center_errs)
                    ch_result["EC_low"] = [ec_result.params["a"].value, ec_result.params["b"].value, ec_result.params["c"].value]
                    ch_result["EC_low_err"] = [ec_result.params["a"].stderr, ec_result.params["b"].stderr, ec_result.params["c"].stderr]
                    ch_result["EC_high"] = ch_result["EC_low"]
                    ch_result["EC_high_err"] = ch_result["EC_low_err"]
                    all_ec_result = ec_result
                else:
                    ch_result["EC_low"] = None
                    ch_result["EC_high"] = None
                    all_ec_result = None

                # --- Resolution fit ---
                resolutions = np.array([d["resolution"] for d in ch_data], dtype=float)
                resolution_errs = np.array([d.get("resolution_err", 0) for d in ch_data], dtype=float)

                res_low_data = [(e, r, re_) for e, r, re_, s in zip(energies, resolutions, resolution_errs, sources) if e <= e_cut]
                res_high_data = [(e, r, re_, s) for e, r, re_, s in zip(energies, resolutions, resolution_errs, sources) if e > e_cut]

                if len(res_low_data) >= 3:
                    res_low_result = self._fit_resolution([d[0] for d in res_low_data], [d[1] for d in res_low_data])
                    ch_result["resolution_low"] = [res_low_result.params["a"].value, res_low_result.params["b"].value, res_low_result.params["c"].value]
                    ch_result["resolution_low_err"] = [res_low_result.params["a"].stderr, res_low_result.params["b"].stderr, res_low_result.params["c"].stderr]
                else:
                    ch_result["resolution_low"] = None

                if len(res_high_data) >= 3:
                    res_high_result = self._fit_resolution([d[0] for d in res_high_data], [d[1] for d in res_high_data])
                    ch_result["resolution_high"] = [res_high_result.params["a"].value, res_high_result.params["b"].value, res_high_result.params["c"].value]
                    ch_result["resolution_high_err"] = [res_high_result.params["a"].stderr, res_high_result.params["b"].stderr, res_high_result.params["c"].stderr]
                else:
                    ch_result["resolution_high"] = None

                results["calibration_params"][str(ch)] = ch_result

                # --- EC fit plot ---
                ec_fig_path = self.layout.get_ec_l3_figure(f"{ts}_{self.payload_name}_ecfit_ch{ch}.l3.png")
                self._plot_ec_fit(ch, ch_data, all_ec_result, e_cut, ec_fig_path)
                results["output_figures"].append(ec_fig_path.name)

                # --- Resolution fit plot ---
                res_fig_path = self.layout.get_ec_l3_figure(f"{ts}_{self.payload_name}_resolution_fit_ch{ch}.l3.png")
                self._plot_resolution_fit(
                    ch, ch_data,
                    ch_result.get("resolution_low"), ch_result.get("resolution_high"),
                    e_cut, res_fig_path,
                )
                results["output_figures"].append(res_fig_path.name)

                # --- Per-channel JSON ---
                ch_json_path = self.layout.get_ec_l3_json(f"{ts}_{self.payload_name}_ec_coef_ch{ch}.l3.json")
                write_json(ch_json_path, ch_result)

            # --- Summary JSON ---
            out_json = self.layout.get_ec_l3_json(f"{ts}_{self.payload_name}_EC.l3.json")
            write_json(
                out_json,
                {
                    "timestamp": ts,
                    "payload": self.payload_name,
                    "energy_split": e_cut,
                    "calibration_params": results["calibration_params"],
                },
            )
            results["output_json"] = out_json.name
            print(f"✓ EC L3: calibration generated ({out_json.name})")

        except Exception as exc:
            results["errors"].append(str(exc))
            print(f"✗ Error in L3: {exc}")
            import traceback
            traceback.print_exc()

        write_json(self.layout.get_ec_l3_json("L3_manifest.json"), results)
        return results

    # ------------------------------------------------------------------ #
    # Data collection
    # ------------------------------------------------------------------ #

    def _collect_l2_results(self, ec_l2_dir: Path) -> List[Dict[str, Any]]:
        json_dir = ec_l2_dir / "json"
        if not json_dir.exists():
            raise ValueError(f"L2 json directory not found: {json_dir}")

        rows: List[Dict[str, Any]] = []
        # 优先扫新版 *_fit_results.l2.json；同时兼容旧版 *_l2_fit_results.json
        candidate_files = list(json_dir.glob("*_fit_results.l2.json"))
        if not candidate_files:
            candidate_files = [
                fp for fp in json_dir.glob("*_l2_fit_results.json")
                if not re.search(r"_ch\d+_l2_fit_results\.json$", fp.name)
            ]

        for fp in sorted(candidate_files):
            data = read_json(fp)

            # 新版：顶层 list[record]
            if isinstance(data, list):
                for rec in data:
                    if not isinstance(rec, dict):
                        continue
                    rows.append({
                        "E": float(rec.get("E", 0.0)),
                        "center": float(rec.get("center", 0.0)),
                        "center_err": float(rec.get("center_err", 0.0)),
                        "sigma": float(rec.get("sigma", 0.0)),
                        "resolution": float(rec.get("resolution", 0.0)),
                        "resolution_err": float(rec.get("resolution_err", 0.0)),
                        "FWHM": float(rec.get("fwhm", rec.get("FWHM", 0.0))),
                        "channel": int(rec.get("channel", 0)),
                        "source": str(rec.get("source", "x")) if rec.get("source") in ("x", "xray", "src") else "x",
                        "file": fp.stem,
                    })
                continue

            # 旧版：dict 嵌套
            energy_key = data.get("energy", fp.stem)
            is_source = str(energy_key).startswith("src_")
            for ch_str, ch_dict in data.get("channels", {}).items():
                rows.append({
                    "E": float(ch_dict.get("E", 0.0)),
                    "center": float(ch_dict.get("center", 0.0)),
                    "center_err": float(ch_dict.get("center_err", 0.0)),
                    "sigma": float(ch_dict.get("sigma", 0.0)),
                    "resolution": float(ch_dict.get("resolution", 0.0)),
                    "resolution_err": float(ch_dict.get("resolution_err", 0.0)),
                    "FWHM": float(ch_dict.get("FWHM", 0.0)),
                    "channel": int(ch_str),
                    "source": "src" if is_source else "x",
                    "file": fp.stem,
                })

        # 归一化 source 标识：xray / x → "x"，src → "src"
        for r in rows:
            r["source"] = "src" if r["source"] in ("src",) else "x"
        return sorted(rows, key=lambda x: x["E"])

    # ------------------------------------------------------------------ #
    # Fitting
    # ------------------------------------------------------------------ #

    def _fit_quadratic_weighted(self, x: list, y: list, yerr: list) -> lmfit.model.ModelResult:
        """Weighted quadratic fit: y = a*x^2 + b*x + c, weighted by 1/yerr."""
        mod = QuadraticModel()
        x_arr = np.array(x, dtype=float)
        y_arr = np.array(y, dtype=float)
        w_arr = np.array(yerr, dtype=float)
        w_arr = np.where(np.isfinite(w_arr) & (w_arr > 0), w_arr, 1.0)
        params = mod.guess(y_arr, x=x_arr)
        return mod.fit(y_arr, params, x=x_arr, weights=1.0 / w_arr)

    def _fit_quadratic(self, x: list, y: list) -> lmfit.model.ModelResult:
        """Quadratic fit: y = a*x^2 + b*x + c

        For EC calibration: x=ADC, y=Energy(keV).
        """
        mod = QuadraticModel()
        x_arr = np.array(x, dtype=float)
        y_arr = np.array(y, dtype=float)
        params = mod.guess(y_arr, x=x_arr)
        return mod.fit(y_arr, params, x=x_arr)

    def _fit_resolution(self, energies: list, resolutions: list) -> lmfit.model.ModelResult:
        """Resolution fit: R(E) = sqrt(a*E + b*E^2 + c) / E"""
        mod = lmfit.Model(res_fit)
        E = np.array(energies, dtype=float)
        R = np.array(resolutions, dtype=float)
        params = mod.make_params(a=1.0, b=0.0, c=0.0)
        return mod.fit(R, params, E=E)

    # ------------------------------------------------------------------ #
    # Plotting
    # ------------------------------------------------------------------ #

    def _plot_ec_fit(
        self,
        ch: int,
        ch_data: List[Dict[str, Any]],
        ec_result: Optional[lmfit.model.ModelResult],
        e_cut: float,
        fig_path: Path,
    ) -> None:
        fig_path.parent.mkdir(parents=True, exist_ok=True)
        from matplotlib import gridspec
        gs = gridspec.GridSpec(2, 1, wspace=0.5, hspace=0.2, height_ratios=[4, 1])
        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_subplot(gs[0])

        x_pts = [d for d in ch_data if d["source"] == "x"]
        src_pts = [d for d in ch_data if d["source"] == "src"]

        if x_pts:
            ax.errorbar(
                [d["center"] for d in x_pts], [d["E"] for d in x_pts],
                xerr=[d.get("center_err", 0) for d in x_pts],
                fmt="s", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=1, label=f"x CH{ch}",
            )
        if src_pts:
            ax.errorbar(
                [d["center"] for d in src_pts], [d["E"] for d in src_pts],
                xerr=[d.get("center_err", 0) for d in src_pts],
                fmt="^", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=0, label=f"source CH{ch}",
            )

        all_centers = np.asarray([d["center"] for d in ch_data])
        all_E = np.asarray([d["E"] for d in ch_data])
        low_centers = np.asarray([d["center"] for d in ch_data if d["E"] <= e_cut])
        high_centers = np.asarray([d["center"] for d in ch_data if d["E"] > e_cut])

        if ec_result is not None:
            if len(low_centers) > 0:
                adc_low = np.arange(np.min(low_centers), np.max(low_centers) + 1, 1)
                e_low = ec_result.eval(x=adc_low)
                ax.plot(adc_low, e_low, "r-", lw=1.5, label=f"quadratic < {e_cut}keV")
            if len(high_centers) > 0:
                adc_high = np.arange(np.min(high_centers), np.max(high_centers) + 1, 1)
                e_high = ec_result.eval(x=adc_high)
                ax.plot(adc_high, e_high, "g-", lw=1.5, label=f"quadratic > {e_cut}keV")

        ax.axhline(e_cut, color="gray", ls="--", lw=0.5)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("ADC")
        ax.set_ylabel("Energy (keV)")
        ax.set_ylim(10.0, 1500.0)
        ax.set_title(f"{self.payload_name} CH{ch} Energy Calibration")
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(True, which="both", ls="--", lw=0.5)

        ax_res = fig.add_subplot(gs[1])
        ax_res.axis("off")
        fig.subplots_adjust(left=0.12, right=0.97, bottom=0.06, top=0.93)
        plt.savefig(fig_path, dpi=150)
        plt.close(fig)

    def _plot_resolution_fit(
        self,
        ch: int,
        ch_data: List[Dict[str, Any]],
        res_low_params: Optional[list],
        res_high_params: Optional[list],
        e_cut: float,
        fig_path: Path,
    ) -> None:
        fig_path.parent.mkdir(parents=True, exist_ok=True)
        from matplotlib import gridspec
        gs = gridspec.GridSpec(2, 1, wspace=0.5, hspace=0.2, height_ratios=[4, 1])
        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_subplot(gs[0])

        x_pts = [d for d in ch_data if d["source"] == "x"]
        src_pts = [d for d in ch_data if d["source"] == "src"]

        if x_pts:
            ax.errorbar(
                [d["E"] for d in x_pts], [d["resolution"] for d in x_pts],
                yerr=[d.get("resolution_err", 0) for d in x_pts],
                fmt="s", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=1, label=f"x CH{ch}",
            )
        if src_pts:
            ax.errorbar(
                [d["E"] for d in src_pts], [d["resolution"] for d in src_pts],
                yerr=[d.get("resolution_err", 0) for d in src_pts],
                fmt="^", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=0, label=f"source CH{ch}",
            )

        all_E = np.array([d["E"] for d in ch_data])
        if res_low_params is not None:
            e_low = np.linspace(all_E.min(), e_cut, 100)
            r_low = res_fit(e_low, *res_low_params)
            ax.plot(e_low, r_low, "r--", label=f"resolution fit < {e_cut}keV")
        if res_high_params is not None:
            e_high = np.linspace(e_cut, all_E.max(), 100)
            r_high = res_fit(e_high, *res_high_params)
            ax.plot(e_high, r_high, "g--", label=f"resolution fit > {e_cut}keV")

        ax.axvline(e_cut, color="gray", ls="--", lw=0.5)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Energy (keV)")
        ax.set_ylabel("Resolution (%)")
        ax.set_ylim(0.5, 150.0)
        ax.set_title(f"{self.payload_name} CH{ch} Resolution")
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(True, which="both", ls="--", lw=0.5)

        ax_res = fig.add_subplot(gs[1])
        ax_res.axis("off")
        fig.subplots_adjust(left=0.12, right=0.97, bottom=0.06, top=0.93)
        plt.savefig(fig_path, dpi=150)
        plt.close(fig)
