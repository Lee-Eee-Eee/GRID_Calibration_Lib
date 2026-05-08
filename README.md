# GRID Calibration Lib

GRID 伽马射线探测器载荷的**地面标定数据处理库**，按层流水线设计：两条管线（TB / EC），七个处理器，每条管线四级标准化产物，统一 CLI 入口。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Managed by uv](https://img.shields.io/badge/managed%20by-uv-purple.svg)](https://docs.astral.sh/uv/)

GRID（Gamma-Ray Integrated Detectors）是一组小型 SiPM 伽马射线探测器星座。每块载荷在出厂前需要做地面标定，给出两套参数：

- **TB**（Temperature–Bias）—— 增益对温度、偏压的依赖关系，5 参数物理曲面拟合。
- **EC**（Energy Calibration）—— ADC → keV 映射，先做 TB 矫正，再用伽马源（Na-22、Cs-137、Co-60 等）能峰拟合得到。

本库把原始 `.dat` / `.hk` 二进制包文件转成有版本号、可视化校验过的标定参数；每一层产物都落盘，单层重跑、单文件重跑都不需要从头来一遍。

---

## 主要特性

- **L0 → L3 分层产物。** 每层都生成 parquet/JSON + PNG 校验图，问题在哪一层一眼能看出来。
- **TB L1 单峰单背景拟合。** 高斯峰 + 4 选 1 背景（无 / 线性 / 指数 / 二次），按 R²、AIC 自动挑最佳，与上游 `calibration_process.fitting.peak_fit` 对齐。
- **TB L2 自由 k 曲面拟合。** 用 `1/center_err` 加权，参数边界对齐 `calibration_process.util_lib.temp_bias_lmfit`。
- **手选拟合窗口编辑器。** 自动峰检测找错的极少数情况，交互修一次终生不再被覆盖。
- **单文件部分重跑。** 改完一个手选窗，只重画那个文件的 L1，再重跑 L2；其他文件原样保留。
- **每层独立校验脚本。** 内置 `calibration_lib.checks.*` 做 schema / sanity 校验。

---

## 安装

本项目用 [`uv`](https://docs.astral.sh/uv/) 管理。先装 `uv`：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

然后克隆 + 同步（自动建 `.venv/` 并装好所有依赖）：

```bash
git clone https://github.com/Lee-Eee-Eee/GRID_Calibration_Lib.git calibration_lib
cd calibration_lib
uv sync
```

> **关于目录名：** Python 端导入用的是 `calibration_lib` 模块名，所以推荐如上把目录直接克隆成 `calibration_lib`。如果你习惯别的名字，记得 `uv sync` 之后用安装好的入口（`uv run grid-calib …`）而不是 `python -m`。

如需开发工具（pytest、ruff 等）：

```bash
uv sync --extra dev
```

---

## 快速开始

库默认从 `14、15B标定数据/{02,03}/{Temp_Vbias,X,X_new,source}/` 读原始数据，向 `products/{14B,15B}/{tb,ec}/L{0..3}/` 写产物。两个根目录都可以在 [`config/payload_config.py`](config/payload_config.py) 里改。

### 单层运行

```bash
# 14B 走 TB 全流程
uv run python -m calibration_lib.run_layer 14B TB_L0
uv run python -m calibration_lib.run_layer 14B TB_L1
uv run python -m calibration_lib.run_layer 14B TB_L2

# 15B 同上，再跑 EC
uv run python -m calibration_lib.run_layer 15B TB_L0
uv run python -m calibration_lib.run_layer 15B EC_L1   # 自动取最新的 TB_L2 参数文件
```

`uv sync` 装好之后还有一个等价的 CLI 入口：

```bash
uv run grid-calib 14B TB_L1
```

### 只重跑某几个 stem（TB L1）

改完某文件的手选窗后，不需要把全部 56 张图重画一遍：

```bash
uv run python -m calibration_lib.run_layer 15B TB_L1 \
    --only m20C-287-106,0C-285-137
uv run python -m calibration_lib.run_layer 15B TB_L2
```

`--only` 接单个或多个 stem（逗号分隔）。其他文件的 `*_fit_results.json` 与图片原样不动，全局 manifest 做 in-place merge。

### 一键全流程（Python API）

```python
from pathlib import Path
from calibration_lib.pipeline import CalibrationPipeline

pipe = CalibrationPipeline("14B", Path("products"))
pipe.run_tb()              # L0 → L1 → L2
pipe.run_ec()              # L0 → L1 → L2 → L3（用最新的 TB_L2 参数）
```

### 交互修改拟合窗口

```bash
uv run python calibration_lib/tools/fit_window_editor.py
```

手选窗存放在 [`resources/legacy_fit_windows_config_{14B,15B}.json`](resources/)，对 L1 来说是只读输入；只有上面这个编辑器会去写它。

---

## 架构

```
calibration_lib/
├── pipeline.py                    # CalibrationPipeline —— 一键全流程
├── run_layer.py                   # CLI 入口（单层 + --only）
├── config/payload_config.py       # PayloadConfig dataclass、源谱信息、原始路径
│
├── common/
│   ├── utils.py                   # DataLayout（路径管理）、parquet/json I/O、时间戳
│   └── legacy_parser.py           # GRID 包二进制解析器适配层
│
├── tb/processors/                 # TB 流水线（3 层）
│   ├── L0_processor.py            #   raw .dat/.hk → parquet + 每通道 T/V metadata
│   ├── L1_processor.py            #   每文件每通道高斯 + 单背景拟合
│   └── L2_processor.py            #   全局曲面拟合 G(T,V) = G0·(V−kT−V0)²·(−T²+bT+c)
│
├── ec/processors/                 # EC 流水线（4 层）
│   ├── L0_processor.py            #   X 射线 .dat/.hk → parquet（每脉冲带 T、V）
│   ├── L1_processor.py            #   套用 TB 矫正 → amp_corr；记录所用 TB 版本
│   ├── L2_processor.py            #   逐能线峰拟合（511、661.7、1332.5 keV …）
│   └── L3_processor.py            #   能量–道关系，低/高能段分段
│
├── checks/                        # 各层 schema / sanity 校验脚本
├── tools/fit_window_editor.py     # 交互式 Tk 拟合窗口编辑器
├── integration/legacy_import.py   # 将历史结果导入新目录树
└── resources/
    ├── legacy/                    # 内置 GRID 包解析器（parse_grid_data.py + XML）
    └── legacy_fit_windows_config_{14B,15B}.json
```

### 产物目录（每个载荷生成）

```
products/14B/
├── tb/
│   ├── L0/{parquet,json}/
│   ├── L1/{json,figures}/
│   └── L2/{json,figures}/         # YYMMDDHHmm_14B_TB.json （带时间戳）
└── ec/
    ├── L0/{parquet,json}/
    ├── L1/{parquet,json}/
    ├── L2/{json,figures}/
    └── L3/{json,figures}/         # YYMMDDHHmm_14B_EC.json （带时间戳）
```

每层都另写一个 `L{n}_manifest.json`，记录输入、参数、输出文件清单和单文件错误信息。

---

## TB 曲面模型

```
G(T, V) = G0 · (V − k·T − V0)² · (−T² + b·T + c)
```

| 参数 | 含义                  | 初值       | 边界                       |
|----:|----------------------|----------:|----------------------------|
| G0  | 整体增益             | 0.012     | [1e-5, 100]                |
| k   | 击穿电压温漂 / °C    | 0.0184    | [0.001, 0.05]              |
| V0  | 0 °C 时击穿电压       | 24.11     | [23, 25]                   |
| b, c| 温度多项式           | 54.31, 23459.83 | b ∈ [-500,500], c ∈ [1e3, 1e8] |

参数初值与边界完全对齐 `calibration_process.util_lib.temp_bias_lmfit`。曲面拟合用 L1 给出的 `1/center_err` 加权，每通道输出 `chi2_reduced`、`RMSE`、`MAPE`。论文 Eq. 8 参考值 `k = 0.0215 V/°C`，实测一般落在 0.014–0.022。

---

## 校验

每层都有对应的 sanity 检查脚本：

```bash
uv run python -m calibration_lib.checks.check_tb_l0 14B products
uv run python -m calibration_lib.checks.check_tb_l1 14B products
uv run python -m calibration_lib.checks.check_tb_l2 14B products
uv run python -m calibration_lib.checks.check_ec_l0 14B products
uv run python -m calibration_lib.checks.check_ec_l1 14B products
uv run python -m calibration_lib.checks.check_ec_l2 14B products
uv run python -m calibration_lib.checks.check_ec_l3 14B products
```

落盘 schema 见 `wiki/`（layer-by-layer 数据格式说明）。

---

## 文件命名规范

| 位置                | 模式                                            |
|---------------------|------------------------------------------------|
| L0 parquet          | `{stem}.parquet`（stem 直接来自原始文件名）         |
| L1 拟合结果         | `{stem}_fit_results.json` + `{stem}_fit.jpg`   |
| L2 / L3 最终参数    | `YYMMDDHHmm_{payload}_{TB|EC}.json`（带时间戳） |
| Manifest            | `L{n}_manifest.json`                           |
| 手选窗资源          | `legacy_fit_windows_config_{payload}.json`      |

L2 / L3 输出加时间戳是有意为之：每次标定都保留，下游（EC L1）会记录用到的 TB L2 时间戳，做完整溯源。

---

## 路线图

- [ ] 4 种 TB L1 背景模式各加 fixture 测试
- [ ] `legacy_parser` 与上游 `lib_reader` 去重
- [ ] 多载荷批处理入口（`grid-calib all`）
- [ ] 大规模 EC 数据可选 GPU 直方图加速

---

## 贡献

欢迎 PR 和 issue。提交前跑一遍：

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
```

本库刻意贴合上游 `calibration_process` 的数据流约定，新增拟合模式请保持"单峰 + 单背景"，不要叠多组分背景。

---
