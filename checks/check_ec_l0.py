from __future__ import annotations

from pathlib import Path
import sys

from calibration_lib.checks._common import check_parquet_basic, check_tb_l0_metadata, summarize


def main() -> int:
    payload = sys.argv[1] if len(sys.argv) > 1 else "14B"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("products")
    parquet_dir = root / payload / "ec" / "L0" / "parquet"

    logs = []
    all_ok = True
    files = sorted(parquet_dir.glob("*.parquet"))
    if not files:
        print(f"No parquet files in {parquet_dir}")
        return 2

    for fp in files:
        ok, tmp = check_parquet_basic(fp, ["amp", "ch", "utc", "temp", "bias"])
        ok_meta, meta_log = check_tb_l0_metadata(fp)
        logs.extend(tmp)
        logs.append(meta_log)
        all_ok = all_ok and ok and ok_meta

    return summarize(f"EC L0 ({payload})", all_ok, logs)


if __name__ == "__main__":
    raise SystemExit(main())
