from __future__ import annotations

import json
from pathlib import Path
import sys

from calibration_lib.checks._common import summarize


def main() -> int:
    payload = sys.argv[1] if len(sys.argv) > 1 else "14B"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("products")
    jdir = root / payload / "ec" / "L3" / "json"

    logs = []
    all_ok = True
    files = sorted(p for p in jdir.glob("*.json") if not p.name.endswith("manifest.json"))
    if not files:
        print(f"No EC L3 json files in {jdir}")
        return 2

    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        cal = data.get("calibration_params", {})
        low = cal.get("low")
        high = cal.get("high")
        if low is None and high is None:
            logs.append(f"[ERR] {fp.name} has no low/high calibration")
            all_ok = False
            continue
        for tag, part in (("low", low), ("high", high)):
            if part is None:
                continue
            miss = [k for k in ("a", "b") if k not in part]
            if miss:
                logs.append(f"[ERR] {fp.name} {tag} missing {miss}")
                all_ok = False
        logs.append(f"[OK] {fp.name}")

    return summarize(f"EC L3 ({payload})", all_ok, logs)


if __name__ == "__main__":
    raise SystemExit(main())
