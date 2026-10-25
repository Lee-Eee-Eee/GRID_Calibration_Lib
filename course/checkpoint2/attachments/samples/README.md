# 10B 离线小样例

- `108_observe_50C_265_fit_results.l1.json`：原样复制自 A 实验冻结工作区的 run `20261023-111431-6e9fdd63c95a`。B 对应文件的 SHA-256 相同：`e69fd46d7b590b91c5312095aaa29a823ae2eab0cc17f7e58bd10dbf986b68ac`。一份 JSON 包含 CH0—CH3；CH2 峰位为 257.508496805 ADC、状态 `rejected`，CH3 为 `model_pending`。CH3 中的零值是占位字段，读取时应结合 `fit_performed=false` 和质量状态。
- `cache-histograms.npz`：原样复制自 `runs/20261023-10b-peak-study/`，含 64 个工况、4 通道的计数直方图、实测温偏、事件数与文件名。`counts` 为 `(64, 4, 2731)`，`x` 为 ADC bin 中心；另保留较宽谱范围的直方图。

可用 Python、NumPy 读取：

```python
from pathlib import Path
import json
import numpy as np

samples = Path("attachments/samples")
rows = json.loads((samples / "108_observe_50C_265_fit_results.l1.json").read_text(encoding="utf-8"))
print([(row["channel"], row["center"], row["fit_quality"]["status"]) for row in rows])
with np.load(samples / "cache-histograms.npz", allow_pickle=False) as data:
    i = list(data["stems"]).index("108_observe_50C_265")
    print(data["counts"][i, 2].shape, data["temp"][i, 2], data["bias"][i, 2])
```

这两份文件用于核对已保存的谱与结果。完整解包和七层重算仍需原始事件/HK 文件及相应读取预设。
