# GRID 数据处理流程规范文档

## 概述

本文档规范了GRID项目中14B和15B两个载荷的温度偏压(TB)标定和能量(EC)标定数据的处理流程。整个处理流程按照数据分层的思想设计，分为多个处理阶段（L0、L1、L2、L3），每层承担不同的职责，形成清晰的模块化结构。

---

## 目录结构

```
products/
├── 14B/
│   ├── tb/
│   │   ├── L0/      # 原始事件到标准化格式
│   │   │   ├── parquet/
│   │   │   └── json/
│   │   ├── L1/      # 脉冲幅度直方图拟合
│   │   │   ├── json/
│   │   │   └── figures/
│   │   └── L2/      # 温偏曲面拟合
│   │       ├── json/
│   │       └── figures/
│   └── ec/
│       ├── L0/      # X射线事件到标准化格式
│       │   ├── parquet/
│       │   └── json/
│       ├── L1/      # 温偏矫正
│       │   ├── parquet/
│       │   └── json/
│       ├── L2/      # 能量直方图拟合
│       │   ├── json/
│       │   └── figures/
│       └── L3/      # 能量标定
│           ├── json/
│           └── figures/
├── 15B/
│   ├── tb/
│   └── ec/
│
calibration_lib/
├── config/
│   ├── __init__.py
│   └── payload_config.py      # 载荷配置(路径、源信息等)
├── common/
│   ├── __init__.py
│   └── utils.py               # 通用工具函数
├── tb/
│   ├── __init__.py
│   └── processors/
│       ├── __init__.py
│       ├── L0_processor.py     # TB L0处理
│       ├── L1_processor.py     # TB L1处理
│       └── L2_processor.py     # TB L2处理
├── ec/
│   ├── __init__.py
│   └── processors/
│       ├── __init__.py
│       ├── L0_processor.py     # EC L0处理
│       ├── L1_processor.py     # EC L1处理
│       ├── L2_processor.py     # EC L2处理
│       └── L3_processor.py     # EC L3处理
└── __init__.py
```

---

## 温度偏压(TB)标定数据处理流程

### 概述

TB标定数据处理分为3个步骤，涉及4级数据：Raw -> L0 -> L1 -> L2

```
┌─────────────────────┐
│  Raw Data           │
│ (.dat, .hk)         │
└──────────┬──────────┘
           │ parse + standardization
           ▼
┌─────────────────────┐
│  L0 - Data staging  │
│ (.parquet + .json)  │
│ amp,ch | T,V,σ      │
└──────────┬──────────┘
           │ histogram + gaussian fit
           ▼
┌─────────────────────┐
│  L1 - Fit results   │
│ (.json + .png)      │
│ μ, σ, FWHM, fit_mode
└──────────┬──────────┘
           │ 2D surface fit
           ▼
┌──────────────────────┐
│  L2 - Calibration    │
│ (.json + .png)       │
│ G0,k,V0,b,c params   │
│ + timestamps         │
└──────────────────────┘
```

### L0 层 - 原始数据标准化

**输入**：原始事件文件 (.event.dat) 和 HK文件 (.hk)

**处理**：
- 解析事件和house-keeping数据
- 提取每个脉冲的幅度(amp)和通道(ch)信息
- 从HK文件获取该测量的平均温度(T)、偏压(V)及其标准差(σ)
- 脉冲级数据写入 parquet，文件级元数据写入 parquet metadata

**输出示例**：
```
products/14B/tb/L0/
├── parquet/
│   ├── 20C_27.5V.parquet      # 其中: amp(pulse amplitude), ch(channel), utc
│   ├── 25C_28.0V.parquet
│   └── ...
└── json/
    └── L0_manifest.json
```

`L0_manifest.json` 只用于记录本次批处理是否完成、处理了哪些文件、哪些文件报错。
它不是后续科学处理使用的输入文件。

TB L0 的文件级元数据写在 parquet metadata 中，格式示意如下：
```json
{
  "event_file": "..../20C_27.5V-001.event.dat",
  "hk_file": "..../20C_27.5V-ecu_001.hk",
  "n_events": 50000,
  "channels": {
    "0": {
      "temp": 20.3,
      "temp_err": 0.2,
      "bias": 27.5,
      "bias_err": 0.1
    },
    ...
  }
}
```

### L1 层 - 脉冲幅度拟合

**输入**：L0层的parquet数据+元数据

**处理**：
- 对每个通道的脉冲幅度分布进行直方图化(可配置binning宽度)
- 进行高斯拟合：$P(x) = A \exp\left(-\frac{(x-\mu)^2}{2\sigma^2}\right)$ + 可选的背景模型
- 支持的拟合模式：
  - `"gaussian"`: 纯高斯
  - `"lin+gaus"`: 线性背景 + 高斯
  - `"exp+gaus"`: 指数背景 + 高斯
- 提取拟合参数：中心μ、标准差σ、FWHM、χ²等
- 生成高斯拟合曲线图

**输出示例**：
```
products/14B/tb/L1/
├── json/
│   ├── 20C_27.5V_fit_results.json
│   ├── 25C_28.0V_fit_results.json
│   └── L1_manifest.json
└── figures/
    ├── 20C_27.5V_ch0_fit.png
    ├── 20C_27.5V_ch1_fit.png
    └── ...
```

**L1_fit_results.json 格式**：
```json
{
  "stem": "20C_27.5V",
  "fit_mode": "lin+gaus",
  "channels": {
    "0": {
      "center": 123.45,
      "sigma": 2.34,
      "amplitude": 5000.0,
      "fwhm": 5.51,
      "rsquared": 0.998,
      "n_events": 15000,
      "bin_width": 1.0,
      "fit_mode": "lin+gaus"
    },
    ...
  }
}
```

### L2 层 - 温偏曲面拟合与最终标定参数

**输入**：L1层的拟合结果(json) + 原始TB数据(用于获取T,V值)

**处理**：
- 收集所有(T, V, G)数据点，其中G为增益(脉冲幅度直接代表)
- 进行2D曲面拟合，模型来自论文 Eq. (8)：

$$G(T, V_b) = G_0 \cdot (V_b - k \cdot T - V_0)^2 \cdot (-T^2 + b \cdot T + c)$$

其中：
- **G0**: 增益系数
- **V0**: 偏压偏移
- **k = 0.0215 V/°C** (固定，来自论文)
- **b, c**: 温度多项式参数

- 使用非线性拟合(scipy.optimize.curve_fit)
- 生成3D曲面拟合图

**输出示例**：
```
products/14B/tb/L2/
├── json/
│   ├── 2603281530_14B_TB.json    # 文件名包含时间戳
│   └── L2_manifest.json
└── figures/
    └── 2603281530_14B_TB_surface.png
```

**L2最终参数json格式**：
```json
{
  "timestamp": "2603281530",
  "payload": "14B",
  "layer": "TB_L2",
  "model": "G(T,V) = G0 * (V-k*T-V0)^2 * (-T^2+b*T+c)",
  "G0": 1.234e-6,
  "V0": 28.45,
  "k": 0.0215,
  "b": 1.234,
  "c": 123.45,
  "n_points": 250,
  "fit_rmse": 0.123
}
```

---

## 能量标定(EC)处理流程

### 概述

EC处理分为4个步骤，涉及5级数据：Raw -> L0 -> L1 -> L2 -> L3

```
┌────────────────────────┐
│  Raw XRay Data         │
│  (.dat, .hk)           │
└──────────┬─────────────┘
           │ parse
           ▼
┌────────────────────────┐
│  L0 - Data staging     │
│  (.parquet)            │
│  amp,ch,utc,T,V        │
└──────────┬─────────────┘
           │ TB correction
           ▼
┌──────────────────────────┐
│  L1 - Gain correction    │
│  (.parquet + .json)      │
│  amp_corr, bkg, corrfile │
└──────────┬───────────────┘
           │ spectrum fit
           ▼
┌────────────────────────┐
│  L2 - Spectrum fit     │
│  (.json + .png)        │
│  E, μ, σ, res, FWHM    │
└──────────┬─────────────┘
           │ energy calib
           ▼
┌──────────────────────────┐
│  L3 - EC parameters      │
│  (.json + .png)          │
│  low/high calib params   │
└──────────────────────────┘
```

### L0 层 - X射线数据标准化

**输入**：X射线事件文件(.event.dat)和HK文件(.hk)

**处理**：
- 解析X射线事件和HK数据
- 提取脉冲幅度(amp)、通道(ch)、UTC时间戳(utc)
- 从HK文件获取对应时刻的温度(temp)和偏压(bias)
- 保存为Parquet格式(高效二进制格式)

**输出**：
```
products/14B/ec/L0/
├── parquet/
│   ├── 511keV_ch0.parquet   # columns: [amp, ch, utc, temp, bias]
│   ├── 661keV_ch0.parquet
│   └── ...
└── json/
    ├── 511keV_ch0_metadata.json
    └── L0_manifest.json
```

### L1 层 - 温度偏压矫正

**输入**：L0层parquet数据 + TB L2的拟合参数json

**处理**：
- 使用TB L2得到的 G0, V0, k, b, c 计算增益函数
- 对每个脉冲进行温偏矫正：$\text{amp\_corr} = \text{amp} \cdot \frac{G(T_{ref}, V_{ref})}{G(T, V_b)}$
- 计算并保存背景信息(如果有背景测量)
- 在json中记录所用的TB参数文件时间戳(用于追溯)

**输出**：
```
products/14B/ec/L1/
├── parquet/
│   ├── 511keV_ch0_corrected.parquet    # 增加了 amp_corr 列
│   └── ...
└── json/
    ├── 511keV_ch0_l1_metadata.json
    └── L1_manifest.json
```

**L1_metadata.json格式**：
```json
{
  "stem": "511keV_ch0",
  "n_pulses": 100000,
  "tb_corrfile": "2603281530_14B_TB.json",
  "tb_params_used": {
    "G0": 1.234e-6,
    "V0": 28.45,
    "k": 0.0215
  },
  "background_info": {
    "bkg_mean": 5.23,
    "bkg_std": 1.45,
    "n_bkg_events": 50000
  }
}
```

### L2 层 - 能量谱拟合

**输入**：L1层的校正数据(parquet)

**处理**：
- 对矫正后的幅度(amp_corr)进行直方图化(可配置binning)
- 进行高斯拟合 + 线性背景
- 对每个通道提取：
  - **E**: 中心能量(keV)
  - **μ**: 谱峰中心
  - **σ**: 标准差
  - **res**: 能量分辨率(%) = FWHM/E × 100%
  - **FWHM**: 半高全宽

**输出**：
```
products/14B/ec/L2/
├── json/
│   ├── 511keV_ch0_l2_fit_results.json
│   └── L2_manifest.json
└── figures/
    ├── 511keV_ch0_energy_fit.png
    └── ...
```

**L2_fit_results.json格式**：
```json
{
  "stem": "511keV_ch0",
  "bin_width": 4.0,
  "channels": {
    "0": {
      "E": 510.5,
      "mu": 510.5,
      "sigma": 12.3,
      "res": 2.41,
      "FWHM": 28.95,
      "rsquared": 0.999,
      "n_events": 100000
    },
    ...
  }
}
```

### L3 层 - 能量标定

**输入**：L2层的所有拟合结果(json)

**处理**：
- 收集所有能量点：511keV, 661.7keV, 1332.5keV等
- 分别对低能(< 49 keV)和高能(> 51 keV)路部分进行线性标定拟合
- 能量标定模型：$E = a \cdot \text{ch} + b$
- 生成标定曲线和分辨率曲线

**输出**：
```
products/14B/ec/L3/
├── json/
│   ├── 2603281530_14B_EC.json
│   └── L3_manifest.json
└── figures/
    └── 2603281530_14B_EC_calibration.png
```

**L3最终参数json格式**：
```json
{
  "timestamp": "2603281530",
  "payload": "14B",
  "layer": "EC_L3",
  "calibration_params": {
    "low": {
      "a": 0.505,
      "b": 5.23,
      "rmse": 2.34,
      "n_points": 2
    },
    "high": {
      "a": 0.502,
      "b": 8.12,
      "rmse": 1.89,
      "n_points": 1
    }
  },
  "energy_split": {
    "low": 49.0,
    "high": 51.0
  },
  "n_lines_low": 2,
  "n_lines_high": 1
}
```

---

## 文件命名规范

### 时间戳命名

L2和L3最终结果文件使用**时间戳**命名(便于版本追踪和追溯)：

**格式**: `YYMMDDHHmm_LoadName_ProcessType.json`

- **YYMMDDH**Hmm**: 年月日时分(6位年+2位月+2位日+2位时+2位分)
- **LoadName**: 载荷名称(14B 或 15B)
- **ProcessType**: TB 或 EC

**示例**：
- `2603281530_14B_TB.json` - 2026年3月28日15:30生成的14B温偏标定参数
- `2603281530_14B_EC.json` - 同时刻生成的能量标定参数

### 其他命名规范

- **parquet**: `{描述名}.parquet`(如 `511keV_ch0.parquet`)
- **json元数据**: `{描述名}_metadata.json`
- **拟合结果**: `{描述名}_fit_results.json`
- **清单**: `L{n}_manifest.json`(如 `L0_manifest.json`)
- **图像**: `{描述名}_{内容}.png`(如 `511keV_ch0_energy_fit.png`)

---

## 使用指南

### 快速开始

```python
from calibration_lib.config import get_payload_config
from calibration_lib.tb.processors import TBL0Processor, TBL1Processor, TBL2Processor
from calibration_lib.ec.processors import ECL0Processor, ECL1Processor, ECL2Processor, ECL3Processor
from calibration_lib.common import DataLayout

# 获取载荷配置
config = get_payload_config("14B")

# 创建数据布局管理器
layout = DataLayout(Path("./products"), "14B")

# TB处理流程 L0
tb_l0 = TBL0Processor("14B", Path("./products"))
tb_l0_result = tb_l0.process(config.tb_raw_dir)

# TB处理流程 L1
tb_l1 = TBL1Processor("14B", Path("./products"))
tb_l1_result = tb_l1.process(layout.get_tb_l0_dir())

# TB处理流程 L2
tb_l2 = TBL2Processor("14B", Path("./products"))
tb_l2_result = tb_l2.process(layout.get_tb_l1_dir(), config.tb_raw_dir)

# EC处理流程 L0-L3
ec_l0 = ECL0Processor("14B", Path("./products"))
ec_l0_result = ec_l0.process(config.ec_xray_dir)

# ...
```

### 配置参数

见 `calibration_lib/config/payload_config.py`：
- `TB_PROCESS_PARAMS`: TB处理参数
- `EC_PROCESS_PARAMS`: EC处理参数

---

## 数据溯源(Provenance)

每层输出都包含**清单文件**(manifest.json)，记录：
- 输入源
- 处理参数
- 生成时间戳
- 输出文件列表
- 错误记录

**示例**(L0_manifest.json)：
```json
{
  "payload": "14B",
  "layer": "TB_L0",
  "timestamp": "2603281530",
  "raw_dir": "/path/to/raw",
  "n_files": 50,
  "outputs": [
    {"stem": "20C_27.5V", "parquet": "20C_27.5V.parquet", "json": "20C_27.5V_metadata.json"},
    ...
  ]
}
```

---

## 扩展性

### 添加新载荷

1. 在 `payload_config.py` 中添加新的 `PayloadConfig` 对象
2. 添加到 `PAYLOADS` 字典
3. 处理逻辑自动适配(通过payload_name参数)

### 自定义处理参数

在各处理器的 `process()` 方法中传入 `config` 字典覆盖默认参数。

---

## 常见问题

**Q: 为什么使用Parquet格式?**
A: Parquet是列式存储格式，压缩率高(snappy +80%)，读取高效，支持部分列读取，兼容多语言。

**Q: 如何处理新的T,V对应关系?**
A: 在 `L0_processor` 中的 `_parse_tb_pair()` 方法中集成HK数据解析逻辑。

**Q: L2和L3文件名为什么用时间戳?**
A: 便于版本管理、追溯和防止覆盖。L1元数据中的"corrfile"记录TB参数版本，支持完整的数据追踪。

---

## 版本历史

- **v1.0.0** (2026-03-28): 初始版本，规范了TB和EC完整处理流程

---

## 联系方式

有问题或建议? 请提交issue或相关讨论。
