<p align="center">
  <img src="docs/images/grid-logo.svg" width="144" alt="天格计划 GRID">
</p>

<h1 align="center">GRID Calibration</h1>

<p align="center">天格探测器地面标定与交互复核</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="workbench/README.md"><img src="https://img.shields.io/badge/Interface-Web%20%2F%20CLI-3567A8" alt="Web / CLI"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#工作台">工作台</a> ·
  <a href="#处理流程">处理流程</a> ·
  <a href="#脚本与批处理">脚本与批处理</a> ·
  <a href="#更多文档">更多文档</a>
</p>

从原始事件和遥测数据生成温度—偏压校正与能量标定参数。把分层计算、三维曲面、原谱和拟合窗口放在同一工作台中：发现异常点后，可以直接查看原谱、调整窗口、局部重算，再比较结果。

![TB 三维曲面与原谱联动](docs/images/tb-surface.jpg)

<p align="center"><sub>TB L2 三维曲面、工况能谱与残差联动 · 14B 交互演示</sub></p>

## 工作台

| 要做的事 | 在工作台中完成 |
| --- | --- |
| 读取不同载荷的数据 | 配置事件与 HK XML、字段换算、文件配对和工况来源，保存为载荷预设。 |
| 找到需要复核的谱 | 从工况列表或质量地图选择通道，查看能谱、候选峰、拟合曲线、参数误差和残差。 |
| 修改某一个结果 | 拖动拟合窗或输入 ADC 边界，重拟合当前通道；屏蔽、恢复点后更新曲面或能量关系。 |
| 处理一张多峰源谱 | 为每条能线、每个通道设置独立窗口和中心锚，拟合当前峰或当前通道的全部能线。 |
| 保存并继续处理 | 每份试算保存读取配置、TB 依赖、窗口、修订、结果和作业日志，重开后恢复。 |

<details>
<summary><strong>能谱复核：质量地图、工况列表与拟合窗口</strong></summary>

![TB 能谱复核](docs/images/tb-spectrum.png)

选择工况后，在同一页检查原谱与残差，调整窗口并重拟合。

</details>

<details>
<summary><strong>EC 多峰：能线配置与 Co60 谱拟合</strong></summary>

![EC 多能峰编辑](docs/images/ec-source-peaks.png)

同一源谱的能线分别配置，各通道保留独立窗口与结果。

</details>

## 快速开始

需要 Python 3.10 及以上和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下面使用 Python 3.11；Node.js 只用于前端测试。

```powershell
git clone https://github.com/Lee-Eee-Eee/GRID_Calibration_Lib.git calibration_lib
cd calibration_lib
uv sync --python 3.11 --extra workbench
New-Item -ItemType Directory -Force ../grid-workspace | Out-Null
uv run --extra workbench grid-workbench --workspace ../grid-workspace --port 8765
```

打开 **http://127.0.0.1:8765/**，选择数据和产品目录、载荷读取预设，再创建试算。已有原始数据或分层产品都可以作为入口。新结果写入工作区的 `runs/`，图表资源随应用在本机加载。

macOS / Linux 使用 `mkdir -p ../grid-workspace` 创建工作区，其余命令相同。

<details>
<summary>使用 pip 安装（Windows）</summary>

在克隆后的仓库目录运行：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install ".[workbench]"
New-Item -ItemType Directory -Force ../grid-workspace | Out-Null
.venv/Scripts/grid-workbench.exe --workspace ../grid-workspace --port 8765
```

</details>

## 处理流程

**TB 标定**固定入射能量，测量同一能峰在不同温度、偏压下的位置，拟合增益曲面。**EC 标定**使用 TB 参数校正温偏，再从已知能线建立 ADC 与能量的关系。

```mermaid
flowchart LR
    TR["事件 / HK"] --> T0["TB L0<br/>解包与结构化"]
    T0 --> T1["TB L1<br/>谱峰拟合"] --> T2["TB L2<br/>温偏曲面"]
    ER["X 射线 / 放射源 / 本底"] --> E0["EC L0<br/>解包与结构化"]
    E0 --> E1["EC L1<br/>温偏校正"] --> E2["EC L2<br/>能峰拟合"] --> E3["EC L3<br/>能量关系与分辨率"]
    T2 -. "校正参数" .-> E1
```

各层分别保存 Parquet、JSON、拟合图和运行清单。工作台直接调用库内处理器，支持逐层执行和局部重算。目录、模型及输出约定见 [处理与数据指南](docs/processing.md)。

## 脚本与批处理

命令行入口 `grid-calib` 提供 14B / 15B 的七层处理；其他载荷通过工作台读取预设接入。原始数据路径和 CLI 参数见 [处理指南](docs/processing.md#命令行运行)。

```bash
uv run grid-calib 15B TB_L1 --products ../products
uv run grid-calib 15B TB_L1 --products ../products --only m20C-287-106
uv run grid-calib 15B TB_L2 --products ../products
```

也可以在 Python 中调用整条流程：

```python
from pathlib import Path
from calibration_lib.pipeline import CalibrationPipeline

pipeline = CalibrationPipeline("14B", Path("../products"))
pipeline.run_tb()
pipeline.run_ec()
```

## 更多文档

| 文档 | 内容 |
| --- | --- |
| [工作台使用](workbench/README.md) | 数据目录、载荷预设、TB 依赖、试算、运行和结果复核。 |
| [L1 谱复核](docs/l1_workbench.md) | 工况与通道切换、拟合窗口、残差和局部重算。 |
| [处理与数据指南](docs/processing.md) | TB / EC 分层、命令行、Python API、产物与曲面模型。 |
| [寻峰与质量诊断](docs/peak_finding.md) | 候选峰、背景模型、计数误差、选窗和疑似问题提示。 |
| [读取协议](docs/packet_protocols.md) | XML、事件与 HK 包、字段和 CRC。 |
| [原始谱读取复测](docs/readout_crc_validation.md) | 14B / 15B 读取配置、CRC 与谱尾问题。 |
| [N5 预设](docs/n5_preset.md) | N5 数据入口、工况识别和本底配对进展。 |

### 当前适用范围

14B / 15B 提供内置处理配置；10B、11B 和 N5 使用各自的读取预设。10B / 11B 的塑料 CH3 保留模型待定状态；N5 跨照射本底配对尚未接入 EC L2 / L3。EC L3 的拟合方向及分段约定仍需与格式规范统一。

拟合质量以疑似问题提示呈现。数值有效的结果可以继续参与下游计算，手动屏蔽、数值失败和模型待定分别处理。

### 开发检查

```bash
uv sync --extra workbench --extra dev
uv run --extra workbench --extra dev pytest tests -q
node --test tests/frontend/*.test.cjs
```

问题和改进建议可通过 [Issues](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lib/issues) 反馈。
