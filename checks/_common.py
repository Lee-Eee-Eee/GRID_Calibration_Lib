"""各层数据结构检查公共函数。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import pandas as pd

from calibration_lib.common import read_parquet_metadata


def _ok(msg: str) -> str:
    return f"[OK] {msg}"


def _err(msg: str) -> str:
    return f"[ERR] {msg}"


def check_required_columns(df: pd.DataFrame, required: Sequence[str]) -> Tuple[bool, str]:
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, _err(f"missing columns: {missing}")
    return True, _ok("columns complete")


def check_json_keys(path: Path, required: Iterable[str]) -> Tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in required if k not in data]
    if missing:
        return False, _err(f"{path.name} missing keys: {missing}")
    return True, _ok(f"{path.name} keys complete")


def check_parquet_basic(path: Path, required_columns: Sequence[str]) -> Tuple[bool, List[str]]:
    logs: List[str] = []
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        return False, [_err(f"{path.name} read failed: {exc}")]

    ok_cols, msg_cols = check_required_columns(df, required_columns)
    logs.append(msg_cols)
    logs.append(_ok(f"{path.name} rows={len(df)}"))
    return ok_cols, logs


def check_tb_l0_metadata(path: Path) -> Tuple[bool, str]:
    meta = read_parquet_metadata(path, meta_key="hk_data")
    if not meta:
        return False, _err(f"{path.name} missing hk_data metadata")
    for key in ("temp", "bias", "temp_err", "bias_err"):
        if key not in meta:
            return False, _err(f"{path.name} metadata missing {key}")
    return True, _ok(f"{path.name} hk_data present")


def summarize(layer: str, all_ok: bool, logs: List[str]) -> int:
    print(f"=== {layer} CHECK ===")
    for line in logs:
        print(line)
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 2
