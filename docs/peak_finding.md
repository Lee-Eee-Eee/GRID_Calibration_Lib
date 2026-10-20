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
随后在 1、2、4 bin 平滑尺度寻找局部显著度及半高宽候选。
局部峰性质采用 [SciPy `find_peaks`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.find_peaks.html)。

连续背景与平滑结果只用于候选定位。实际拟合始终使用原始 TB 计数，或按 UTC 时长归一化的 EC 净计数率。
TB 使用 `sqrt(max(N,1))` 计数误差；EC 保留信号与本底的独立方差之和。
显著性用候选两侧的局部线性本底估计，传播本底估计误差及峰区计数误差。
默认 4.5 是候选筛选阈值，不能解释为多重搜索后的全局发现显著度。

## 候选、窗口与模型

- 多尺度稳定的候选参与自动选择。明确手窗或独立中心锚可以保留统计显著的单尺度候选。
- 实测 `noise_ceiling` 优先；缺少实测阈值时，低端窄强分量只标记为经验噪声候选。
  宽暗峰、邻近弱信号或多个证据接近的峰会要求缩窗、暗谱阈值或独立中心锚。
- 自动窗口根据候选宽度生成；高斯中心限制在候选邻域，避免拟合漂移到相邻暗峰。
- TB 保留单高斯加零/线性/指数/二次背景四选一；EC 使用单高斯加零/线性/指数背景。
  优先选择能估计高斯参数误差的模型，再比较已知计数方差下的 `χ²+2k`。
- 结果保留 `R²`，同时计算同窗平滑本底与峰模型的 `ΔBIC`、峰面积信噪比、约化 χ²、边界和宽度误差。
  这些指标不满足条件时 `fit_quality.accepted=false`，曲线可检查，TB L2、EC L3 不采用该点。

源谱有多条已知能线时，最强峰不能直接标为最低能线。第一条线必须有明确窗口或独立中心/能量锚。
后续能线可用已接受前峰的能量比例引导候选搜索；比例是局部搜索近似，仍逐峰检查拟合质量。
HPGe 参考谱使用 `infer_noise=false`，窄 HPGe 峰不按 SiPM 的经验暗区规则排除。

## 参数与记录

`peak_detection` 可设置：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `min_significance` | 4.5 | 包含局部本底不确定性的候选面积信噪比下限 |
| `min_sigma_bins` | 1 | 候选固有宽度下限，单位 bin |
| `max_resolution` | 1.2 | 候选 FWHM/ADC 中心上限 |
| `noise_ceiling` | null | 同 ADC 单位的实测噪声上界 |
| `expected_center` | null | 独立标定提供的 ADC 中心 |
| `expected_tolerance` | 0.25 | 中心相对容差 |
| `expected_energy_keV` | null | EC 中心锚对应的物理能量；用于其他能线的比例搜索 |

记录包含 `peak_detection.algorithm`、`algorithm_sha256`、所有候选、噪声边界、选择中心和提示，
以及 `fit_quality` 的各项数值。无可分辨峰或身份歧义抛出 `PeakDetectionError`，其 `diagnostics`
保留候选证据。寻峰参数按逐次任务配置保存；载荷读取预设保存 XML、字段换算、文件规则与源能线。
本模块不改写原始数据、历史产物或手工窗口资源。
