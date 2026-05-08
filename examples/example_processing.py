#!/usr/bin/env python3
"""
GRID 标定流程示例脚本
演示如何使用calibration_lib进行14B温度偏压和能量标定处理

使用方法:
    python examples/example_processing.py 14B
    python examples/example_processing.py 15B
"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from calibration_lib.pipeline import CalibrationPipeline


def main():
    if len(sys.argv) < 2:
        print("用法: python example_processing.py [载荷名称]")
        print("  载荷名称: 14B 或 15B")
        sys.exit(1)
    
    payload_name = sys.argv[1].upper()
    if payload_name not in ("14B", "15B"):
        print(f"✗ 未知的载荷: {payload_name}")
        sys.exit(1)
    
    product_root = SCRIPT_DIR.parent / "products"
    product_root.mkdir(exist_ok=True)

    print(f"\\n开始处理 {payload_name} 的标定数据")
    print(f"产品根目录: {product_root}")

    pipeline = CalibrationPipeline(payload_name, product_root)

    print("\n[Legacy] 导入原有结果...")
    legacy = pipeline.import_legacy()
    print(f"✓ 导入 {legacy['n_copied']} 个历史结果文件")

    print("\n[TB] 运行 L0->L2...")
    tb = pipeline.run_tb()
    print(f"✓ TB 完成: L0={tb['L0'].get('n_files', 0)}, L1={tb['L1'].get('n_processed', 0)}")

    print("\n[EC] 运行 L0->L3...")
    ec = pipeline.run_ec()
    print(f"✓ EC 完成: L0={ec['L0'].get('n_files', 0)}, L1={ec['L1'].get('n_processed', 0)}, L2={ec['L2'].get('n_processed', 0)}")
    
    print(f"\\n{'='*60}")
    print("✓ 所有处理完成！")
    print("可选校验: python -m calibration_lib.checks.check_tb_l0 14B products")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
