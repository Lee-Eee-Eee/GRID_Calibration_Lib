"""温度偏压(TB)标定 - L2层处理器。"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
from scipy.optimize import curve_fit

from ...common.utils import DataLayout, get_timestamp, read_json, read_parquet_metadata, write_json


def _empty_tb_l2_record() -> Dict[str, float]:
    """对齐 cali_format/wiki 规范的空记录占位（11 字段全 0）。"""
    return {
        "G0": 0.0, "k": 0.0, "V0": 0.0, "b": 0.0, "c": 0.0,
        "G0_err": 0.0, "k_err": 0.0, "V0_err": 0.0, "b_err": 0.0, "c_err": 0.0,
        "chisquare": 0.0,
    }


class TBL2Processor:
    """TB L2层处理器 - 温偏曲面拟合。

    模型:
        G(T,V) = G0 * (V - k*T - V0)^2 * (-T^2 + b*T + c)

    k 作为自由拟合参数，初始 0.0184，约束 [0.001, 0.05]；其余参数初值/边界亦
    与 calibration_process.util_lib.temp_bias_lmfit 对齐。拟合采用 1/center_err
    作为权重（由 L1 提供 center_err，缺失时回退为 sqrt(center) 经验权重）。
    论文 Eq. 8 给出 k = 0.0215 V/°C，结果应在该值附近。
    """

    # 与 calibration_process.util_lib.temp_bias_lmfit 一致
    K_INIT = 0.0184
    K_LO, K_HI = 0.001, 0.05
    G0_INIT = 0.012
    G0_LO, G0_HI = 1e-5, 100.0
    V0_INIT = 24.11
    V0_LO, V0_HI = 23.0, 25.0
    B_INIT = 54.31
    B_LO, B_HI = -500.0, 500.0
    C_INIT = 23459.83
    C_LO, C_HI = 1e3, 1e8

    # L1 拟合质量低于此 R² 的点不参与曲面拟合（仍作 outlier 提示）
    MIN_RSQUARED = 0.5

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(
        self,
        tb_l1_dir: Path,
        tb_l0_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        l0_dir = tb_l0_dir if tb_l0_dir is not None else self.layout.get_tb_l0_dir()

        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "TB_L2",
            "timestamp": get_timestamp(),
            "k_init": self.K_INIT,
            "fit_params": None,
            "output_json": None,
            "output_figures": [],
            "errors": [],
        }

        try:
            fit_data = self._collect_fit_data(tb_l1_dir, l0_dir)
            if not fit_data:
                raise ValueError("No TB L1/L0 matched data for L2 fit")

            timestamp = get_timestamp()
            params_list = []
            files_list = []

            for ch in range(4):
                ch_data = [d for d in fit_data if d["channel"] == ch]
                if len(ch_data) < 8:
                    print(f"Channel {ch} has too few data points ({len(ch_data)}) for L2 fit.")
                    params_list.append(None)
                    continue

                params = self._fit_surface(ch_data)
                params["channel"] = ch
                params["payload"] = self.payload_name
                params_list.append(params)

                out_fig = self.layout.get_tb_l2_figure(f"{timestamp}_{self.payload_name}_ch{ch}_TB_fit.l2.png")
                self._plot_channel_fit(ch_data, params, out_fig, ch)
                files_list.append(out_fig.name)

            results["fit_params"] = params_list

            # cali_format wiki 规范：list[4]，每条 11 个字段 {G0,k,V0,b,c,*_err,chisquare}
            wiki_records = []
            for p in params_list:
                if p is None:
                    wiki_records.append(_empty_tb_l2_record())
                else:
                    wiki_records.append({
                        "G0": float(p["G0"]),
                        "k": float(p["k"]),
                        "V0": float(p["V0"]),
                        "b": float(p["b"]),
                        "c": float(p["c"]),
                        "G0_err": float(p["G0_err"]),
                        "k_err": float(p["k_err"]),
                        "V0_err": float(p["V0_err"]),
                        "b_err": float(p["b_err"]),
                        "c_err": float(p["c_err"]),
                        "chisquare": float(p.get("chi2", 0.0)),
                    })
            # 补齐到 4 条（如有通道未拟合）
            while len(wiki_records) < 4:
                wiki_records.append(_empty_tb_l2_record())

            out_json = self.layout.get_tb_l2_json(f"{timestamp}_{self.payload_name}_TB.l2.json")
            write_json(out_json, wiki_records)
            results["output_json"] = out_json.name
            results["output_figures"] = files_list

            print(f"TB L2: fitted {len(fit_data)} total points across {sum(p is not None for p in params_list)} channels")
        except Exception as exc:
            results["errors"].append(str(exc))
            print(f"Error in L2: {exc}")

        write_json(self.layout.get_tb_l2_json("L2_manifest.json"), results)
        return results

    def _collect_fit_data(self, tb_l1_dir: Path, tb_l0_dir: Path) -> List[Dict[str, Any]]:
        """从 L1 拟合结果 + L0 metadata 中收集每通道独立的 T/V/gain/center_err。"""
        json_dir = tb_l1_dir / "json"
        l0_parquet_dir = tb_l0_dir / "parquet"
        if not json_dir.exists():
            raise ValueError(f"L1 json directory not found: {json_dir}")
        if not l0_parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {l0_parquet_dir}")

        rows: List[Dict[str, Any]] = []
        for l1_file in sorted(json_dir.glob("*_fit_results.l1.json")):
            data = read_json(l1_file)
            # 优先用 list-of-4 schema；若是旧版 dict 嵌套，兼容读取
            if isinstance(data, list):
                channel_records = data
                stem = l1_file.stem.replace("_fit_results.l1", "")
            else:
                stem = data.get("stem") or l1_file.stem.replace("_fit_results.l1", "")
                channel_records = []
                for ch_str, ch_result in (data.get("channels") or {}).items():
                    rec = dict(ch_result)
                    rec["channel"] = int(ch_str)
                    channel_records.append(rec)

            # T/V 优先取记录里的 temp/bias（list-of-4 schema 已带），缺失再回退 L0 metadata
            per_ch_tv: Dict[int, Dict[str, float]] = {}
            for rec in channel_records:
                ch = int(rec.get("channel", -1))
                if ch < 0:
                    continue
                t = rec.get("temp")
                v = rec.get("bias")
                if isinstance(t, (int, float)) and np.isfinite(t) and isinstance(v, (int, float)) and np.isfinite(v):
                    per_ch_tv[ch] = {"temp": float(t), "bias": float(v)}

            if not per_ch_tv:
                pq_path = l0_parquet_dir / f"{stem}.l0.parquet"
                if not pq_path.exists():
                    pq_path = l0_parquet_dir / f"{stem}.parquet"  # 向后兼容旧产物
                if pq_path.exists():
                    meta = read_parquet_metadata(pq_path, meta_key="hk_data")
                    temps = meta.get("temp", []) if isinstance(meta, dict) else []
                    biases = meta.get("bias", []) if isinstance(meta, dict) else []
                    for ch in range(4):
                        t = temps[ch] if ch < len(temps) else float("nan")
                        b = biases[ch] if ch < len(biases) else float("nan")
                        if isinstance(t, (int, float)) and np.isfinite(t) and isinstance(b, (int, float)) and np.isfinite(b):
                            per_ch_tv[ch] = {"temp": float(t), "bias": float(b)}

            if not per_ch_tv:
                continue

            for rec in channel_records:
                ch = int(rec.get("channel", -1))
                if ch not in per_ch_tv:
                    continue
                gain = rec.get("center", 0.0)
                gain_err = rec.get("center_err", 0.0)
                rsq = rec.get("rsquared", 0.0)
                if not (np.isfinite(gain) and gain > 0):
                    continue
                if np.isfinite(rsq) and rsq < self.MIN_RSQUARED:
                    continue
                rows.append({
                    "temp": per_ch_tv[ch]["temp"],
                    "bias": per_ch_tv[ch]["bias"],
                    "gain": float(gain),
                    "gain_err": float(gain_err) if np.isfinite(gain_err) and gain_err > 0 else 0.0,
                    "channel": ch,
                    "file": str(stem),
                })
        return rows

    def _fit_surface(self, fit_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """5 参数模型: G(T,V) = G0 * (V - k*T - V0)^2 * (-T^2 + b*T + c)"""
        temps = np.array([d["temp"] for d in fit_data], dtype=float)
        biases = np.array([d["bias"] for d in fit_data], dtype=float)
        gains = np.array([d["gain"] for d in fit_data], dtype=float)
        gains_err = np.array([d["gain_err"] for d in fit_data], dtype=float)

        # center_err 缺失时回退到 sqrt(gain)（poisson-ish proxy），保证全部有正值
        sigma = np.where(gains_err > 0, gains_err, np.sqrt(np.maximum(gains, 1.0)))

        x_data = np.column_stack([temps, biases])

        def model(x, g0, v0, b, c, k):
            t = x[:, 0]; v = x[:, 1]
            vov = v - k * t - v0
            return g0 * (vov ** 2) * (-(t ** 2) + b * t + c)

        # 多起始点策略：第一组用 calibration_process 默认，后两组提供扰动
        p0_list = [
            [self.G0_INIT, self.V0_INIT, self.B_INIT, self.C_INIT, self.K_INIT],
            [self.G0_INIT * 0.5, 23.5, 30.0, 20000.0, 0.020],
            [self.G0_INIT * 2.0, 24.5, 80.0, 30000.0, 0.022],
        ]
        lower = [self.G0_LO, self.V0_LO, self.B_LO, self.C_LO, self.K_LO]
        upper = [self.G0_HI, self.V0_HI, self.B_HI, self.C_HI, self.K_HI]

        best_popt = None; best_pcov = None; best_chi2 = np.inf

        for p0 in p0_list:
            try:
                popt, pcov = curve_fit(
                    model, x_data, gains,
                    p0=p0,
                    sigma=sigma,
                    absolute_sigma=True,
                    bounds=(lower, upper),
                    maxfev=200000,
                )
                pred = model(x_data, *popt)
                chi2 = float(np.sum(((gains - pred) / sigma) ** 2))
                if chi2 < best_chi2:
                    best_chi2 = chi2
                    best_popt = popt
                    best_pcov = pcov
            except Exception:
                continue

        if best_popt is None:
            raise RuntimeError("All starting points failed for surface fit")

        popt = best_popt; pcov = best_pcov
        perr = np.sqrt(np.maximum(np.diag(pcov), 0.0))

        pred = model(x_data, *popt)
        residual = gains - pred
        rmse = float(np.sqrt(np.mean(residual ** 2)))
        mape = float(np.mean(np.abs(residual) / np.maximum(np.abs(gains), 1e-12)) * 100.0)
        dof = max(len(gains) - len(popt), 1)
        chi2_red = float(best_chi2 / dof)

        return {
            "G0": float(popt[0]),
            "V0": float(popt[1]),
            "b": float(popt[2]),
            "c": float(popt[3]),
            "k": float(popt[4]),
            "G0_err": float(perr[0]),
            "V0_err": float(perr[1]),
            "b_err": float(perr[2]),
            "c_err": float(perr[3]),
            "k_err": float(perr[4]),
            "chi2": float(best_chi2),
            "rmse": rmse,
            "mape_percent": mape,
            "chi2_reduced": chi2_red,
            "n_points": int(len(fit_data)),
        }

    def _plot_channel_fit(
        self,
        fit_data: List[Dict[str, Any]],
        fit_params: Dict[str, Any],
        output_path: Path,
        channel: int,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        temps = np.array([d["temp"] for d in fit_data], dtype=float)
        biases = np.array([d["bias"] for d in fit_data], dtype=float)
        gains = np.array([d["gain"] for d in fit_data], dtype=float)

        x_data = np.column_stack([temps, biases])

        def model(x):
            t = x[:, 0]; v = x[:, 1]
            k = fit_params["k"]
            vov = v - k * t - fit_params["V0"]
            return fit_params["G0"] * (vov ** 2) * (-(t ** 2) + fit_params["b"] * t + fit_params["c"])

        pred = model(x_data)
        residual = gains - pred

        fig = plt.figure(figsize=(12.5, 5.2))
        ax3d = fig.add_subplot(1, 2, 1, projection="3d")
        ax2d = fig.add_subplot(1, 2, 2)

        ax3d.scatter(temps, biases, gains, s=18, alpha=0.95, color="#1f77b4")
        t_min, t_max = float(np.min(temps)), float(np.max(temps))
        b_min, b_max = float(np.min(biases)), float(np.max(biases))
        tt, bb = np.meshgrid(
            np.linspace(t_min, t_max, 40),
            np.linspace(b_min, b_max, 40),
        )
        xx = np.stack([tt.ravel(), bb.ravel()], axis=1)
        zz = model(xx).reshape(tt.shape)

        ax3d.plot_surface(tt, bb, zz, alpha=0.48, cmap="viridis", linewidth=0, antialiased=True)
        ax3d.set_xlabel("Temperature (degC)")
        ax3d.set_ylabel("Bias (V)")
        ax3d.set_zlabel("Gain (peak center)")
        rmse = fit_params.get("rmse", 0.0)
        mape = fit_params.get("mape_percent", 0.0)
        k_val = fit_params.get("k", 0.0)
        chi2r = fit_params.get("chi2_reduced", 0.0)
        ax3d.set_title(
            f"{self.payload_name} Ch{channel}\n"
            f"k={k_val:.4f}, RMSE={rmse:.3f}, MAPE={mape:.2f}%, chi2/dof={chi2r:.2f}",
            fontsize=10,
        )
        ax3d.view_init(elev=24, azim=-58)

        triang = mtri.Triangulation(temps, biases)
        vmax = np.nanpercentile(np.abs(residual), 95)
        levels = np.linspace(-vmax, vmax, 41)
        contour = ax2d.tricontourf(triang, residual, levels=levels, cmap="RdBu_r", extend="both")
        ax2d.scatter(temps, biases, c="r", s=np.abs(residual))
        ax2d.set_xlabel("Temperature (degC)")
        ax2d.set_ylabel("Bias (V)")
        ax2d.set_title(f"{self.payload_name} Ch{channel} Residuals (data - fit)", fontsize=10)
        ax2d.grid(True, which="both", ls="--", lw=0.5)

        cbar = fig.colorbar(contour, ax=ax2d)
        cbar.set_label("Residual")

        plt.tight_layout()
        plt.savefig(output_path, dpi=180)
        plt.close(fig)
