"""EC xray 自动选窗工具（背景扣除版）。

物理：
  - 暗噪声 pedestal 固定在低 ADC（~70-80）
  - 电子学 artifact 固定在 ADC ~13500 和 ~31500
  - 信号峰 ADC 随能量线性增加
  - 必须做背景扣除才能分离信号与噪声

算法：
  1. 读取 EC L1 parquet，对每能量每通道做背景扣除（signal_rate - bkg_rate）
  2. 在净谱上寻峰，排除噪声 pedestal 区（ADC < 100）和 artifact 区（ADC > 10000）
  3. 选净谱中 prominence 最高的峰作为信号峰
  4. 四通道共识：median center，异常通道用共识窗口替代
  5. 窗口 = center ± max(30, 5σ)

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

NOISE_ADC_MAX = 150.0
ARTIFACT_ADC_MIN = 10000.0


def find_signal_peak_net(
    sig_amp: np.ndarray, bkg_amp: np.ndarray,
) -> Optional[dict]:
    sig = sig_amp[np.isfinite(sig_amp) & (sig_amp > 0)]
    bkg = bkg_amp[np.isfinite(bkg_amp) & (bkg_amp > 0)]
    if len(sig) < 30 or len(bkg) < 30:
        return None

    hi = min(np.percentile(sig, 99.9), np.percentile(bkg, 99.9)) + 50
    bins = np.arange(0, hi + BIN_WIDTH, BIN_WIDTH)
    s_hist, edges = np.histogram(sig, bins=bins)
    b_hist, _ = np.histogram(bkg, bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2.0

    s_rate = s_hist / max(len(sig), 1)
    b_rate = b_hist / max(len(bkg), 1)
    net = s_rate - b_rate

    w = min(15, len(net) // 2 * 2 + 1) if len(net) >= 15 else max(5, len(net) // 2 * 2 + 1)
    smooth = savgol_filter(np.clip(net, 0, None), w, 2)

    max_val = float(np.max(smooth))
    if max_val <= 0:
        return None

    peaks, props = find_peaks(
        smooth,
        prominence=max(1e-7, max_val * 0.02),
        distance=max(5, int(20 / max(BIN_WIDTH, 1))),
    )
    if len(peaks) == 0:
        return None

    best_score, best_idx = -1.0, -1
    for i, p in enumerate(peaks):
        c = mids[p]
        if c < NOISE_ADC_MAX or c > ARTIFACT_ADC_MIN:
            continue
        score = float(props["prominences"][i])
        if score > best_score:
            best_score, best_idx = score, i

    if best_idx < 0:
        return None

    p = peaks[best_idx]
    center = float(mids[p])
    half_max = smooth[p] / 2.0
    left = p
    while left > 0 and smooth[left] > half_max:
        left -= 1
    right = p
    while right < len(smooth) - 1 and smooth[right] > half_max:
        right += 1
    fwhm = (right - left) * BIN_WIDTH
    sigma_est = max(6.0, fwhm / 2.355) if fwhm > BIN_WIDTH else 15.0

    half_win = max(30.0, sigma_est * 5.0)
    return {"center": center, "sigma": sigma_est, "lo": max(0, center - half_win), "hi": center + half_win}


def consensus_windows(ch_peaks: Dict[int, dict]) -> Dict[str, dict]:
    if not ch_peaks:
        return {}

    centers = {c: p["center"] for c, p in ch_peaks.items()}
    median_c = float(np.median(list(centers.values())))
    good = {c: p for c, p in ch_peaks.items() if abs(p["center"] - median_c) / max(median_c, 1.0) <= 0.25}

    if not good:
        good = ch_peaks

    ref_sigma = float(np.median([p["sigma"] for p in good.values()]))
    half_win = max(30.0, ref_sigma * 5.0)

    result = {}
    for ch in range(4):
        if ch in good:
            p = good[ch]
            result[str(ch)] = {"lo": round(p["lo"], 1), "hi": round(p["hi"], 1)}
        elif ch in ch_peaks:
            result[str(ch)] = {"lo": round(median_c - half_win, 1), "hi": round(median_c + half_win, 1)}
    return result


def build_windows(payload: str) -> dict:
    l1_dir = Path(PRODUCTS) / payload / "ec" / "L1" / "parquet"
    if not l1_dir.exists():
        raise FileNotFoundError(f"Missing L1 data: {l1_dir}")

    bkg_by_ch: Dict[int, np.ndarray] = {}
    for fp in sorted(l1_dir.glob("src_bkg_*_corr.l1.parquet")):
        df = pd.read_parquet(fp)
        for ch in range(4):
            ch_df = df[df["channel"] == ch]
            if len(ch_df) > 0:
                bkg_by_ch[ch] = ch_df["amp_corr"].to_numpy(dtype=float)

    if not bkg_by_ch:
        raise FileNotFoundError("No background file found (src_bkg_*_corr.l1.parquet)")

    by_energy: Dict[int, Dict[int, np.ndarray]] = defaultdict(dict)
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
        ch_peaks: Dict[int, dict] = {}
        for ch in range(4):
            if ch not in ch_map or ch not in bkg_by_ch:
                continue
            p = find_signal_peak_net(ch_map[ch], bkg_by_ch[ch])
            if p:
                ch_peaks[ch] = p

        windows = consensus_windows(ch_peaks)
        if windows:
            output[f"{energy_keV}keV"] = windows

    return dict(sorted(output.items()))


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m calibration_lib.tools.auto_xray_windows <14B|15B>")
        sys.exit(1)

    payload = sys.argv[1]
    windows = build_windows(payload)
    out_path = Path(__file__).resolve().parents[1] / "resources" / f"ec_xray_windows_{payload}.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(windows, f, ensure_ascii=False, indent=2)

    spread_list = []
    for ekey in sorted(windows):
        chs = windows[ekey]
        centers = [(v["lo"] + v["hi"]) / 2 for v in chs.values()]
        if len(centers) >= 2:
            s = (max(centers) - min(centers)) / max(np.median(centers), 1.0) * 100
            spread_list.append(s)
            flag = "✓" if s < 20 else "⚠"
        else:
            s = 0
            flag = "?"
            spread_list.append(s)
        lo = min(v["lo"] for v in chs.values())
        hi = max(v["hi"] for v in chs.values())
        print(f"  {flag} {ekey}: lo={lo:.0f} hi={hi:.0f}  spread={s:.0f}%  ({len(chs)} ch)")

    print(f"\n{payload}: {len(windows)} energies → {out_path}")
    if spread_list:
        print(f"  mean spread={np.mean(spread_list):.0f}%  max spread={np.max(spread_list):.0f}%")
    print(f"\nRerun:")
    print(f"  .venv/Scripts/python.exe -m calibration_lib.run_layer {payload} EC_L2 --products {PRODUCTS}")
    print(f"  .venv/Scripts/python.exe -m calibration_lib.run_layer {payload} EC_L3 --products {PRODUCTS}")


if __name__ == "__main__":
    main()
