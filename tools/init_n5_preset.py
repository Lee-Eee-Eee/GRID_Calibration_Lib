"""Register the copied N5 official TB archive as a reusable reading preset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calibration_lib.workbench.acquisition_store import prepare_profile, profile_digest
from calibration_lib.workbench.n5_preset import build_n5_profile
from calibration_lib.workbench.presets import PresetRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True, help="Copied archive root containing 温度偏压/N5 and x/N5")
    parser.add_argument("--name", default="N5 官方48工况 · HK182 · Am241")
    args = parser.parse_args()
    profile, summary = build_n5_profile(args.raw_root)
    registry = PresetRegistry(args.workspace)
    existing = [item for item in registry.list("N5")["presets"] if item["name"] == args.name]
    if len(existing) > 1:
        raise ValueError("同名 N5 预设不唯一，请指定其他 --name")
    if existing:
        current = registry.get(existing[0]["id"])
        if profile_digest(prepare_profile(args.workspace, profile)) == current["reference"]["sha256"]:
            registry.bind(existing[0]["id"], current["reference"]["version"],
                          [{"role": "raw", "path": str(args.raw_root.resolve())}], reason="关联已验证的 N5 副本目录")
            print(json.dumps({"reference": current["reference"], "summary": summary, "unchanged": True}, ensure_ascii=False))
            return
    result = registry.save(profile=profile, name=args.name, payload="N5",
                           preset_id=existing[0]["id"] if existing else None,
                           reason="N5 official48 TB + X索引；HK182/38×14、实测HK温偏、59.54keV；EC2/3待正确同道本底接口",
                           bindings=[{"role": "raw", "path": str(args.raw_root.resolve())}])
    print(json.dumps({"reference": result["reference"], "summary": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
