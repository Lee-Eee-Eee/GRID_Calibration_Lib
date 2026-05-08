from __future__ import annotations

from pathlib import Path
import sys

from calibration_lib.checks._common import check_parquet_basic, check_tb_l0_metadata, summarize
from calibration_lib.common import read_parquet_metadata


def main() -> int:
    payload = sys.argv[1] if len(sys.argv) > 1 else "14B"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("products")
    pdir = root / payload / "ec" / "L1" / "parquet"

    logs = []
    all_ok = True

    files = sorted(pdir.glob("*_corrected.parquet"))
    if not files:
        print(f"No corrected parquet files in {pdir}")
        return 2

    for fp in files:
        ok, tmp = check_parquet_basic(fp, ["amp", "ch", "utc", "temp", "bias", "amp_corr"])
        ok_meta, meta_log = check_tb_l0_metadata(fp)
        logs.extend(tmp)
        logs.append(meta_log)
        meta = read_parquet_metadata(fp)
        if "tb_corrfile" not in meta:
            logs.append(f"[ERR] {fp.name} metadata missing tb_corrfile")
            all_ok = False
        else:
            logs.append(f"[OK] {fp.name} tb_corrfile present")
        all_ok = all_ok and ok and ok_meta

    return summarize(f"EC L1 ({payload})", all_ok, logs)


if __name__ == "__main__":
    raise SystemExit(main())
