"""简化入口：一键运行 TB/EC 与 legacy 结果导入。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from calibration_lib.config import EC_PROCESS_PARAMS, TB_PROCESS_PARAMS, get_payload_config
from calibration_lib.common import DataLayout
from calibration_lib.ec.processors.L0_processor import ECL0Processor
from calibration_lib.ec.processors.L1_processor import ECL1Processor
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.integration.legacy_import import import_legacy_results
from calibration_lib.tb.processors.L0_processor import TBL0Processor
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor


class CalibrationPipeline:
    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.product_root = Path(product_root)
        self.layout = DataLayout(self.product_root, payload_name)
        self.config = get_payload_config(payload_name)

    def run_tb(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        tb_cfg = dict(TB_PROCESS_PARAMS)
        if config:
            tb_cfg.update(config)

        l0 = TBL0Processor(self.payload_name, self.product_root).process(self.config.tb_raw_dir)
        l1 = TBL1Processor(self.payload_name, self.product_root).process(self.layout.get_tb_l0_dir(), config=tb_cfg)
        l2 = TBL2Processor(self.payload_name, self.product_root).process(self.layout.get_tb_l1_dir(), self.layout.get_tb_l0_dir())
        return {"L0": l0, "L1": l1, "L2": l2}

    def run_ec(self, tb_l2_json_path: Optional[Path] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ec_cfg = dict(EC_PROCESS_PARAMS)
        if config:
            ec_cfg.update(config)

        if tb_l2_json_path is None:
            candidates = sorted((self.layout.get_tb_l2_dir() / "json").glob("*_TB.json"))
            if not candidates:
                raise FileNotFoundError("No TB L2 parameter file found")
            tb_l2_json_path = candidates[-1]

        l0 = ECL0Processor(self.payload_name, self.product_root).process(self.config.ec_xray_dir)
        l1 = ECL1Processor(self.payload_name, self.product_root).process(self.layout.get_ec_l0_dir(), tb_l2_json_path)
        l2 = ECL2Processor(self.payload_name, self.product_root).process(self.layout.get_ec_l1_dir(), config=ec_cfg)
        l3 = ECL3Processor(self.payload_name, self.product_root).process(self.layout.get_ec_l2_dir(), config=ec_cfg)
        return {"L0": l0, "L1": l1, "L2": l2, "L3": l3}

    def import_legacy(self) -> Dict[str, Any]:
        return import_legacy_results(self.payload_name, self.product_root)
