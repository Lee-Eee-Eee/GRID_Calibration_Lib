"""EC xray 自动选窗工具。

物理事实：
- SiPM 噪声 pedestal 固定在 ADC ~70-80
- 宇宙线/高能背景在 ADC ~900+ 和 ~32000
- 信号峰 ADC 与能量成正比（TB 矫正后）
- 四通道测同个物理信号 → center 应 ≤15% 离散

算法：
1. 按能量段预设信号峰 ADC 搜索区间（排除噪声和宇宙线）
2. area_score = counts × prominence：选计数多且突出的峰
3. position_bonus = 1 - |center-expected|/expected×2：就近原则
4. 四通道 median center 共识，异常通道用共识窗口强制约束
5. 窗口宽度 = center ± max(60, 5σ)，σ 由半高宽估算

用法：
    .venv/Scripts/python.exe -m calibration_lib.tools.auto_xray_windows <14B|15B> [--products products]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from scipy.signal import find_peaks, savgol_filter

BIN_WIDTH = 4.0
PRODUCTS = "products"

# 各载荷每能量段的信号峰 ADC 预期区间
# (energy_low, energy_high, adc_lo, adc_hi, expected_center)
ADC_ZONES: Dict[str, list] = {
    "14B": [
        (40, 60, 250, 550, 415),
        (60, 100, 250, 550, 430),
    ],
    "15B": [
        (12, 25, 50, 250, 140),
        (25, 40, 80, 300, 180),
        (40, 55, 150, 450, 320),
        (55, 95, 200, 600, 400),
    ],
}


def find_signal_peak(amp_corr: np.ndarray, adc_lo: float, adc_hi: float,
                     expected_center: float) -> Optional[dict]:
    amp = amp_corr[np.isfinite(amp_corr) & (amp_corr > 0)]
    if len(amp) < 20:
        return None

    hi_edge = int(max(adc_hi + 50, np.percentile(amp, 95) + 50))
    bins = np.arange(0, hi_edge + BIN_WIDTH, BIN_WIDTH)
    hist, edges = np.histogram(amp, bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2.0

    w = min(11, len(hist) // 2 * 2 + 1) if len(hist) >= 11 else max(3, len(hist) // 2 * 2 + 1)
    smooth = savgol_filter(hist.astype(float), window_length=w, polyorder=2)

    peaks, props = find_peaks(smooth, prominence=max(5, float(np.max(smooth)) * 0.015), distance=3)

    best_score, best_p = -1.0, None
    for i, p in enumerate(peaks):
        center = mids[p]
        if center < adc_lo or center > adc_hi:
            continue
        score = float(hist[p]) * float(props["prominences"][i])
        if expected_center > 0:
            score *= 1.0 - min(1.0, abs(center - expected_center) / expected_center * 2.0) * 0.4
        if score > best_score:
            best_score, best_p = score, p

    if best_p is None:
        return None

    center = float(mids[best_p])
    half_max = smooth[best_p] / 2.0
    left = best_p
    while left > 0 and smooth[left] > half_max:
        left -= 1
    right = best_p
    while right < len(smooth) - 1 and smooth[right] > half_max:
        right += 1
    sigma_est = max(8.0, (right - left) * BIN_WIDTH / 2.355) if right - left > 1 else 20.0

    half_win = max(60.0, sigma_est * 5.0)
    lo = max(adc_lo, center - half_win)
    hi = min(adc_hi, center + half_win)
    return {"center": center, "sigma": sigma_est, "lo": lo, "hi": hi}


def get_zone(payload: str, energy_keV: int) -> tuple:
    for z in ADC_ZONES[payload]:
        if z[0] <= energy_keV <= z[1]:
            return z[2], z[3], z[4]
    return 150, 600, 350


def build_windows(payload: str) -> dict:
    l1_dir = Path(PRODUCTS) / payload / "ec" / "L1" / "parquet"
    if not l1_dir.exists():
        raise FileNotFoundError(f"Missing L1 data: {l1_dir}")

    by_energy = defaultdict(dict)
    for fp in sorted(l1_dir.glob("*keV_ch*_corr.l1.parquet")):
        if fp.name.startswith("src_"):
            continue
        m = re.match(r"(\d+)keV_ch(\d+)$", fp.stem.replace("_corr.l1", ""))
        if not m:
            continue
        energy_keV, ch = int(m.group(1)), int(m.group(2))

        df = pd.read_parquet(fp)
        ch_df = df[df["channel"] == ch]
        by_energy[energy_keV][ch] = ch_df["amp_corr"].to_numpy(dtype=float)

    output = {}
    for energy_keV in sorted(by_energy):
        ch_map = by_energy[energy_keV]
        adc_lo, adc_hi, exp_center = get_zone(payload, energy_keV)

        ch_peaks = {}
        for ch in range(4):
            if ch not in ch_map:
                continue
            p = find_signal_peak(ch_map[ch], adc_lo, adc_hi, exp_center)
            if p:
                ch_peaks[ch] = p

        if len(ch_peaks) < 2:
            half = 80.0
            centers = [ch_peaks[c]["center"] for c in ch_peaks]
            consensus = np.median(centers) if centers else exp_center
        else:
            centers = [ch_peaks[c]["center"] for c in ch_peaks]
            consensus = float(np.median(centers))
            half = max(60.0, np.std(centers) * 5.0) if len(centers) >= 3 else 80.0

        ekey = f"{energy_keV}keV"
        for ch in range(4):
            if ch not in ch_map:
                continue
            if ch in ch_peaks and abs(ch_peaks[ch]["center"] - consensus) / max(consensus, 1.0) <= 0.20:
                w = ch_peaks[ch]
                lo, hi = w["lo"], w["hi"]
            else:
                lo = max(adc_lo, consensus - half)
                hi = min(adc_hi, consensus + half)
            output.setdefault(ekey, {})[str(ch)] = {"lo": round(lo, 1), "hi": round(hi, 1)}

    return dict(sorted(output.items()))


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m calibration_lib.tools.auto_xray_windows <14B|15B>")
        sys.exit(1)

    payload = sys.argv[1]
    if payload not in ADC_ZONES:
        print(f"Unknown payload: {payload}, expected 14B or 15B")
        sys.exit(1)

    windows = build_windows(payload)
    out_path = Path(__file__).resolve().parents[1] / "resources" / f"ec_xray_windows_{payload}.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(windows, f, ensure_ascii=False, indent=2)

    spread_list = []
    for ekey in sorted(windows):
        any_ch = next(iter(windows[ekey]))
        w0 = windows[ekey][any_ch]
        centers = [w["lo"] + (w["hi"]-w["lo"]) / 2 for w in windows[ekey].values()]
        if len(centers) >= 2:
            s = (max(centers) - min(centers)) / np.median(centers) * 100
            spread_list.append(s)
            flag = "⚠" if s > 20 else "✓"
        else:
            s = 0
            flag = "?"
            spread_list.append(s)
        print(f"  {flag} {ekey}: lo={w0['lo']:.0f} hi={w0['hi']:.0f}  spread={s:.0f}%")

    print(f"\n{payload}: {len(windows)} energies → {out_path}")
    if spread_list:
        print(f"  mean spread={np.mean(spread_list):.0f}%  max spread={np.max(spread_list):.0f}%")
    print(f"\nRerun:")
    print(f"  .venv/Scripts/python.exe -m calibration_lib.run_layer {payload} EC_L2 --products {PRODUCTS}")
    print(f"  .venv/Scripts/python.exe -m calibration_lib.run_layer {payload} EC_L3 --products {PRODUCTS}")


if __name__ == "__main__":
    main()