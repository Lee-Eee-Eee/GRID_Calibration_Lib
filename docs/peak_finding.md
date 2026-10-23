# 自动寻峰与拟合诊断

`common.peak_finding.detect_photopeak` 是 TB L1 与 EC L2 的共同候选入口。
`config["automatic_window"]=True` 重新推荐窗口；默认保留已有的手工窗口。
手工窗口的 `lo/hi` 原值进入拟合，候选中心替代原先的窗内最大 bin 初值。

## 物理依据

SiPM 的热载流子可以触发与光子信号相同的单像素雪崩，串扰还会产生多个像素的脉冲。
暗计数随温度变化，因此最高峰、固定 ADC 下限与高 R² 都不足以确定标定光电峰。
参见 [Hamamatsu MPPC 技术说明，§3-3、§3-4](https://www.hamamatsu.com/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/ssd/mppc_kapd9008e.pdf)。

背景估计先于寻峰的做法参照 [CERN ROOT TSpectrum](https://root.cern.ch/doc/master/classTSpectrum.html)
所述 SNIP 背景消除与寻峰流程；本实现使用递减窗口的 log-log-sqrt 削峰估计连续背景，
随后在随直方图跨度扩展的几何平滑尺度（1、2、4、8……，最多 128 bin）寻找候选。
SNIP 的窗口随最大尺度扩展，避免把宽光电峰吸收进连续本底。不同尺度的支持分别记录，
半高宽扣除平滑核展宽；已分辨峰的宽度不被粗尺度的曲率模板覆盖。
局部峰性质采用 [SciPy `find_peaks`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.find_peaks.html)。

连续背景与平滑结果只用于候选定位。实际拟合始终使用原始 TB 计数，或按 UTC 时长归一化的 EC 净计数率。
TB 使用 `sqrt(max(N,1))` 计数误差；EC 保留信号与本底的独立方差之和。
候选筛选使用原始谱上 Gaussian 模板相对局部二次本底的加权正交对比，传播本底协方差。
零和 Gaussian 差分核补充陡峭连续谱上的肩部候选，其方差按核权重平方传播。
两侧本底估计的面积显著度也记录在诊断中。
默认 4.5 是候选筛选阈值，不能解释为多重搜索后的全局发现显著度。

## 候选、窗口与模型

- 多尺度稳定的候选参与自动选择。明确手窗或独立中心锚可以保留统计显著的单尺度候选。
- 实测 `noise_ceiling` 优先；缺少实测阈值时，低端窄强分量只标记为经验噪声候选。
  宽暗峰、邻近弱信号或多个证据接近的峰会要求缩窗、暗谱阈值或独立中心锚。
- 自动窗口根据候选宽度生成；高斯中心限制在候选邻域，避免拟合漂移到相邻暗峰。
- TB 先选择目标，再检查该目标的模型。目标残差不合格时保留拒绝结果，
  不转去接受远处较弱的结构。粗尺度合并邻峰时，先在原计数上估计峰宽，再试有限的
  3σ、2.5σ、2σ窗口；已明确指定的手动边界保持原值。
- TB 保留单高斯加零/线性/指数/二次背景四选一；EC 使用单高斯加零/线性/指数背景。
  优先选择能估计高斯参数误差的模型，再比较已知计数方差下的 `χ²+2k`。
- 结果保留 `R²`，同时计算同窗平滑本底与峰模型的 `ΔBIC`、峰面积信噪比、约化 χ²、边界和宽度误差。
  这些指标不满足条件时 `fit_quality.accepted=false`，曲线可检查，TB L2、EC L3 不采用该点。

源谱有多条已知能线时，最强峰不能直接标为最低能线。第一条线必须有明确窗口或独立中心/能量锚。
后续能线可用已接受前峰的能量比例引导候选搜索；比例是局部搜索近似，仍逐峰检查拟合质量。
HPGe 参考谱使用 `infer_noise=false`，窄 HPGe 峰不按 SiPM 的经验暗区规则排除。

## TB 同源工况辅助

对同一通道、同一读取版本的 TB 工况序列，库从 L0 原始计数中选取多尺度、强显著且竞争较弱的候选。
使用实测 HK 温度与偏压，对 `sqrt(center)` 作含 V、T、VT、T² 的稳健局部展开。
这一搜索近似来自过压控制 PDE 与 SiPM 增益、光产额随温度平滑变化的物理关系，
参见 [GRID 原始标定论文 §3.1](https://arxiv.org/html/2109.00669#S3.SS1)。
它提供软中心范围，最终参数和误差仍来自该谱的原始计数拟合。

每个目标都从自己的参考集合中排除，辅助接受的新结果不回流训练参考集合；不读取 TBL2 或历史人工窗。
至少需要 16 个谱、12 个可用参考候选及足够温偏覆盖，并检查偏压正向、温度反向增益趋势和参考残差。
不满足时按单谱规则处理。独立中心锚和手动窗口优先。
`auto_peak_context.json` 保存 L0 内容哈希、参考工况、算法版本和逐点预测；相同输入跨任务复用。
损坏的辅助输入被跳过，当前文件仍独立处理。

10B、11B 的 CH3 为塑料闪烁体，康普顿响应模型暂缓。
这两种硬件的 CH3 可看谱、选窗，TB L1 / EC L2 暂不执行高斯拟合，也不进入新的 TB L2 / EC L3。
该策略跟随读取预设和数据集的硬件编号，修改输出载荷名不会改变响应策略。
其他载荷的 CH3 保留原有流程。
固定四通道的 JSON 使用明确空槽：`placeholder:true`、`fit_performed:false`、
`fit_quality.status:model_pending`；数值字段为零，`fit_mode` 枚举只兼容旧容器，
不表示执行过该模型。空槽不计为拟合成功、不显示曲线、不进入下一层计算。
EC L1 仅对具有 TB 参数的受支持通道校正，暂缓通道不写入矫正事件。

## 参数与记录

`peak_detection` 可设置：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `min_significance` | 4.5 | 原谱 Gaussian 对比显著度下限 |
| `min_sigma_bins` | 1 | 候选固有宽度下限，单位 bin |
| `max_resolution` | 1.2 | 候选 FWHM/ADC 中心上限 |
| `min_resolution` | 0（TB 自动为 0.03） | 候选 FWHM/ADC 中心下限；TB 自动上限为 0.6，可配置 |
| `noise_ceiling` | null | 同 ADC 单位的实测噪声上界 |
| `expected_center` | null | 独立标定提供的 ADC 中心 |
| `expected_tolerance` | 0.25 | 中心相对容差 |
| `expected_energy_keV` | null | EC 中心锚对应的物理能量；用于其他能线的比例搜索 |

记录包含 `peak_detection.algorithm`、`algorithm_sha256`、所有候选、噪声边界、选择中心和提示，
以及 `fit_quality` 的各项数值。无可分辨峰或身份歧义抛出 `PeakDetectionError`，其 `diagnostics`
保留候选证据。寻峰参数按逐次任务配置保存；载荷读取预设保存 XML、字段换算、文件规则与源能线。
本模块不改写原始数据、历史产物或手工窗口资源。
