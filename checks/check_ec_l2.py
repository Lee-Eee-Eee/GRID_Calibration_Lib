from __future__ import annotations

import json
from pathlib import Path
import sys

from calibration_lib.checks._common import summarize


def main() -> int:
    payload = sys.argv[1] if len(sys.argv) > 1 else "14B"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("products")
    jdir = root / payload / "ec" / "L2" / "json"

    logs = []
    all_ok = True
    files = sorted(jdir.glob("*_l2_fit_results.json"))
    if not files:
        print(f"No L2 fit json files in {jdir}")
        return 2

    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        channels = data.get("channels", {})
        if not channels:
            logs.append(f"[ERR] {fp.name} channels empty")
            all_ok = False
            continue
        for ch, row in channels.items():
            need = ["E", "mu", "sigma", "res", "FWHM"]
            miss = [k for k in need if k not in row]
            if miss:
                logs.append(f"[ERR] {fp.name} ch{ch} missing {miss}")
                all_ok = False
        logs.append(f"[OK] {fp.name} channels={len(channels)}")

    return summarize(f"EC L2 ({payload})", all_ok, logs)


if __name__ == "__main__":
    raise SystemExit(main())
