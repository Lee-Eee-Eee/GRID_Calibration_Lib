import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

from calibration_lib.common import DataLayout
from calibration_lib.config import get_payload_config, EC_PROCESS_PARAMS, TB_PROCESS_PARAMS
from calibration_lib.tb.processors.L0_processor import TBL0Processor
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor
from calibration_lib.ec.processors.L0_processor import ECL0Processor
from calibration_lib.ec.processors.L1_processor import ECL1Processor
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor

def main():
    parser = argparse.ArgumentParser(description="分级运行 GRID 标定流程")
    parser.add_argument("payload", type=str, choices=["14B", "15B", "GRID-14B", "GRID-15B"], help="载荷名称")
    parser.add_argument("layer", type=str, choices=["TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3"], help="要单独运行的层级")
    parser.add_argument("--products", type=str, default="products", help="输出产品目录")
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="(仅 TB_L1) 只重跑这些 stem，逗号分隔，例如 --only m20C-287-106,0C-285-137",
    )

    args = parser.parse_args()

    product_root = Path(args.products).resolve()
    layout = DataLayout(product_root, args.payload)
    config = get_payload_config(args.payload)

    print("=" * 60)
    print(f"载荷: {args.payload} | 模块: {args.layer}")
    print("=" * 60)

    only_stems = [s.strip() for s in args.only.split(",")] if args.only else None
    if only_stems and args.layer != "TB_L1":
        print(f"⚠ --only 仅 TB_L1 支持，已忽略（当前层级：{args.layer}）")

    try:
        if args.layer == "TB_L0":
            TBL0Processor(args.payload, product_root).process(config.tb_raw_dir)
        elif args.layer == "TB_L1":
            TBL1Processor(args.payload, product_root).process(
                layout.get_tb_l0_dir(),
                TB_PROCESS_PARAMS,
                only_stems=only_stems,
            )
        elif args.layer == "TB_L2":
            TBL2Processor(args.payload, product_root).process(layout.get_tb_l1_dir(), layout.get_tb_l0_dir())
        elif args.layer == "EC_L0":
            # 兼容带有Source测试的文件
            ECL0Processor(args.payload, product_root).process(
                config.ec_xray_dir, 
                source_dir=config.ec_source_dir, 
                source_specs=config.source_specs
            )
        elif args.layer == "EC_L1":
            # 自动寻找TB L2最近的配置文件
            candidates = sorted((layout.get_tb_l2_dir() / "json").glob("*_TB.l2.json"))
            if not candidates:
                candidates = sorted((layout.get_tb_l2_dir() / "json").glob("*_TB.json"))
            if not candidates:
                raise FileNotFoundError("未找到TB L2生成的参数文件，请先运行 TB_L2")
            tb_l2_json_path = candidates[-1]
            ECL1Processor(args.payload, product_root).process(layout.get_ec_l0_dir(), tb_l2_json_path)
        elif args.layer == "EC_L2":
            ECL2Processor(args.payload, product_root).process(layout.get_ec_l1_dir(), EC_PROCESS_PARAMS)
        elif args.layer == "EC_L3":
            ECL3Processor(args.payload, product_root).process(layout.get_ec_l2_dir(), EC_PROCESS_PARAMS)
            
        print(f"\n✓ {args.layer} 运行完成。")
        
    except Exception as e:
        print(f"✗ {args.layer} 运行失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
