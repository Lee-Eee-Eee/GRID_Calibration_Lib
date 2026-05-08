from __future__ import annotations

import json
from pathlib import Path
import sys

from calibration_lib.checks._common import summarize


def main() -> int:
    payload = sys.argv[1] if len(sys.argv) > 1 else "14B"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("products")
    json_dir = root / payload / "tb" / "L2" / "json"

    logs = []
    all_ok = True
    files = sorted(p for p in json_dir.glob("*.json") if not p.name.endswith("manifest.json"))
    if not files:
        print(f"No TB L2 json files in {json_dir}")
        return 2

    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        need = ["G0", "V0", "k", "b", "c"]
        miss = [k for k in need if k not in data]
        if miss:
            logs.append(f"[ERR] {fp.name} missing {miss}")
            all_ok = False
        else:
            logs.append(f"[OK] {fp.name}")

    return summarize(f"TB L2 ({payload})", all_ok, logs)


if __name__ == "__main__":
    raise SystemExit(main())
