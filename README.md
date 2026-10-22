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
- **TB L1 单峰单背景拟合。** 高斯峰 + 4 选 1 背景（无 / 线性 / 指数 / 二次），基于计数方差选择模型，保存 R²、参数误差及相对纯背景的质量诊断。
- **TB L2 自由 k 曲面拟合。** 用 `1/center_err` 加权，参数边界对齐 `calibration_process.util_lib.temp_bias_lmfit`。
- **共同自动寻峰与手选窗口。** TB L1、EC L2结合连续背景、多尺度候选和计数误差定位峰；保留精确手窗，暗区歧义可加入实测噪声上界或独立中心锚。
- **单文件部分重跑。** 改完一个手选窗，只重画那个文件的 L1，再重跑 L2；其他文件原样保留。
- **每层独立校验脚本。** 内置 `calibration_lib.checks.*` 做 schema / sanity 校验。
- **按载荷复用读取预设。** 工作台可选择 / 编辑事件与 HK XML、字段换算、文件规则、逐文件映射和源能线，另存为预设、发布不可变版本并关联目录；试算保存完整快照与输入溯源。

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

### 可操作的本地可视化 Demo

在包含 `calibration_lib/`、原始数据和 `products/` 的工作区根目录运行：

```powershell
uv venv calibration_lib/.venv --python 3.11
uv pip install --python calibration_lib/.venv/Scripts/python.exe -r calibration_lib/pyproject.toml --extra workbench --extra dev
calibration_lib/.venv/Scripts/python.exe -X utf8 -m calibration_lib.workbench.server --workspace . --port 8765
```

打开 <http://127.0.0.1:8765>。通过“数据与产品”选择原始数据/产品目录，默认扫描现有14B/15B，输出载荷编号可编辑。支持四通道、七层真实处理、TB三维残差及谱下钻、直接拖窗拟合、TB/EC屏蔽恢复和参数比较。EC可独立选择不同时间的TB L2 JSON包，逐文件核验信号/本底及拟合输入哈希。所有新产物和失败尝试保存在 `runs/<run_id>/`，原始数据和历史 products 只读；保存的试算可重开恢复。图表资源由本地 Plotly 提供，无 CDN。

TB L1 使用工况文件列表、主谱与残差、拟合详情三栏布局。搜索和状态筛选定位文件，用前后按钮连续复核，四通道结果卡直接切换通道。“拟合当前通道”保存窗口并仅重算该通道；“拟合此文件 · 四通道”计算整份文件。批量任务逐通道保存结果，显示已完成数与失败原因。详见 [L1 工作台操作](docs/l1_workbench.md)。

不同载荷可在顶栏“载荷读取配置”中分别选择事件 / HK XML 预设或外部路径、编辑 XML 内容，并修改包标签、字节序、字段映射与物理量换算。文件命名变化时可编辑正则规则，也可直接填写事件 / HK 映射表或导入 CSV / TSV；能量、通道、发射源、源能线、曝光和标称工况均可配置。先预览真实配对及提取值，校验后保存；每份 XML 上限为 1 MiB UTF-8 字节。

温度 / 偏压默认使用 **HK 实测值**，文件名中的工况仅作标称参考；显式切换至文件名、映射表或固定工况模式后才采用相应数值。预览校验后可“另存为新预设”或“保存为新版本”，同时关联当前数据与产品目录。选择目录时优先匹配明确关联；载荷唯一预设可作为后备，冲突时需显式选择。每个试算冻结完整 XML / 配置快照，发布新版不改变已有试算；旧产品目录仍引用其生成时的版本。保存新标准后需从对应 L0 重新解析，界面逐层显示更新覆盖率，禁止混用旧标准输入。源谱计数率归一化仍复用现有 UTC 时长算法。

TB L1、EC L2的“自动寻峰并拟合”实际调用原处理器，窗口由共享寻峰器重新推荐。图中保留候选与噪声边界，详情显示峰模型相对背景的统计证据；拒绝点仍可检查，但不参与新的TB L2 / EC L3拟合。手动拖窗继续使用精确边界。多能线源的首条线需要手窗或独立中心 / 能量锚，避免把最强峰误标为最低能线。算法依据与参数见 [docs/peak_finding.md](docs/peak_finding.md)。

“参考能谱”支持读取 TKA 计数谱，默认目录为工作区 `data/imported/GRID-11B_HPGe_data/`。点击“分析寻峰候选”后，将来源 SHA-256、设置和候选证据保存到独立 `runs/<id>/analysis.json`。横轴保留 ADC 通道，文件名标称能量不替代能量刻度；CNF 二进制及采集表保留为参考输入。

默认“代表样例”会结合历史快照完成操作闭环，计算状态与规范 / CRC 检查分开显示。14B / 15B 的旧40事件、12字节步长配置已修正为38事件、14字节步长；XML可推断重复布局，显式配置优先。旧快照及历史产物保留，需重新解析L0后再进行新的科学评估。全部事件CRC失败会阻断；HK实测模式下全部HK CRC失败也会阻断。部分CRC失败的事件保留检查记录，仅通过CRC的事件进入拟合 / 矫正。现行EC L3的拟合方向与wiki尚有差异。详细操作及验收见 [workbench/README.md](workbench/README.md)。

工作台还提供只读目录索引：新建试算时展开“已索引的数据目录”，可复用保存的目录记录。读取索引不扫描源目录，重新索引由用户显式触发；索引记录大小 / 修改时间而非内容哈希。10B使用独立的560字节事件XML、106字节HK XML、温度折返与电流压降修正、稳态HK选择及0–16384 ADC /6 ADC分箱。默认10B源配对为Cs137观测073与本底075，其他源需要另存读取配置并指定对应本底。

N5正式采集预设可从已复制的manifest注册48个TB工况及72份X射线文件：

```powershell
calibration_lib/.venv/Scripts/python.exe -X utf8 -m calibration_lib.tools.init_n5_preset --workspace . --raw-root "D:/GRID-calibration-data/Seafile/四天格"
```

TB使用HK实测温偏及独立182字节HK XML，已完成真实工况L0 /自动L1。N5的X射线本底需要另一次照射文件中的同一物理通道，当前EC L2 /L3及整条流水线禁用并显示原因，EC L0 /L1保留检查入口。详细规则见 [docs/n5_preset.md](docs/n5_preset.md)。

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

### 流程总览（只读产物浏览）

扫描 `products/` 下 TB / EC 各层产物，生成一份自包含 HTML 总览页（14B/15B 切换、流程图卡片、点击查看每层用途与文件链接）：

```bash
python calibration_lib/tools/flow_overview.py                 # 生成 course_outputs/calibration_flow.html
python calibration_lib/tools/flow_overview.py --check         # 顺便校验所有内嵌链接
```

纯标准库实现，无需安装依赖、无需联网，浏览器双击 HTML 即可查看。只读扫描，不修改任何产物。

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
│   ├── legacy_parser.py           # GRID 包二进制解析器适配层
│   ├── peak_finding.py            # TB/EC 共用候选寻峰及质量诊断
│   └── reference_spectra.py       # HPGe TKA参考谱读取
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
├── tools/flow_overview.py         # 只读扫描 products/，生成自包含 HTML 全流程总览
├── tools/migrate_calibration_data.py # 校验SHA-256并复制开发输入
├── workbench/                    # 目录/预设管理、运行服务及可视化复核入口
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
