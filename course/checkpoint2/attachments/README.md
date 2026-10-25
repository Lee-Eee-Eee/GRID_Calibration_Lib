# 图表、验收记录与小样例

| 目录 | 内容与来源 |
| --- | --- |
| [ab-comparison](ab-comparison/README.md) | A/B 共同基线的 13 张截图和图表原图，含采集清单。A 为 `3f26210`，B 为 `8e66e62`。 |
| [current-demo](current-demo/) | TB/EC 界面、TB L2 三维曲面及残差联动、多能峰编辑、配置/输出预览及修复后能谱，来自 10 月 19—25 日的开发验收。 |
| [validation](validation/) | B 合入、质量提示、EC 多峰与输入修复阶段的实际验收记录和测试输出。 |
| [samples](samples/README.md) | 冻结的 10B 单工况 L1 记录及 64 工况计数直方图，供离线检查。 |
| [background/calibration-pipeline.png](background/calibration-pipeline.png) | 用户提供的原项目 TB/EC 标定流程图，原样收录。 |

A/B 实验截图中，10B 支持高斯峰处理的 192 个通道工况为 179 个通过、13 个拒绝；CH3 的 64 个工况为模型待定。后续主线在 `c3ee6b4` 将质量阈值改为疑似问题提示，数值有效的 192 点继续参与 TB L2，手动屏蔽、数值失败及模型待定分别处理。实验快照与当前版本的状态按各自提交理解。

`current-demo` 图片是对应开发轮次的真实页面记录；与图像一起提供的验收 JSON 保存具体任务和结果。

文件来源与 SHA-256 见 [source-manifest.json](source-manifest.json)。
