# GRID 数据处理流程快速参考

## 数据层级与职责速查表

| 层级 | 输入 | 输出 | 主要操作 | 时间戳 |
|------|------|------|---------|--------|
| **TB-L0** | .dat, .hk | .parquet + .json | 解析、标准化 | 无 |
| **TB-L1** | .parquet | .json + .png | 直方图、高斯拟合 | 无 |
| **TB-L2** | .json | .json + .png | 2D曲面拟合 | **有** |
| **EC-L0** | .dat, .hk | .parquet + .json | 解析、标准化 | 无 |
| **EC-L1** | .parquet | .parquet + .json | 温偏矫正 | 无 |
| **EC-L2** | .parquet | .json + .png | 能量谱拟合 | 无 |
| **EC-L3** | .json | .json + .png | 能量标定 | **有** |

## TB 处理流程

```
原始数据(*.dat, *.hk)
    ↓ L0: TBL0Processor
    ├─ parquet/: 脉冲数据(amp, ch, utc)
    └─ json/: 元数据(T, V, σ)
    ↓ L1: TBL1Processor
    ├─ json/: 拟合参数(μ, σ, FWHM, fit_mode)
    └─ figures/: 高斯拟合曲线图
    ↓ L2: TBL2Processor
    ├─ json/: G0, V0, k, b, c (时间戳命名)
    └─ figures/: 3D温偏曲面图
```

**关键参数模型**:
$$G(T, V_b) = G_0 \cdot (V_b - k \cdot T - V_0)^2 \cdot (-T^2 + b \cdot T + c)$$

## EC 处理流程

```
X射线数据(*.dat, *.hk)
    ↓ L0: ECL0Processor
    ├─ parquet/: [amp, ch, utc, temp, bias]
    └─ json/: 元数据
    ↓ L1: ECL1Processor (使用TB-L2参数)
    ├─ parquet/: [amp, ch, utc, temp, bias, amp_corr]
    └─ json/: 矫正参数、TB版本追踪
    ↓ L2: ECL2Processor
    ├─ json/: E, μ, σ, res, FWHM
    └─ figures/: 能量谱拟合图
    ↓ L3: ECL3Processor
    ├─ json/: 低/高能标定参数 (时间戳命名)
    └─ figures/: 标定曲线图
```

**关键参数模型**: $E = a \cdot ch + b$ (分别拟合低能和高能段)

## 文件命名速查

### 时间戳命名(TB-L2, EC-L3)
```
{YYMMDDHHmm}_{LoadName}_{Type}.json
2603281530_14B_TB.json
2603281530_15B_EC.json
```

### 其他文件
```
L0/
  parquet/: {description}.parquet
  json/: {description}_metadata.json, L0_manifest.json

L1/
  json/: {description}_fit_results.json
  figures/: {description}_ch{n}_{type}_fit.png

L2(TB)/
  json/: {timestamp}_{payload}_TB.json
  figures/: {timestamp}_{payload}_TB_surface.png

L2(EC)/
  json/: {description}_l2_fit_results.json
  figures/: {description}_ch{n}_energy_fit.png

L3(EC)/
  json/: {timestamp}_{payload}_EC.json
  figures/: {timestamp}_{payload}_EC_calibration.png
```

## Python API 快速使用

```python
from calibration_lib.config import get_payload_config, PAYLOAD_14B
from calibration_lib.tb.processors import TBL0Processor, TBL1Processor, TBL2Processor
from calibration_lib.ec.processors import ECL0Processor, ECL1Processor, ECL2Processor, ECL3Processor
from calibration_lib.common import DataLayout

# 配置
payload = PAYLOAD_14B
product_root = Path("./products")

# TB 完整流程
tb_l0 = TBL0Processor("14B", product_root)
tb_l0.process(payload.tb_raw_dir)

tb_l1 = TBL1Processor("14B", product_root)
tb_l1.process(DataLayout(product_root, "14B").get_tb_l0_dir())

tb_l2 = TBL2Processor("14B", product_root)
tb_l2.process(DataLayout(product_root, "14B").get_tb_l1_dir(), payload.tb_raw_dir)

# EC 完整流程
ec_l0 = ECL0Processor("14B", product_root)
ec_l0.process(payload.ec_xray_dir)

# (需要获取TB L2的json路径)
tb_l2_json = product_root / "14B" / "tb" / "L2" / "json" / "最新的时间戳_14B_TB.json"

ec_l1 = ECL1Processor("14B", product_root)
ec_l1.process(DataLayout(product_root, "14B").get_ec_l0_dir(), tb_l2_json)

ec_l2 = ECL2Processor("14B", product_root)
ec_l2.process(DataLayout(product_root, "14B").get_ec_l1_dir())

ec_l3 = ECL3Processor("14B", product_root)
ec_l3.process(DataLayout(product_root, "14B").get_ec_l2_dir())
```

## 配置参数

### TB处理参数
```python
TB_PROCESS_PARAMS = {
    "fit_mode": "gaussian",  # 或 "lin+gaus", "exp+gaus"
    "auto_window_sigma": 3.0,
    "min_counts": 10,
}
```

### EC处理参数
```python
EC_PROCESS_PARAMS = {
    "bin_width": 4.0,  # keV
    "energy_split_low": 49.0,
    "energy_split_high": 51.0,
    "ref_temp": 25.0,  # °C
    "ref_bias": 28.5,  # V
    "k_fixed": 0.0215,  # V/°C
}
```

## 调试检查清单

- [ ] 原始数据文件对(*.event.dat 与对应的 *.hk)是否完整?
- [ ] L0 parquet 和 json 生成成功?
- [ ] L1 拟合结果的χ²/rsquared是否可接受(>0.99)?
- [ ] L2 温偏曲面拟合RMSE是否合理?
- [ ] L1(EC) 的tb_corrfile是否指向最新的TB-L2参数?
- [ ] L3 标定参数的n_points是否足够(>=2)?
- [ ] 输出json和png文件是否已保存?

## 目录初始化

首次使用需要创建目录结构:

```python
from pathlib import Path
from calibration_lib.common import DataLayout

layout = DataLayout(Path("./products"), "14B")

# TB 目录
layout.get_tb_l0_dir().mkdir(parents=True, exist_ok=True)
layout.get_tb_l1_dir().mkdir(parents=True, exist_ok=True)
layout.get_tb_l2_dir().mkdir(parents=True, exist_ok=True)

# EC 目录
layout.get_ec_l0_dir().mkdir(parents=True, exist_ok=True)
layout.get_ec_l1_dir().mkdir(parents=True, exist_ok=True)
layout.get_ec_l2_dir().mkdir(parents=True, exist_ok=True)
layout.get_ec_l3_dir().mkdir(parents=True, exist_ok=True)
```

或者直接运行各处理器 `process()` 方法(会自动创建目录)

## 常见命令

```bash
# 查看生成的清单
cat products/14B/tb/L0/json/L0_manifest.json

# 查看最新的TB参数
ls -lt products/14B/tb/L2/json/*_14B_TB.json | head -1

# 查看处理进度
grep -r "✓\|✗" products/

# 清理旧结果(谨慎!)
rm -rf products/14B/tb/L2/*
```

---

**Last Updated: 2026-03-28**
