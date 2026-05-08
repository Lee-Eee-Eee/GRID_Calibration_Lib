# 数据处理流程集成指南

## 一、现状与下一步

### 已完成部分 ✓

1. **规范化目录结构**
   - 在 `products/14B/` 和 `products/15B/` 中建立了标准的L0-L3分层目录
   - 每层数据独立存储(parquet+json或json+png)

2. **模块化处理框架**
   - `calibration_lib/` 包含完整的处理器类体系
   - TB: `TBL0Processor`, `TBL1Processor`, `TBL2Processor`
   - EC: `ECL0Processor`, `ECL1Processor`, `ECL2Processor`, `ECL3Processor`

3. **统一的配置管理**
   - `payload_config.py` 中定义载荷信息、原始数据路径
   - `utils.py` 提供文件I/O、时间戳管理等通用功能

4. **详细的文档**
   - `README.md` 详细说明各层的数据格式、参数含义
   - `QUICKREF.md` 快速参考速查表
   - `example_processing.py` 完整使用示例

### 本次已完成的集成项 ✓

1. **原始解析代码已接入**
    - 新增 `calibration_lib/common/legacy_parser.py`。
    - `TBL0Processor` / `ECL0Processor` 直接调用 `1415B_json/homework2/parse_grid_data.py` 的 `parse_grid_data_new()`。

2. **TB-L0 元数据改为 Parquet 内嵌**
    - 每个 TB L0 parquet 文件包含 `calib.meta.json` schema metadata。
    - 不再为每个 TB L0 数据文件单独输出 metadata.json。
    - L0 层仍保留 `L0_manifest.json` 记录批处理结果。

3. **原结果已接入新结构**
    - 新增 `calibration_lib/integration/legacy_import.py`。
    - 将 `1415B_json/temp_bias_surface_output/` 与 `1415B_json/xray_output/` 下可用历史结果导入 `products/<payload>/` 对应层。

4. **库结构已简化**
    - 新增统一入口 `calibration_lib/pipeline.py` (`CalibrationPipeline`)。
    - 可直接使用 `run_tb()`、`run_ec()`、`import_legacy()`，减少手动串接处理器。

5. **每一层已增加 check 脚本**
    - `calibration_lib/checks/check_tb_l0.py`
    - `calibration_lib/checks/check_tb_l1.py`
    - `calibration_lib/checks/check_tb_l2.py`
    - `calibration_lib/checks/check_ec_l0.py`
    - `calibration_lib/checks/check_ec_l1.py`
    - `calibration_lib/checks/check_ec_l2.py`
    - `calibration_lib/checks/check_ec_l3.py`

---

## 二、集成使用步骤

### 步骤1: 运行一键流程

```python
from pathlib import Path
from calibration_lib.pipeline import CalibrationPipeline

pipeline = CalibrationPipeline("14B", Path("products"))
pipeline.import_legacy()  # 导入历史结果
pipeline.run_tb()         # TB: L0 -> L2
pipeline.run_ec()         # EC: L0 -> L3
```

### 步骤2: 分层执行数据结构检查

```bash
python -m calibration_lib.checks.check_tb_l0 14B products
python -m calibration_lib.checks.check_tb_l1 14B products
python -m calibration_lib.checks.check_tb_l2 14B products
python -m calibration_lib.checks.check_ec_l0 14B products
python -m calibration_lib.checks.check_ec_l1 14B products
python -m calibration_lib.checks.check_ec_l2 14B products
python -m calibration_lib.checks.check_ec_l3 14B products
```

---

## 三、配置调整

根据实际数据，调整配置参数：

### TB处理参数
```python
# calibration_lib/config/payload_config.py
TB_PROCESS_PARAMS = {
    "fit_mode": "lin+gaus",  # 根据背景情况选择
    "auto_window_sigma": 3.0,  # 调整窗口宽度
    "min_counts": 10,  # 最少事件数
    "bin_width": 1.0,  # 直方图bin宽度
}
```

### EC处理参数
```python
EC_PROCESS_PARAMS = {
    "bin_width": 4.0,  # keV - 按能量分辨率调整
    "energy_split_low": 49.0,  # 低/高能分割点
    "energy_split_high": 51.0,
    "ref_temp": 25.0,  # 参考温度
    "ref_bias": 28.5,  # 参考偏压
}
```

---

## 四、与现有代码的对照表

| 功能 | 现有位置 | 新位置 | 状态 |
|------|---------|--------|------|
| 原始数据解析 | `homework2/parse_grid_data.py` | `TB-L0` | 需集成 |
| TB拟合窗口 | `14_15_tempbiias/fit_window_editor.py` | 待实现 | 部分 |
| TB高斯拟合 | `14_15_tempbiias/process_temp_vbias.py` | `TB-L1` | 部分 |
| TB曲面拟合 | `temp_bias_surface_calibration.py` | `TB-L2` | 框架完 ✓ |
| EC原始处理 | `process_xray_1415B.py` | `EC-L0` | 需集成 |
| EC能量谱拟合 | `process_xray_1415B.py` | `EC-L2` | 参考实现 |

---

## 五、与旧代码的平移计划

完成新处理流程后，旧的 `1415B_json/` 下的代码可以：

1. **保留为参考** - 在 `docs/legacy/` 中归档
2. **逐步去重** - 提取通用功能到 `calibration_lib/`
3. **最终清理** - 新处理流程验证后删除或归档

### 推荐清理时间表

- **第1阶段**: 验证新流程与旧流程结果一致 (1-2周)
- **第2阶段**: 完整的TB+EC处理并生成标定参数 (1周)
- **第3阶段**: 处理新载荷，验证可扩展性 (1周)
- **第4阶段**: 文档完善，制定规范 (1周)
- **第5阶段**: 旧代码归档/删除 (1周)

---

## 六、常见问题与故障排查

### Q: 导入错误 "ModuleNotFoundError: No module named 'calibration_lib'"
**A**: 确保 `calibration_lib` 在Python路径中，或在脚本中添加：
```python
import sys
sys.path.insert(0, "/path/to/GRID")
```

### Q: 占位符方法返回mock数据怎么办?
**A**: 按步骤2-4中的说明集成真实数据解析函数

### Q: 如何处理新数据格式的载荷?
**A**: 
1. 在 `payload_config.py` 中添加新的 `PayloadConfig`
2. 在相应的 `_parse_*` 方法中处理新格式
3. 处理器自动适配(通过payload_name)

### Q: 为什么EC-L1需要TB-L2参数?
**A**: 为了进行温度偏压矫正。如果没有TB标定，可以跳过L1步骤

### Q: 能否并行处理多个载荷?
**A**: 可以，处理器是无状态的，可以在不同进程中同时运行

---

## 七、下一步优化方向

1. **自动化批处理脚本** - 支持batch处理多个载荷/配置
2. **Web UI** - 实时监控处理进度并可视化结果
3. **数据库集成** - 建立标定参数的版本管理和查询系统
4. **单元测试** - 测试每层处理的输入/输出契约
5. **性能优化** - 并行化拟合任务，缓存解析结果

---

**文档更新**: 2026-03-28
