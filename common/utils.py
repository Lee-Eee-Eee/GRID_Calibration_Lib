"""通用工具函数 - 文件I/O、Parquet metadata、数据布局。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd


PARQUET_META_JSON_KEY = "calib.meta.json"


def ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any, ensure_parent: bool = True) -> None:
    """写入JSON文件"""
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_json(path: Path) -> Any:
    """读取JSON文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_parquet(
    path: Path,
    df: pd.DataFrame,
    ensure_parent: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """写入 Parquet 文件，可选写入 JSON metadata 到 schema metadata。"""
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        if metadata is not None:
            raise RuntimeError("pyarrow is required for parquet metadata writing") from exc
        if ensure_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, compression="snappy", index=False)
        return

    table = pa.Table.from_pandas(df, preserve_index=False)
    if metadata is not None:
        encoded = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        current = dict(table.schema.metadata or {})
        current[PARQUET_META_JSON_KEY.encode("utf-8")] = encoded
        table = table.replace_schema_metadata(current)

    pq.write_table(table, path, compression="snappy")


def read_parquet(path: Path) -> pd.DataFrame:
    """读取Parquet文件"""
    return pd.read_parquet(path)


def read_parquet_metadata(path: Path) -> Dict[str, Any]:
    """读取 Parquet schema metadata 中保存的 JSON metadata。"""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {}

    schema = pq.read_schema(path)
    meta = schema.metadata or {}
    raw = meta.get(PARQUET_META_JSON_KEY.encode("utf-8"))
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def get_timestamp() -> str:
    """获取当前时间戳 (格式: YYMMDDHHmm)"""
    return datetime.now().strftime("%y%m%d%H%M")


def get_timestamp_full() -> str:
    """获取完整时间戳 (格式: YYMMDDHHmmss)"""
    return datetime.now().strftime("%y%m%d%H%M%S")


class DataLayout:
    """数据存储布局管理"""
    
    def __init__(self, product_root: Path, payload_name: str):
        self.product_root = product_root
        self.payload_name = payload_name
        self.payload_root = product_root / payload_name
    
    def get_tb_l0_dir(self) -> Path:
        return self.payload_root / "tb" / "L0"
    
    def get_tb_l1_dir(self) -> Path:
        return self.payload_root / "tb" / "L1"
    
    def get_tb_l2_dir(self) -> Path:
        return self.payload_root / "tb" / "L2"
    
    def get_ec_l0_dir(self) -> Path:
        return self.payload_root / "ec" / "L0"
    
    def get_ec_l1_dir(self) -> Path:
        return self.payload_root / "ec" / "L1"
    
    def get_ec_l2_dir(self) -> Path:
        return self.payload_root / "ec" / "L2"
    
    def get_ec_l3_dir(self) -> Path:
        return self.payload_root / "ec" / "L3"
    
    def get_tb_l0_parquet(self, name: str) -> Path:
        """获取TB L0 parquet文件路径"""
        return ensure_dir(self.get_tb_l0_dir() / "parquet") / name
    
    def get_tb_l0_json(self, name: str) -> Path:
        """获取TB L0 json文件路径"""
        return ensure_dir(self.get_tb_l0_dir() / "json") / name

    def list_tb_l0_parquet(self):
        return sorted((self.get_tb_l0_dir() / "parquet").glob("*.parquet"))
    
    def get_tb_l1_json(self, name: str) -> Path:
        """获取TB L1 json文件路径"""
        return ensure_dir(self.get_tb_l1_dir() / "json") / name
    
    def get_tb_l1_figure(self, name: str) -> Path:
        """获取TB L1 图像文件路径"""
        return ensure_dir(self.get_tb_l1_dir() / "figures") / name
    
    def get_tb_l2_json(self, name: str) -> Path:
        """获取TB L2 json文件路径"""
        return ensure_dir(self.get_tb_l2_dir() / "json") / name
    
    def get_tb_l2_figure(self, name: str) -> Path:
        """获取TB L2 图像文件路径"""
        return ensure_dir(self.get_tb_l2_dir() / "figures") / name
    
    def get_ec_l0_parquet(self, name: str) -> Path:
        """获取EC L0 parquet文件路径"""
        return ensure_dir(self.get_ec_l0_dir() / "parquet") / name
    
    def get_ec_l0_json(self, name: str) -> Path:
        """获取EC L0 json文件路径"""
        return ensure_dir(self.get_ec_l0_dir() / "json") / name

    def list_ec_l0_parquet(self):
        return sorted((self.get_ec_l0_dir() / "parquet").glob("*.parquet"))
    
    def get_ec_l1_parquet(self, name: str) -> Path:
        """获取EC L1 parquet文件路径"""
        return ensure_dir(self.get_ec_l1_dir() / "parquet") / name
    
    def get_ec_l1_json(self, name: str) -> Path:
        """获取EC L1 json文件路径"""
        return ensure_dir(self.get_ec_l1_dir() / "json") / name
    
    def get_ec_l2_json(self, name: str) -> Path:
        """获取EC L2 json文件路径"""
        return ensure_dir(self.get_ec_l2_dir() / "json") / name
    
    def get_ec_l2_figure(self, name: str) -> Path:
        """获取EC L2 图像文件路径"""
        return ensure_dir(self.get_ec_l2_dir() / "figures") / name
    
    def get_ec_l3_json(self, name: str) -> Path:
        """获取EC L3 json文件路径"""
        return ensure_dir(self.get_ec_l3_dir() / "json") / name
    
    def get_ec_l3_figure(self, name: str) -> Path:
        """获取EC L3 图像文件路径"""
        return ensure_dir(self.get_ec_l3_dir() / "figures") / name
