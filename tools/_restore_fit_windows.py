"""一次性脚本：从 1415B_json legacy 配置回填 calibration_lib 资源里的手选窗。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_15B = ROOT / "1415B_json/14_15_tempbiias/output/fit_windows_config.json"
LEGACY_14B = ROOT / "1415B_json/14_15_tempbiias_02/output/fit_windows_config.json"
RAW_15B = ROOT / "14、15B标定数据/03/Temp_Vbias"
RAW_14B = ROOT / "14、15B标定数据/02/Temp_Vbias"
RES_15B = ROOT / "calibration_lib/resources/legacy_fit_windows_config_15B.json"
RES_14B = ROOT / "calibration_lib/resources/legacy_fit_windows_config_14B.json"


def raw_stems(rawdir):
    return [p.name[: -len(".event.dat")] for p in rawdir.glob("*.event.dat")]


def remap(legacy, stems):
    underscore_to_real = {s.replace("-", "_"): s for s in stems}
    out, missing = {}, []
    for k, v in legacy.items():
        real = underscore_to_real.get(k)
        if real is None:
            missing.append(k); continue
        out[real] = v
    return out, missing


def main():
    raw_15B = raw_stems(RAW_15B)
    raw_14B = raw_stems(RAW_14B)
    legacy_15B = json.loads(LEGACY_15B.read_text(encoding="utf-8"))
    legacy_14B = json.loads(LEGACY_14B.read_text(encoding="utf-8"))

    new_15B, m15 = remap(legacy_15B, raw_15B)
    new_14B, m14 = remap(legacy_14B, raw_14B)
    print(f"15B raw={len(raw_15B)} legacy={len(legacy_15B)} remapped={len(new_15B)} missing={m15}")
    print(f"14B raw={len(raw_14B)} legacy={len(legacy_14B)} remapped={len(new_14B)} missing={m14}")

    RES_15B.write_text(json.dumps(new_15B, ensure_ascii=False, indent=2), encoding="utf-8")
    RES_14B.write_text(json.dumps(new_14B, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Wrote", RES_15B)
    print("Wrote", RES_14B)


if __name__ == "__main__":
    main()
