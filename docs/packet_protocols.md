# 载荷包格式与遥测转换

14B、15B 当前原始包是 584 byte 的 HNU 格式：44 byte 前缀、38 个 14 byte 事件、8 byte 尾部。
默认载荷预设显式保存 `event_multi_evt=38`、`event_multi_step=14`。
未指定重复参数的库调用根据 XML 中重复字段的完整跨度与尾部位置推断：
584 byte 为 38/14，旧 516 byte 为 40/12，库内另一 528 byte 格式为 41/12。
用户显式配置的重复参数保留；全事件 CRC 失败会阻止生成 L0。

10B 预设采用 `resources/legacy/grid10_packet.xml`：`grid1x_wf_packet` 为 560 byte 波形包，
`hk_grid1x_packet` 为 106 byte 遥测包，每包一个事件。
XML 从原项目 `lib_reader/src/lib_reader/reader10/grid_packet.xml` 复制，原文件保持只读。

## 遥测转换

命名转换依据原项目 `reader10/read.py` 的 `temp_rebuild` 与 `single_read10`，以及 `reader11/read.py`。
`fields.current` 可编辑电流字段名；电流修正模式要求 XML 中存在对应四通道电流字段。

| 配置 | 物理换算 |
|---|---|
| `temp_mode=affine` | `T = raw * temp_scale + temp_offset`，默认14/15保持 `0.01*raw−273.15 °C` |
| `temp_mode=grid10_signed` | `t=raw/256`，`t>128` 时减255，得到°C |
| `bias_mode=affine` | `V=raw * bias_scale + bias_offset`，默认14/15保持 `raw/1000 V` |
| `bias_mode=grid10_current_drop` | `20.57*2.5*Vraw/4096 − 499*2.5*Iraw/(4096*548.88)` V |
| `bias_mode=grid11_current_drop` | `Vraw/1000 − 499*Iraw*10⁻⁶` V |

10B 默认 `hk_selection=stable_plateau`：以 HK 尾部实测偏压的中位数为中心，在
`stable_bias_tolerance=0.2 V` 内选择最长连续稳态片段，温度使用同一片段。
14/15 保留 `legacy_tail`。这个选择使用遥测值，文件名中的名义偏压用于配对和工况标记。

HK 工况读取会排除 CRC 失败的遥测包；全 HK CRC 失败时拒绝使用该温偏。
L0 保留事件 CRC 状态供检查，TB L1、EC L1/L2 仅使用通过 CRC 的事件，并记录排除计数。
历史全 CRC 失败输入同样拒绝进入新计算。

## 10B 读取与谱范围

模板自动配对 `tb_data/*_observe_*` 与同编号 `*_hk_*`，X光机文件提取能量与目标通道。
源文件名没有曝光信息，因此规则的 `metadata` 常量按 `src_data/data_des.csv` 设置：
默认只启用 Cs137 的 073 源谱60 min与075本底30 min。
Co60、Na22、Am241 的能线可选；切换源时需要另存预设并选择其对应本底及曝光，
分别为077/078的60/30 min、085/086的6/6 min、089/090的15/7.5 min。

10B 默认 `fitting.tb_histogram={min:0,max:16384,bin_width:6}`，通过 L0 acquisition metadata
传给 TB L1，拟合记录保存实际直方图配置。14/15 的默认 bin 与范围保持不变。
界面窗口、谱和实际处理器共享同一配置；范围限定0–65536 ADC、9–65536个bin。
