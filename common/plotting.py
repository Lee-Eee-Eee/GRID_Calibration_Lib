"""统一的 matplotlib 风格 helper —— TB / EC 各层绘图共用。

参照 calibration_process / calibration_lib 既有约定：
- raw spectrum：灰色 step
- 背景：红色 step
- 净谱 / cut data：橙色 step / 1.6 lw
- Fit (best)：黑色虚线
- 高斯峰组分：绿色虚线
- 背景组分：紫色虚线
- 拟合窗口：红色 dashed vline
- 中心位置：红色 dashed vline 也可（当 axhline / axvline 用）
- grid：`ls="--", alpha=0.25`，字号 10
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np


# ── 调色板 ───────────────────────────────────────────────────────────────
COLOR_RAW = "0.5"        # 灰
COLOR_BKG = "C3"         # 红
COLOR_NET = "C1"         # 橙
COLOR_CUT = "C1"         # 橙（"cut_data"）
COLOR_FIT = "k"          # 黑虚线
COLOR_GAUSS = "C2"       # 绿
COLOR_BKG_FIT = "C4"     # 紫
COLOR_FIT_RANGE = "r"    # 红
COLOR_CENTER = "r"       # 红

LINESTYLE_FIT = "--"
LINESTYLE_RANGE = "dashed"
LINESTYLE_GRID = "--"
GRID_ALPHA = 0.25
TITLE_FS = 10
LEGEND_FS = 7
LW_RAW = 1.0
LW_NET = 1.6
LW_FIT = 2.0


def style_axes(ax: plt.Axes, *, xlabel: str = "ADC unit", ylabel: str = "counts") -> None:
    """统一设置轴标签 + grid + 字号。"""
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(ls=LINESTYLE_GRID, alpha=GRID_ALPHA)


def adaptive_xlim(
    ax: plt.Axes,
    mids: np.ndarray,
    hist: np.ndarray,
    center: float,
    sigma: float,
    hi_window: float,
) -> Tuple[float, float]:
    """从 0 到 max(center+6σ, hi×1.15, 末位非零 bin×1.05)，至少 center+30。"""
    sigma_safe = max(float(sigma), 1.0)
    right_from_peak = float(center) + 6.0 * sigma_safe
    right_from_window = float(hi_window) * 1.15
    nonzero = np.flatnonzero(np.asarray(hist) > 0)
    right_from_data = float(mids[nonzero[-1]]) * 1.05 if nonzero.size else right_from_window
    x_max = max(right_from_peak, right_from_window, min(right_from_data, float(np.max(mids))))
    x_max = max(x_max, float(center) + 30.0)
    ax.set_xlim(0, x_max)
    return 0.0, x_max


def plot_spectrum_fit(
    ax: plt.Axes,
    *,
    mids: np.ndarray,
    raw: np.ndarray,
    cut_x: Optional[np.ndarray] = None,
    cut_y: Optional[np.ndarray] = None,
    bkg_spec: Optional[np.ndarray] = None,
    net_spec: Optional[np.ndarray] = None,
    best_fit: Optional[np.ndarray] = None,
    gauss_fit: Optional[np.ndarray] = None,
    background_fit: Optional[np.ndarray] = None,
    fit_lo: Optional[float] = None,
    fit_hi: Optional[float] = None,
    center: Optional[float] = None,
    fit_x: Optional[np.ndarray] = None,
    raw_label: str = "all spectrum",
    bkg_label: str = "bkg",
    net_label: str = "net",
) -> None:
    """统一光谱+拟合的画法。所有 series 都是可选的。

    fit_x：best_fit / gauss_fit / background_fit 的 x 轴；缺省时退回 cut_x 或 mids。
    """
    ax.step(mids, raw, where="mid", lw=LW_RAW, color=COLOR_RAW, label=raw_label)
    if bkg_spec is not None:
        ax.step(mids, bkg_spec, where="mid", lw=LW_RAW, color=COLOR_BKG, alpha=0.85, label=bkg_label)
    if net_spec is not None:
        ax.step(mids, net_spec, where="mid", lw=LW_NET, color=COLOR_NET, label=net_label)
    if cut_x is not None and cut_y is not None and net_spec is None:
        ax.plot(cut_x, cut_y, color=COLOR_CUT, lw=LW_NET, label="cut_data")

    plot_x = fit_x if fit_x is not None else cut_x
    if best_fit is not None and plot_x is not None:
        ax.plot(plot_x, best_fit, ls=LINESTYLE_FIT, color=COLOR_FIT, lw=LW_FIT, label="Fit")
    if gauss_fit is not None and plot_x is not None:
        ax.plot(plot_x, gauss_fit, ls=LINESTYLE_FIT, color=COLOR_GAUSS, lw=1.3, label="gaussian")
    if background_fit is not None and plot_x is not None:
        ax.plot(plot_x, background_fit, ls=LINESTYLE_FIT, color=COLOR_BKG_FIT, lw=1.3, label="background")

    if center is not None:
        ax.axvline(center, color=COLOR_CENTER, ls=LINESTYLE_FIT, lw=1.0)
    if fit_lo is not None and fit_hi is not None:
        y_max = float(np.max(raw)) if len(raw) else 1.0
        ax.vlines([fit_lo, fit_hi], 0, y_max, colors=COLOR_FIT_RANGE, linestyles=LINESTYLE_RANGE, label="Fit Range")


def style_title(ax: plt.Axes, title: str) -> None:
    ax.set_title(title, fontsize=TITLE_FS)


def style_legend(ax: plt.Axes, *, loc: str = "upper right") -> None:
    ax.legend(fontsize=LEGEND_FS, loc=loc)
