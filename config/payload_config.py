"""
载荷配置文件 - 定义14B和15B两个载荷的基本信息
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict


@dataclass
class ChannelInfo:
    """通道信息"""
    ch_id: int
    name: str = ""


@dataclass
class SourceSpec:
    """标定放射源规格。

    每个源一份 event/hk 文件，含所有能峰（``peak_energies_keV``）。
    ``exposure_minutes`` 来自原始文件名，EC L2 扣本底时按计数率归一化。
    """
    name: str
    event_file: str
    hk_file: str
    exposure_minutes: int
    peak_energies_keV: List[float] = field(default_factory=list)


@dataclass
class PayloadConfig:
    """载荷配置"""
    payload_id: str  # "02" or "03"
    payload_name: str  # "14B" or "15B"
    raw_root: Path
    tb_raw_dir: Path
    ec_xray_dir: Path
    ec_source_dir: Path
    n_channels: int = 4
    channels: List[ChannelInfo] = field(default_factory=lambda: [ChannelInfo(i) for i in range(4)])
    source_specs: List[SourceSpec] = field(default_factory=list)
    bkg_event_file: str = ""
    bkg_hk_file: str = ""
    bkg_exposure_minutes: int = 0
    

# 定义项目根目录
ROOT_DIR = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT_DIR / "14、15B标定数据"
PRODUCT_ROOT = ROOT_DIR / "products"

# 14B载荷配置
GRID_14B = PayloadConfig(
    payload_id="02",
    payload_name="14B",
    raw_root=RAW_ROOT / "02",
    tb_raw_dir=RAW_ROOT / "02" / "Temp_Vbias",
    ec_xray_dir=RAW_ROOT / "02" / "X_new",
    ec_source_dir=RAW_ROOT / "02" / "source",
    bkg_event_file="Bkg_30m_084.event.dat",
    bkg_hk_file="Bkg_30m_ecu_127.hk",
    bkg_exposure_minutes=30,
    source_specs=[
        SourceSpec("Na22", "Na22_5m_083.event.dat", "Na22_5m_ecu_126.hk",
                  exposure_minutes=5, peak_energies_keV=[511.0, 1274.5]),
        SourceSpec("Cs137", "CS137_25m_082.event.dat", "CS137_25m_ecu_125.hk",
                  exposure_minutes=25, peak_energies_keV=[661.7]),
        SourceSpec("Co60", "Co60_40m_081.event.dat", "Co60_ 40m_ecu_124.hk",
                  exposure_minutes=40, peak_energies_keV=[1173.2, 1332.5]),
    ]
)

# 15B载荷配置
GRID_15B = PayloadConfig(
    payload_id="03",
    payload_name="15B",
    raw_root=RAW_ROOT / "03",
    tb_raw_dir=RAW_ROOT / "03" / "Temp_Vbias",
    ec_xray_dir=RAW_ROOT / "03" / "X",
    ec_source_dir=RAW_ROOT / "03" / "source",
    bkg_event_file="bkg_30m_180.event.dat",
    bkg_hk_file="bkg_30m_ecu_022.hk",
    bkg_exposure_minutes=30,
    source_specs=[
        SourceSpec("Na22", "na22_3m_179.event.dat", "na22_3m_ecu_021.hk",
                  exposure_minutes=3, peak_energies_keV=[511.0, 1274.5]),
        SourceSpec("Cs137", "cs137_20m_c8_178.event.dat", "cs137_20m_c8_ecu_020.hk",
                  exposure_minutes=20, peak_energies_keV=[661.7]),
        SourceSpec("Co60", "co60_1h_177.event.dat", "co60_1h_ecu_019.hk",
                  exposure_minutes=60, peak_energies_keV=[1173.2, 1332.5]),
    ]
)

PAYLOADS: Dict[str, PayloadConfig] = {
    "14B": GRID_14B,
    "15B": GRID_15B,
}


def get_payload_config(payload_name: str) -> PayloadConfig:
    """根据载荷名称获取配置"""
    if payload_name not in PAYLOADS:
        raise ValueError(f"Unknown payload: {payload_name}. Available: {list(PAYLOADS.keys())}")
    return PAYLOADS[payload_name]


# TB标定处理参数
TB_PROCESS_PARAMS = {
    "fit_mode": "gaussian",  # gaussian, linear+gaussian, exp+gaussian
    "auto_window": True,
    "fit_windows_config": None,
    "min_counts": 10,  # 最小事件数
}

# EC标定处理参数
EC_PROCESS_PARAMS = {
    "bin_width": 4.0,  # keV
    "energy_split_low": 49.0,  # keV
    "energy_split_high": 51.0,  # keV
    "ref_temp": 25.0,  # 参考温度 °C
    "ref_bias": 28.5,  # 参考偏压 V
    "k_fixed": 0.0215,  # V/°C (从论文 Eq. 8)
}
