"""标定库主模块。"""

from .config.payload_config import get_payload_config
from .pipeline import CalibrationPipeline
from .tb.processors.L0_processor import TBL0Processor
from .tb.processors.L1_processor import TBL1Processor
from .tb.processors.L2_processor import TBL2Processor
from .ec.processors.L0_processor import ECL0Processor
from .ec.processors.L1_processor import ECL1Processor
from .ec.processors.L2_processor import ECL2Processor
from .ec.processors.L3_processor import ECL3Processor

__version__ = "1.0.0"
__all__ = [
    "CalibrationPipeline",
    "get_payload_config",
    "TBL0Processor",
    "TBL1Processor",
    "TBL2Processor",
    "ECL0Processor",
    "ECL1Processor",
    "ECL2Processor",
    "ECL3Processor",
]
