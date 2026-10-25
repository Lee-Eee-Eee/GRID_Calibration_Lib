# 分箱核查（2026-10-25）

核查入口为保存的读取预设、不可变任务产物，以及当前8765谱读取接口。没有重新拟合或改写原始输入。

| 项目 | 实际做法 |
|---|---|
| 10B TB | 固定6 ADC等宽bin；配置0–16384 ADC，末边界向上补齐为16386 ADC，共2731 bin。192条受支持L1记录设置一致，实际谱接口步长也是6 ADC。 |
| 14B/15B TB默认 | 1 ADC；预设默认范围0–1200 ADC；旧无显式配置的谱显示可按99.8%分位数缩短上限。 |
| EC | 固定4个TB校正后ADC每bin，横轴尚未转换为keV。保存记录可带独立bin_width，Inspector按记录值读取。 |
| TB拟合误差 | 对原始计数N拟合，σ=sqrt(max(N,1))；平滑谱用于候选/诊断，未作为最终拟合数据。 |
| EC拟合误差 | 信号/本底分别按实测UTC跨度归一化；净率N/t−Nb/tb，误差sqrt(max(N,1)/t²+max(Nb,1)/tb²)。 |
| EC导出图 | PNG中的3-bin均值合并仅用于显示（默认相当于12 ADC采样）；实际拟合和交互谱使用4 ADC原生bin。 |

真实14B CH1 EC谱读取接口的6559个bin，与`ECL2Processor._prepare_spectrum`返回的横轴、净计数率和误差逐值一致。证据为`binning-audit.json`。

目前Gaussian模型在bin中心取值，没有对每bin边界做Gaussian积分，也没有从σ中扣除bin展宽。此批10B最窄σ为11.863 ADC，最大bin_width/σ约0.506。调整bin宽会改变每bin计数与统计误差，拟合幅度也随bin宽缩放；中心与σ均以ADC表示。

本轮检查起点发现并已告知实现者的衔接点：

- EC处理器可收`cfg.bin_width`，但工作台提交/适配器未透传；在旧8 ADC点点击拟合可能回到默认4 ADC，应优先保留所见点的分箱。
- EC背景通道评分曾固定4 ADC，应与当前拟合bin宽一致。
- 处理器需要至少8个拟合bin，原拖窗最小值却固定8 ADC；10B6 ADC应至少48 ADC、EC4 ADC至少32 ADC。界面和接口需使用同一宽度约束。
- TB下方显示残差使用sqrt(max(model,1))，实际拟合χ²使用sqrt(max(N_observed,1))，因此显示RMS不严格等于sqrt(约化χ²)。

质量改为仅警告时，需同时覆盖：TB L2 `_collect_fit_data`、ComputeAdapter曲面`used`、service EC `draft_used`、ECInspector `quality_pass`/单调性筛选、EC L3质量与单调筛选、EC L2跨能线锚及质量抛错。旧`accepted:false`是诊断，不能再隐式触发这些下游过滤；`fit_failed`、placeholder及缺失数值仍需独立识别。
