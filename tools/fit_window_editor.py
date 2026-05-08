"""
交互式拟合窗口编辑器
用于手动调整每个数据文件的拟合范围
支持从 L0 Parquet 直接读取
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('TkAgg')

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyBboxPatch
from matplotlib.widgets import Button, RangeSlider
from scipy.signal import find_peaks, savgol_filter

# Find root automatically
ROOT_DIR = Path(__file__).resolve().parents[2]
PRODUCTS_DIR = ROOT_DIR / "products"
RESOURCES_DIR = ROOT_DIR / "calibration_lib" / "resources"

# Store the global fits list
def get_config_path(payload: str) -> Path:
    return RESOURCES_DIR / f"legacy_fit_windows_config_{payload}.json"

def get_parquet_files(payload: str = "14B") -> List[Path]:
    pq_dir = PRODUCTS_DIR / payload / "tb" / "L0" / "parquet"
    if not pq_dir.exists():
        return []
    return sorted(pq_dir.glob("*.parquet"))

def load_fit_windows_config(payload: str) -> Dict:
    """加载拟合窗口配置文件"""
    config_path = get_config_path(payload)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_fit_windows_config(payload: str, config: Dict) -> None:
    """保存拟合窗口配置文件"""
    import shutil
    config_path = get_config_path(payload)
    if config_path.exists():
        shutil.copy(config_path, config_path.with_suffix('.bak')) 
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    print(f"[SAVED] Configuration saved to {config_path}")
def compute_spectrum(energy: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """计算能谱并自动检测峰位置"""
    bin_lo = 0
    bin_hi = int(max(420, np.percentile(energy, 99.8) + 40))
    bin_hi = min(1200, bin_hi)
    bins = np.arange(bin_lo, bin_hi + 1, 1)
    hist, edges = np.histogram(energy, bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2.0

    window = 11 if len(hist) >= 11 else max(5, len(hist) // 2 * 2 + 1)
    hist_smooth = savgol_filter(hist.astype(float), window_length=window, polyorder=2)
    peaks, props = find_peaks(
        hist_smooth,
        prominence=max(15, float(np.max(hist_smooth)) * 0.03),
        distance=18,
    )
    
    if len(peaks) == 0:
        return mids, hist, float(mids[len(mids)//2]), float(mids[len(mids)//2])

    peak_floor = 70
    valid = peaks[mids[peaks] >= peak_floor]
    if len(valid) > 0:
        valid_idx = [int(np.where(peaks == p)[0][0]) for p in valid]
        pick = int(valid[np.argmax(props["prominences"][valid_idx])])
    else:
        pick = int(peaks[np.argmax(props["prominences"])])

    peak_x = float(mids[pick])
    center_idx = int(np.argmin(np.abs(mids - peak_x)))
    
    span_left = 85
    span_right = 110
    
    lo_idx = max(0, center_idx - span_left)
    segment_left = hist_smooth[lo_idx : center_idx + 1]
    if segment_left.size >= 5:
        lo_idx = lo_idx + int(np.argmin(segment_left))
    
    hi_idx = min(len(mids) - 1, center_idx + span_right)
    segment_right = hist_smooth[center_idx : hi_idx + 1]
    if segment_right.size >= 5:
        hi_idx = center_idx + int(np.argmin(segment_right))
    
    auto_lo = float(mids[lo_idx])
    auto_hi = float(mids[hi_idx])
    
    return mids, hist, auto_lo, auto_hi


class FitWindowEditor:
    def __init__(self, payload="14B"):
        self.payload = payload
        self.config = load_fit_windows_config(self.payload)
        self.pq_files = get_parquet_files(self.payload)

        if not self.pq_files:
            alternative = "15B" if payload == "14B" else "14B"
            print(f"[Warn] No Parquet files found for {self.payload}, falling back to {alternative}")
            self.payload = alternative
            self.config = load_fit_windows_config(self.payload)
            self.pq_files = get_parquet_files(self.payload)
        self.current_channel = 0
        
        self.current_file_key = ""
        
        self.mids: Optional[np.ndarray] = None
        self.hist: Optional[np.ndarray] = None
        self.auto_lo = 0.0
        self.auto_hi = 100.0
        self.current_lo = 0.0
        self.current_hi = 100.0
        
        self.channel_data: List[np.ndarray] = []
        
        self.fig = None
        self.ax = None
        self.ax_filelist = None
        self.slider = None
        self.file_list_visible = False
        
        self._file_click_cid = None
        self._plot_press_cid = None
        self._plot_motion_cid = None
        self._plot_release_cid = None
        
        self.ch_buttons: List[Button] = []
        self.btn_toggle_list = None
        
        self.dragging_edge: Optional[str] = None
        
        self.file_cache: Dict[int, List[np.ndarray]] = {}
        self.spectrum_cache: Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray, float, float]] = {}
        self._updating = False
        
    def get_file_key(self, path: Path) -> str:
        return path.stem.replace("-", "_")
    
    def get_display_name(self, path: Path) -> str:
        return path.stem
    
    def load_current_file(self) -> None:
        if self.current_idx >= len(self.pq_files):
            return
            
        pq_file = self.pq_files[self.current_idx]
        
        if self.current_idx in self.file_cache:
            self.channel_data = self.file_cache[self.current_idx]
            print(f"[INFO] Loading {pq_file.name} (cached)")
        else:
            print(f"[INFO] Loading {pq_file.name}")
            df = pd.read_parquet(pq_file)
            channel_data: List[np.ndarray] = []
            for ch in range(4):
                energy = df[df["channel"] == ch]["amp"].to_numpy(dtype=float)
                channel_data.append(energy)
            
            self.file_cache[self.current_idx] = channel_data
            self.channel_data = channel_data
        
        self.current_file_key = self.get_file_key(pq_file)
        self.backfill_manual_flags()

    def get_channel_config(self, idx: int, ch: int) -> Optional[Dict]:
        file_key = self.get_file_key(self.pq_files[idx])
        ch_map = self.config.get(file_key, {})
        val = ch_map.get(str(ch))
        if isinstance(val, dict):
            return val
        return None

    def backfill_manual_flags(self) -> None:
        file_cfg = self.config.get(self.current_file_key, {})
        if not isinstance(file_cfg, dict):
            return
        for ch in range(4):
            ch_cfg = file_cfg.get(str(ch))
            if not isinstance(ch_cfg, dict):
                continue
            if "manual" in ch_cfg:
                continue
            _, _, auto_lo, auto_hi = self.get_spectrum_cached(self.current_idx, ch)
            lo = float(ch_cfg.get("lo", auto_lo))
            hi = float(ch_cfg.get("hi", auto_hi))
            ch_cfg["manual"] = abs(lo - auto_lo) > 0.1 or abs(hi - auto_hi) > 0.1

    def get_spectrum_cached(self, idx: int, ch: int) -> Tuple[np.ndarray, np.ndarray, float, float]:
        key = (idx, ch)
        if key in self.spectrum_cache:
            return self.spectrum_cache[key]
        if idx not in self.file_cache:
            df = pd.read_parquet(self.pq_files[idx])
            channel_data = []
            for idx_ch in range(4):
                channel_data.append(df[df["channel"] == idx_ch]["amp"].to_numpy(dtype=float))
            self.file_cache[idx] = channel_data
        
        channel_data = self.file_cache[idx]
        if len(channel_data[ch]) < 10:
            spectrum = (np.array([1, 2, 3]), np.array([1, 2, 3]), 0.0, 100.0)
        else:
            spectrum = compute_spectrum(channel_data[ch])
        self.spectrum_cache[key] = spectrum
        return spectrum
    
    def get_current_window(self) -> Tuple[float, float]:
        if self.current_file_key in self.config and str(self.current_channel) in self.config[self.current_file_key]:
            ch_config = self.config[self.current_file_key][str(self.current_channel)]
            return ch_config.get("lo", self.auto_lo), ch_config.get("hi", self.auto_hi)
        return self.auto_lo, self.auto_hi
    
    def update_config(self) -> None:
        if self.current_file_key not in self.config:
            self.config[self.current_file_key] = {}
        is_manual = abs(self.current_lo - self.auto_lo) > 0.1 or abs(self.current_hi - self.auto_hi) > 0.1
        self.config[self.current_file_key][str(self.current_channel)] = {
            "lo": self.current_lo,
            "hi": self.current_hi,
            "manual": is_manual,
        }
    
    def setup_figure(self) -> None:
        self.fig = plt.figure(figsize=(16, 10))
        self.fig.patch.set_facecolor("#e9edf2")
        
        self.ax = self.fig.add_axes([0.28, 0.38, 0.68, 0.52])
        self.ax.set_facecolor("#f8fafc")
        
        self.ax_filelist = self.fig.add_axes([0.02, 0.25, 0.20, 0.65])
        self.ax_filelist.set_facecolor("#eef2f7")
        
        ax_toggle = self.fig.add_axes([0.02, 0.92, 0.20, 0.04])
        self.btn_toggle_list = Button(ax_toggle, "Show File List", color="#d7dfe9", hovercolor="#c7d0dc")
        self.style_button(self.btn_toggle_list)
        self.btn_toggle_list.on_clicked(self.on_toggle_file_list)
        
        self.create_file_list()
        
        if self._file_click_cid is None:
            self._file_click_cid = self.fig.canvas.mpl_connect('button_press_event', self.on_filelist_click)
        if self._plot_press_cid is None:
            self._plot_press_cid = self.fig.canvas.mpl_connect('button_press_event', self.on_plot_press)
        if self._plot_motion_cid is None:
            self._plot_motion_cid = self.fig.canvas.mpl_connect('motion_notify_event', self.on_plot_motion)
        if self._plot_release_cid is None:
            self._plot_release_cid = self.fig.canvas.mpl_connect('button_release_event', self.on_plot_release)
        
        for ch in range(4):
            ax_ch = self.fig.add_axes([0.02 + ch * 0.05, 0.18, 0.045, 0.04])
            btn = Button(ax_ch, f"Ch{ch}", color="#d6dee8", hovercolor="#c4cfdb")
            self.style_button(btn, fontsize=8)
            btn.on_clicked(lambda event, c=ch: self.on_select_channel(c))
            self.ch_buttons.append(btn)
        
        self.update_channel_buttons()
        
        ax_slider = self.fig.add_axes([0.24, 0.25, 0.70, 0.04])
        self.slider = RangeSlider(
            ax_slider, 'Fit Window',
            valmin=0, valmax=500,
            valinit=(self.current_lo, self.current_hi),
            valstep=0.5
        )
        self.slider.on_changed(self.on_slider_changed)
        self.update_layout()
        
        ax_prev = self.fig.add_axes([0.24, 0.08, 0.08, 0.04])
        ax_next = self.fig.add_axes([0.33, 0.08, 0.08, 0.04])
        self.btn_prev = Button(ax_prev, "< Prev", color="#d7dfe9", hovercolor="#c7d0dc")
        self.btn_next = Button(ax_next, "Next >", color="#d7dfe9", hovercolor="#c7d0dc")
        self.style_button(self.btn_prev)
        self.style_button(self.btn_next)
        self.btn_prev.on_clicked(self.on_prev_file)
        self.btn_next.on_clicked(self.on_next_file)
        
        ax_auto = self.fig.add_axes([0.42, 0.08, 0.08, 0.04])
        self.btn_auto = Button(ax_auto, "Auto", color="#b9dfc7", hovercolor="#a5cfb4")
        self.style_button(self.btn_auto)
        self.btn_auto.on_clicked(self.on_use_auto)
        
        ax_apply_all = self.fig.add_axes([0.51, 0.08, 0.10, 0.04])
        self.btn_apply_all = Button(ax_apply_all, "All Channels", color="#f7e2bb", hovercolor="#ecd39f")
        self.style_button(self.btn_apply_all)
        self.btn_apply_all.on_clicked(self.on_apply_all_channels)
        
        ax_save = self.fig.add_axes([0.62, 0.08, 0.12, 0.04])
        self.btn_save = Button(ax_save, "Save Config", color="#98d1a4", hovercolor="#87c392")
        self.style_button(self.btn_save)
        self.btn_save.on_clicked(self.on_save)
        
        self.status_text = self.fig.text(0.24, 0.15, '', fontsize=9, va='top', family='monospace')
        self.info_text = self.fig.text(0.62, 0.18, '', fontsize=10, va='top', ha='center', weight='bold')

    def style_button(self, btn: Button, fontsize: int = 9) -> None:
        btn.label.set_color("#1f2d3a")
        btn.label.set_fontsize(fontsize)
        
    def update_layout(self) -> None:
        if self.file_list_visible:
            self.ax_filelist.set_visible(True)
            self.ax.set_position([0.28, 0.38, 0.68, 0.52])
            self.slider.ax.set_position([0.24, 0.25, 0.70, 0.04])
            self.btn_toggle_list.label.set_text("Hide File List")
        else:
            self.ax_filelist.set_visible(False)
            self.ax.set_position([0.12, 0.38, 0.84, 0.52])
            self.slider.ax.set_position([0.12, 0.25, 0.82, 0.04])
            self.btn_toggle_list.label.set_text("Show File List")

    def on_toggle_file_list(self, event) -> None:
        self.file_list_visible = not self.file_list_visible
        self.create_file_list()
        self.update_layout()
        self.fig.canvas.draw_idle()
        
    def create_file_list(self) -> None:
        self.ax_filelist.clear()
        self.ax_filelist.set_title('Files (L0 Parquets)', fontsize=9)
        self.ax_filelist.set_xlim(0, 1)
        self.ax_filelist.set_ylim(0, len(self.pq_files))
        self.ax_filelist.axis('off')
        if not self.file_list_visible:
            return
        
        for i, pq_file in enumerate(self.pq_files):
            y_pos = len(self.pq_files) - i - 1
            file_key = self.get_file_key(pq_file)
            cfg = self.config.get(file_key, {})
            has_cfg = isinstance(cfg, dict) and len(cfg) > 0
            
            row_color = "#d6e5ff" if i == self.current_idx else ("#dff4e6" if has_cfg else "#f7f9fc")

            self.ax_filelist.add_patch(
                FancyBboxPatch((0.01, y_pos + 0.04), 0.98, 0.92, facecolor=row_color, edgecolor="#d2dae5")
            )
            self.ax_filelist.text(0.02, y_pos + 0.5, self.get_display_name(pq_file)[:20], fontsize=7, va='center')

    def on_filelist_click(self, event) -> None:
        if event.inaxes != self.ax_filelist or not self.file_list_visible: return
        y = event.ydata
        if y is None: return
        idx = len(self.pq_files) - int(np.floor(y)) - 1
        if 0 <= idx < len(self.pq_files):
            x = event.xdata
            if x is not None and x >= 0.58:
                for ch in range(4):
                    if 0.58 + ch * 0.10 <= x <= 0.58 + ch * 0.10 + 0.085:
                        self.current_channel = ch
                        break
            if idx != self.current_idx:
                self.current_idx = idx
                self.load_current_file()
            self.update_channel_buttons()
            self.update_channel_data()
            self.draw_plot()
            self.update_slider_silent()
            self.create_file_list()
            self.fig.canvas.draw_idle()

    def update_channel_buttons(self) -> None:
        for i, btn in enumerate(self.ch_buttons):
            btn.ax.set_facecolor("#2f6fed" if i == self.current_channel else "#d6dee8")
            btn.label.set_color("white" if i == self.current_channel else "#1f2d3a")

    def on_select_channel(self, ch: int) -> None:
        if ch == self.current_channel: return
        self.current_channel = ch
        self.update_channel_buttons()
        self.update_channel_data()
        self.draw_plot()
        self.update_slider_silent()
        self.create_file_list()
        self.fig.canvas.draw_idle()

    def update_channel_data(self) -> None:
        if len(self.channel_data[self.current_channel]) < 10:
            self.mids, self.hist = np.array([1,2,3]), np.array([1,2,3])
            self.auto_lo, self.auto_hi = 0, 100
        else:
            self.mids, self.hist, self.auto_lo, self.auto_hi = compute_spectrum(self.channel_data[self.current_channel])
        self.current_lo, self.current_hi = self.get_current_window()
        
    def draw_plot(self) -> None:
        ax = self.ax
        ax.clear()
        
        ax.plot(self.mids, self.hist, color="#34495e", lw=1.2, alpha=0.3, ds='steps-mid')
        mask = (self.mids >= self.current_lo) & (self.mids <= self.current_hi)
        
        if np.any(mask):
            ax.plot(self.mids[mask], self.hist[mask], color="#e74c3c", lw=2.0)
            ax.fill_between(self.mids[mask], 0, self.hist[mask], color="#e74c3c", alpha=0.15)
            
        ax.axvline(self.current_lo, color="#2980b9", lw=2.0, ls="-")
        ax.axvline(self.current_hi, color="#2980b9", lw=2.0, ls="-")
        
        disp_max = int(min(np.max(self.mids), max(100, self.current_hi * 2.5)))
        ax.set_xlim(0, disp_max)
        
        y_max = np.max(self.hist[self.mids <= disp_max]) if len(self.hist) > 0 else 100
        ax.set_ylim(0, y_max * 1.1)
        
        ax.set_title(f"{self.current_file_key} - Ch{self.current_channel}", fontsize=12)
        ax.set_xlabel("ADC Unit")
        ax.set_ylabel("Counts")
        ax.grid(True, ls="--", alpha=0.3)
        
        self.update_status_text()

    def update_status_text(self) -> None:
        is_manual = abs(self.current_lo - self.auto_lo) > 0.1 or abs(self.current_hi - self.auto_hi) > 0.1
        status = "MANUAL" if is_manual else "AUTO"
        self.status_text.set_text(f"File: {self.current_file_key}\nChannel: {self.current_channel}\nWindow: [{self.current_lo:.1f}, {self.current_hi:.1f}] ({status})")

    def show_info(self, text: str, color: str = "#2ecc71") -> None:
        self.info_text.set_text(text)
        self.info_text.set_backgroundcolor(color)
        def clear_info():
            self.info_text.set_text("")
            self.fig.canvas.draw_idle()
        import threading
        threading.Timer(1.5, clear_info).start()

    def update_slider_silent(self) -> None:
        self._updating = True
        try:
            self.slider.set_val((self.current_lo, self.current_hi))
        finally:
            self._updating = False

    def on_slider_changed(self, val) -> None:
        if self._updating: return
        self.current_lo, self.current_hi = val
        self.update_config()
        self.draw_plot()
        self.create_file_list()
        self.fig.canvas.draw_idle()
        
    def on_plot_press(self, event) -> None:
        if event.inaxes != self.ax: return
        x = event.xdata
        dist_lo = abs(x - self.current_lo)
        dist_hi = abs(x - self.current_hi)
        tol = (self.ax.get_xlim()[1] - self.ax.get_xlim()[0]) * 0.05
        
        if dist_lo < tol and dist_lo <= dist_hi:
            self.dragging_edge = 'lo'
        elif dist_hi < tol:
            self.dragging_edge = 'hi'

    def on_plot_motion(self, event) -> None:
        if not self.dragging_edge or event.inaxes != self.ax: return
        x = max(0, event.xdata)
        if self.dragging_edge == 'lo':
            self.current_lo = min(x, self.current_hi - 1)
        else:
            self.current_hi = max(x, self.current_lo + 1)
            
        self.update_config()
        self.draw_plot()
        self.update_slider_silent()
        self.create_file_list()
        self.fig.canvas.draw_idle()

    def on_plot_release(self, event) -> None:
        self.dragging_edge = None

    def on_use_auto(self, event) -> None:
        self.current_lo = self.auto_lo
        self.current_hi = self.auto_hi
        self.update_config()
        self.draw_plot()
        self.update_slider_silent()
        self.create_file_list()
        self.fig.canvas.draw_idle()
        self.show_info("Applied Auto", "#e8f8f5")

    def on_apply_all_channels(self, event) -> None:
        lo, hi = self.current_lo, self.current_hi
        for ch in range(4):
            if str(ch) not in self.config[self.current_file_key]:
                self.config[self.current_file_key][str(ch)] = {}
            self.config[self.current_file_key][str(ch)]["lo"] = lo
            self.config[self.current_file_key][str(ch)]["hi"] = hi
            self.config[self.current_file_key][str(ch)]["manual"] = True
        self.create_file_list()
        self.fig.canvas.draw_idle()
        self.show_info("Applied to All Channels", "#fcf3cf")

    def on_save(self, event) -> None:
        save_fit_windows_config(self.payload, self.config)
        self.show_info("Config Saved!", "#d4efdf")

    def on_prev_file(self, event) -> None:
        if self.current_idx > 0:
            self.current_idx -= 1
            self.load_current_file()
            self.update_channel_data()
            self.draw_plot()
            self.update_slider_silent()
            self.create_file_list()
            self.fig.canvas.draw_idle()

    def on_next_file(self, event) -> None:
        if self.current_idx < len(self.pq_files) - 1:
            self.current_idx += 1
            self.load_current_file()
            self.update_channel_data()
            self.draw_plot()
            self.update_slider_silent()
            self.create_file_list()
            self.fig.canvas.draw_idle()

    def run(self) -> None:
        if not self.pq_files:
            print("[ERROR] No L0 Parquet files found in products/14B or 15B!")
            return
        self.load_current_file()
        self.setup_figure()
        self.update_channel_data()
        self.draw_plot()
        self.update_slider_silent()
        plt.show()

def main() -> None:
    print("=" * 60)
    print("Interactive Fit Window Editor (Parquet-backed)")
    print("=" * 60)
    print("Reading data directly from L0 Parquet...")
    payload = sys.argv[1] if len(sys.argv) > 1 else '14B'
    editor = FitWindowEditor(payload=payload)
    editor.run()

if __name__ == "__main__":
    main()
