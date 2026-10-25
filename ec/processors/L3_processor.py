"""能量标定(EC)处理 - L3层处理器。"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import lmfit
import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import QuadraticModel

from ...common.fit_policy import quality_warnings, unavailable_fit
from ...common.utils import DataLayout, get_timestamp, read_json, write_json

MIN_RSQUARED = 0.7
MAX_CENTER_RELERR = 0.15
MAX_RESOLUTION = 50.0


def res_fit(E, a, b, c):
    """Resolution model: R(E) = sqrt(a*E + b*E^2 + c) / E"""
    return np.sqrt(np.abs(a * E + b * E**2 + c)) / E


def _empty_channel_result(channel: int) -> Dict[str, Any]:
    """Keep unavailable fits explicit and compliant with the documented schema."""
    result: Dict[str, Any] = {"channel": channel}
    for name in ("EC_low", "EC_high", "resolution_low", "resolution_high"):
        result[name] = None
        result[f"{name}_err"] = [None, None, None]
    return result


def _number(value):
    """A damaged optional record must not stop the other channels."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


class ECL3Processor:
    """EC L3层处理器 - 能量标定与分辨率拟合。每个通道独立。"""

    def __init__(self, payload_name: str, product_root: Path, *, response_payload: str | None = None):
        self.payload_name = payload_name
        self.response_payload = response_payload or payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(self, ec_l2_dir: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ...common.spectral_models import unsupported_spectral_model
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
            "warnings": [],
            "quality_policy": "warning_only",
            "fit_weight_fallbacks": [],
            "excluded_points": sorted(set(cfg.get("ec_excluded", []))),
        }

        try:
            fit_data_all = self._collect_l2_results(ec_l2_dir)
            excluded = set(cfg.get("ec_excluded", []))
            fit_data_all = [row for row in fit_data_all if self._point_id(row) not in excluded]
            if not fit_data_all:
                raise ValueError("No L2 fit data collected")

            ts = get_timestamp()

            for ch in range(4):
                if unsupported_spectral_model(self.response_payload, ch):
                    results["calibration_params"][str(ch)] = _empty_channel_result(ch)
                    continue
                ch_data_raw = [d for d in fit_data_all if d["channel"] == ch]
                if not ch_data_raw:
                    results["calibration_params"][str(ch)] = _empty_channel_result(ch)
                    results["errors"].append(f"Channel {ch}: no L2 points available")
                    continue

                ch_data = self._filter_quality(ch_data_raw)
                if not ch_data:
                    print(f"⚠ EC L3 ch{ch}: no finite usable L2 parameters")
                    results["calibration_params"][str(ch)] = _empty_channel_result(ch)
                    results["errors"].append(f"Channel {ch}: no finite usable L2 parameters")
                    continue

                ch_data = self._filter_monotonic(ch_data)
                for row in ch_data:
                    if row.get("quality_warnings"):
                        results["warnings"].append(
                            f"Channel {ch}, {row['E']:g} keV: {'; '.join(row['quality_warnings'])}; retained for fitting"
                        )

                n_raw = len(ch_data_raw)
                n_kept = len(ch_data)
                if n_kept < n_raw:
                    print(f"  EC L3 ch{ch}: {n_kept}/{n_raw} points have usable parameters")

                try:
                    self._fit_channel(ch, ch_data, ch_data_raw, ts, e_cut, results)
                except Exception as exc:
                    results["calibration_params"].setdefault(str(ch), _empty_channel_result(ch))
                    results["errors"].append(f"Channel {ch}: {exc}")
                    print(f"⚠ EC L3 ch{ch}: {exc}; continuing other channels")

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

    def _fit_channel(self, ch, ch_data, ch_data_raw, ts, e_cut, results):
        # Error vectors remain part of the schema even when a segment
        # has too few points and its coefficient vector is null.
        ch_result = _empty_channel_result(ch)

        energies = np.array([d["E"] for d in ch_data], dtype=float)
        centers = np.array([d["center"] for d in ch_data], dtype=float)
        center_errs = self._effective_errors(ch_data, "center_err", results)
        resolution_errs = self._effective_errors(ch_data, "resolution_err", results)
        sources = [d["source"] for d in ch_data]

        if len(ch_data) >= 3:
            ec_result = self._fit_quadratic_weighted(centers, energies, center_errs)
            a_val = ec_result.params["a"].value
            adc_min, adc_max = float(np.min(centers)), float(np.max(centers))
            deriv_lo = 2 * a_val * adc_min + ec_result.params["b"].value
            deriv_hi = 2 * a_val * adc_max + ec_result.params["b"].value
            if deriv_lo <= 0 or deriv_hi <= 0:
                print(f"  EC L3 ch{ch}: quadratic non-monotonic (a={a_val:.6f}), falling back to linear")
                from lmfit.models import LinearModel
                lin_mod = LinearModel()
                lin_params = lin_mod.guess(energies, x=centers)
                ec_result = lin_mod.fit(energies, lin_params, x=centers, weights=1.0 / center_errs)
                ch_result["EC_low"] = [0.0, ec_result.params["slope"].value, ec_result.params["intercept"].value]
                ch_result["EC_low_err"] = [0.0, ec_result.params["slope"].stderr or 0.0, ec_result.params["intercept"].stderr or 0.0]
            else:
                ch_result["EC_low"] = [a_val, ec_result.params["b"].value, ec_result.params["c"].value]
                ch_result["EC_low_err"] = [ec_result.params["a"].stderr or 0.0, ec_result.params["b"].stderr or 0.0, ec_result.params["c"].stderr or 0.0]
            ch_result["EC_high"] = ch_result["EC_low"]
            ch_result["EC_high_err"] = ch_result["EC_low_err"]
            all_ec_result = ec_result
        else:
            ch_result["EC_low"] = None
            ch_result["EC_high"] = None
            all_ec_result = None

        resolutions = np.array([d["resolution"] for d in ch_data], dtype=float)

        res_low_data = [(e, r, re_) for e, r, re_, s in zip(energies, resolutions, resolution_errs, sources) if e <= e_cut]
        res_high_data = [(e, r, re_) for e, r, re_, s in zip(energies, resolutions, resolution_errs, sources) if e > e_cut]

        if len(res_low_data) >= 3:
            res_low_result = self._fit_resolution_weighted(
                [d[0] for d in res_low_data], [d[1] for d in res_low_data], [d[2] for d in res_low_data],
            )
            ch_result["resolution_low"] = [res_low_result.params["a"].value, res_low_result.params["b"].value, res_low_result.params["c"].value]
            ch_result["resolution_low_err"] = [res_low_result.params["a"].stderr or 0.0, res_low_result.params["b"].stderr or 0.0, res_low_result.params["c"].stderr or 0.0]
        else:
            ch_result["resolution_low"] = None

        if len(res_high_data) >= 3:
            res_high_result = self._fit_resolution_weighted(
                [d[0] for d in res_high_data], [d[1] for d in res_high_data], [d[2] for d in res_high_data],
            )
            ch_result["resolution_high"] = [res_high_result.params["a"].value, res_high_result.params["b"].value, res_high_result.params["c"].value]
            ch_result["resolution_high_err"] = [res_high_result.params["a"].stderr or 0.0, res_high_result.params["b"].stderr or 0.0, res_high_result.params["c"].stderr or 0.0]
        else:
            ch_result["resolution_high"] = None

        results["calibration_params"][str(ch)] = ch_result

        ec_fig_path = self.layout.get_ec_l3_figure(f"{ts}_{self.payload_name}_ecfit_ch{ch}.l3.png")
        self._plot_ec_fit(ch, ch_data, ch_data_raw, all_ec_result, e_cut, ec_fig_path)
        results["output_figures"].append(ec_fig_path.name)

        res_fig_path = self.layout.get_ec_l3_figure(f"{ts}_{self.payload_name}_resolution_fit_ch{ch}.l3.png")
        self._plot_resolution_fit(
            ch, ch_data,
            ch_result.get("resolution_low"), ch_result.get("resolution_high"),
            e_cut, res_fig_path,
        )
        results["output_figures"].append(res_fig_path.name)

        ch_json_path = self.layout.get_ec_l3_json(f"{ts}_{self.payload_name}_ec_coef_ch{ch}.l3.json")
        write_json(ch_json_path, ch_result)


    # ------------------------------------------------------------------ #
    # Data collection & quality filtering
    # ------------------------------------------------------------------ #

    def _point_id(self, row: dict[str, Any]) -> str:
        stem = str(row.get("file", "")).removesuffix(".l2").removesuffix("_fit_results")
        return f"{self.payload_name}:ec:{stem}:{int(row['channel'])}:{float(row['E']):g}"

    def _collect_l2_results(self, ec_l2_dir: Path) -> List[Dict[str, Any]]:
        json_dir = ec_l2_dir / "json"
        if not json_dir.exists():
            raise ValueError(f"L2 json directory not found: {json_dir}")

        rows: List[Dict[str, Any]] = []
        candidate_files = list(json_dir.glob("*_fit_results.l2.json"))
        if not candidate_files:
            candidate_files = [
                fp for fp in json_dir.glob("*_l2_fit_results.json")
                if not re.search(r"_ch\d+_l2_fit_results\.json$", fp.name)
            ]

        for fp in sorted(candidate_files):
            data = read_json(fp)

            if isinstance(data, list):
                for rec in data:
                    if not isinstance(rec, dict):
                        continue
                    rows.append({
                        "E": _number(rec.get("E")),
                        "center": _number(rec.get("center")),
                        "center_err": _number(rec.get("center_err")),
                        "sigma": _number(rec.get("sigma")),
                        "sigma_err": _number(rec.get("sigma_err")),
                        "resolution": _number(rec.get("resolution")),
                        "resolution_err": _number(rec.get("resolution_err")),
                        "FWHM": _number(rec.get("fwhm", rec.get("FWHM"))),
                        "channel": int(rec.get("channel", 0)),
                        "source": str(rec.get("source", "x")) if rec.get("source") in ("x", "xray", "src") else "x",
                        "rsquared": _number(rec.get("rsquared")),
                        "fit_quality": rec.get("fit_quality", {}),
                        "placeholder": rec.get("placeholder", False),
                        "fit_performed": rec.get("fit_performed", True),
                        "file": fp.stem,
                    })
                continue

            energy_key = data.get("energy", fp.stem)
            is_source = str(energy_key).startswith("src_")
            for ch_str, ch_dict in data.get("channels", {}).items():
                rows.append({
                    "E": _number(ch_dict.get("E")),
                    "center": _number(ch_dict.get("center")),
                    "center_err": _number(ch_dict.get("center_err")),
                    "sigma": _number(ch_dict.get("sigma")),
                    "sigma_err": _number(ch_dict.get("sigma_err")),
                    "resolution": _number(ch_dict.get("resolution")),
                    "resolution_err": _number(ch_dict.get("resolution_err")),
                    "FWHM": _number(ch_dict.get("FWHM")),
                    "channel": int(ch_str),
                    "source": "src" if is_source else "x",
                    "rsquared": _number(ch_dict.get("rsquared")),
                    "fit_quality": ch_dict.get("fit_quality", {}),
                    "placeholder": ch_dict.get("placeholder", False),
                    "fit_performed": ch_dict.get("fit_performed", True),
                    "file": fp.stem,
                })

        for r in rows:
            r["source"] = "src" if r["source"] in ("src",) else "x"
        return sorted(rows, key=lambda x: x["E"])

    @staticmethod
    def usable_record(record: dict[str, Any]) -> bool:
        """Only missing/nonnumeric fit inputs prevent numerical aggregation."""
        if unavailable_fit(record):
            return False
        return all(np.isfinite(value := _number(record.get(key))) and value > 0
                   for key in ("E", "center", "sigma", "resolution"))

    @staticmethod
    def quality_reasons(record: dict[str, Any]) -> list[str]:
        """Saved quality diagnostics are annotations, never vetoes."""
        reasons = quality_warnings(record)
        r2 = _number(record.get("rsquared"))
        if not np.isfinite(r2) or r2 < MIN_RSQUARED:
            reasons.append(f"R² < {MIN_RSQUARED}" if np.isfinite(r2) else "R² is missing")
        center, ce = _number(record.get("center")), _number(record.get("center_err"))
        if not np.isfinite(center) or center <= 0:
            reasons.append("center is missing or non-positive")
        if not np.isfinite(ce) or ce <= 0:
            reasons.append("center_err is missing or non-positive")
        elif center > 0 and ce / center > MAX_CENTER_RELERR:
            reasons.append(f"center relative error > {MAX_CENTER_RELERR:.0%}")
        sigma, se = _number(record.get("sigma")), _number(record.get("sigma_err"))
        if not np.isfinite(sigma) or sigma <= 0:
            reasons.append("sigma is missing or non-positive")
        if not np.isfinite(se) or se <= 0:
            reasons.append("sigma_err is missing or non-positive")
        elif sigma > 0 and se / sigma > 2:
            reasons.append("sigma relative error > 200%")
        resolution, re_ = _number(record.get("resolution")), _number(record.get("resolution_err"))
        if not np.isfinite(resolution) or resolution <= 0 or resolution > MAX_RESOLUTION:
            reasons.append(f"resolution outside (0, {MAX_RESOLUTION}] %")
        if not np.isfinite(re_) or re_ <= 0:
            reasons.append("resolution_err is missing or non-positive")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _filter_quality(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Retain every numerically usable point and annotate possible problems."""
        retained = []
        for row in rows:
            row["quality_warnings"] = ECL3Processor.quality_reasons(row)
            row["quality_suspect"] = bool(row["quality_warnings"])
            if ECL3Processor.usable_record(row):
                retained.append(row)
        return retained

    @staticmethod
    def _filter_monotonic(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(rows) < 2:
            return rows
        sorted_rows = sorted(rows, key=lambda d: d["E"])
        for i in range(1, len(sorted_rows)):
            if sorted_rows[i]["center"] <= sorted_rows[i - 1]["center"] * 0.99:
                warnings = sorted_rows[i].setdefault("quality_warnings", [])
                if "non-monotonic center" not in warnings:
                    warnings.append("non-monotonic center")
                sorted_rows[i]["quality_suspect"] = True
        return sorted_rows

    @staticmethod
    def _filter_adc_proximity(rows: List[Dict[str, Any]], adc_tol: float = 0.05, energy_ratio_min: float = 1.5) -> List[Dict[str, Any]]:
        if len(rows) < 2:
            return rows
        sorted_rows = sorted(rows, key=lambda d: d["center"])
        groups: List[List[Dict[str, Any]]] = []
        for r in sorted_rows:
            if groups and abs(r["center"] - groups[-1][-1]["center"]) / max(groups[-1][-1]["center"], 1.0) < adc_tol:
                groups[-1].append(r)
            else:
                groups.append([r])
        result = []
        for grp in groups:
            if len(grp) == 1:
                result.append(grp[0])
                continue
            energies = [g["E"] for g in grp]
            e_max = max(energies)
            e_min = max(min(energies), 0.01)
            if e_max / e_min < energy_ratio_min:
                result.extend(grp)
                continue
            for point in grp:
                warnings = point.setdefault("quality_warnings", [])
                if "ADC proximity of distinct energy lines" not in warnings:
                    warnings.append("ADC proximity of distinct energy lines")
                point["quality_suspect"] = True
            result.extend(grp)
        return sorted(result, key=lambda d: d["E"])

    # ------------------------------------------------------------------ #
    # Fitting
    # ------------------------------------------------------------------ #

    @staticmethod
    def _effective_errors(rows, field, results):
        """Fallback weights are derived fit inputs, never measured errors."""
        values = np.asarray([_number(row.get(field)) for row in rows])
        valid = np.isfinite(values) & (values > 0)
        fallback = float(np.median(values[valid])) if np.any(valid) else 1.
        source = "median_positive_error" if np.any(valid) else "unit_weight"
        for row, available in zip(rows, valid, strict=True):
            if not available:
                results["fit_weight_fallbacks"].append({
                    "channel": row["channel"], "E": row["E"], "file": row.get("file"),
                    "field": field, "source": source, "effective_uncertainty": fallback,
                })
        return np.where(valid, values, fallback)

    def _fit_quadratic_weighted(self, x: list, y: list, yerr: list) -> lmfit.model.ModelResult:
        """Weighted quadratic fit: y = a*x^2 + b*x + c, weighted by 1/yerr."""
        mod = QuadraticModel()
        x_arr = np.array(x, dtype=float)
        y_arr = np.array(y, dtype=float)
        w_arr = np.array(yerr, dtype=float)
        valid = np.isfinite(w_arr) & (w_arr > 0)
        w_arr = np.where(valid, w_arr, np.median(w_arr[valid]) if np.any(valid) else 1.0)
        params = mod.guess(y_arr, x=x_arr)
        return mod.fit(y_arr, params, x=x_arr, weights=1.0 / w_arr)

    def _fit_resolution_weighted(
        self, energies: list, resolutions: list, res_errs: list,
    ) -> lmfit.model.ModelResult:
        """Weighted resolution fit: R(E) = sqrt(a*E + b*E^2 + c) / E"""
        mod = lmfit.Model(res_fit)
        E = np.array(energies, dtype=float)
        R = np.array(resolutions, dtype=float)
        W = np.array(res_errs, dtype=float)
        valid = np.isfinite(W) & (W > 0)
        if np.any(valid):
            floor = np.nanmedian(W[valid])
            W = np.where(valid, W, floor)
        else:
            W = np.ones_like(W)
        R_var = np.var(R)
        E_var = np.var(E)
        a0 = R_var * E_var * 1e-4 if E_var > 0 else 1.0
        params = mod.make_params(a=a0, b=0.0, c=max(a0 * np.mean(E), 1.0))
        return mod.fit(R, params, E=E, weights=1.0 / W)

    # ------------------------------------------------------------------ #
    # Plotting
    # ------------------------------------------------------------------ #

    def _plot_ec_fit(
        self,
        ch: int,
        ch_data: List[Dict[str, Any]],
        ch_data_raw: List[Dict[str, Any]],
        ec_result: Optional[lmfit.model.ModelResult],
        e_cut: float,
        fig_path: Path,
    ) -> None:
        fig_path.parent.mkdir(parents=True, exist_ok=True)
        from matplotlib import gridspec
        gs = gridspec.GridSpec(2, 1, wspace=0.5, hspace=0.2, height_ratios=[4, 1])
        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_subplot(gs[0])

        unavailable = [d for d in ch_data_raw if d not in ch_data]
        if unavailable:
            ax.scatter(
                [d["center"] for d in unavailable], [d["E"] for d in unavailable],
                marker="x", c="lightgray", s=20, zorder=0, label="missing fit inputs",
            )

        x_pts = [d for d in ch_data if d["source"] == "x"]
        src_pts = [d for d in ch_data if d["source"] == "src"]

        if x_pts:
            ax.errorbar(
                [d["center"] for d in x_pts], [d["E"] for d in x_pts],
                xerr=[value if np.isfinite(value := _number(d.get("center_err"))) and value > 0 else 0 for d in x_pts],
                fmt="s", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=1, label=f"x CH{ch}",
            )
        if src_pts:
            ax.errorbar(
                [d["center"] for d in src_pts], [d["E"] for d in src_pts],
                xerr=[value if np.isfinite(value := _number(d.get("center_err"))) and value > 0 else 0 for d in src_pts],
                fmt="^", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=0, label=f"source CH{ch}",
            )

        suspect = [point for point in ch_data if point.get("quality_suspect")]
        if suspect:
            ax.scatter([point["center"] for point in suspect], [point["E"] for point in suspect],
                       marker="o", facecolors="none", edgecolors="#a87824", s=90, zorder=4,
                       label="review: included in fit")

        r2 = 0.0
        all_centers = np.asarray([d["center"] for d in ch_data])
        all_E = np.asarray([d["E"] for d in ch_data])

        if ec_result is not None and len(all_centers) > 0:
            adc_fit = np.linspace(np.min(all_centers), np.max(all_centers), 200)
            e_fit = ec_result.eval(x=adc_fit)
            ax.plot(adc_fit, e_fit, "r-", lw=2.0, zorder=10, label="quadratic fit")
            e_pred = ec_result.eval(x=all_centers)
            ss_res = np.sum((all_E - e_pred) ** 2)
            ss_tot = np.sum((all_E - np.mean(all_E)) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("ADC")
        ax.set_ylabel("Energy (keV)")
        ax.set_ylim(10.0, 1500.0)
        ax.set_title(f"{self.payload_name} CH{ch} Energy Calibration  (R²={r2:.4f}, {len(ch_data)}/{len(ch_data_raw)} pts)")
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
                yerr=[value if np.isfinite(value := _number(d.get("resolution_err"))) and value > 0 else 0 for d in x_pts],
                fmt="s", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=1, label=f"x CH{ch}",
            )
        if src_pts:
            ax.errorbar(
                [d["E"] for d in src_pts], [d["resolution"] for d in src_pts],
                yerr=[value if np.isfinite(value := _number(d.get("resolution_err"))) and value > 0 else 0 for d in src_pts],
                fmt="^", mfc="white", ms=6, elinewidth=1, capsize=3,
                barsabove=True, zorder=0, label=f"source CH{ch}",
            )

        suspect = [point for point in ch_data if point.get("quality_suspect")]
        if suspect:
            ax.scatter([point["E"] for point in suspect], [point["resolution"] for point in suspect],
                       marker="o", facecolors="none", edgecolors="#a87824", s=90, zorder=4,
                       label="review: included in fit")

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
