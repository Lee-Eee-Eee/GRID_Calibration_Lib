"""把原目录中的历史结果导入到新 products 结构。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Iterable, List

from calibration_lib.common import DataLayout, get_timestamp, write_json
from calibration_lib.integration.organize_products import organize_payload


def _payload_id(payload_name: str) -> str:
    return "02" if payload_name == "14B" else "03"


def _payload_tbfit_dir(root: Path, payload_name: str) -> Path:
    # 旧目录中: 14B -> 14_15_tempbiias_02, 15B -> 14_15_tempbiias
    if payload_name == "14B":
        return root / "1415B_json" / "14_15_tempbiias_02"
    return root / "1415B_json" / "14_15_tempbiias"


def _copy_files(files: Iterable[Path], target_dir: Path, copied: List[str], prefix: str = "legacy_") -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for fp in files:
        if not fp.exists() or not fp.is_file():
            continue
        dst = target_dir / f"{prefix}{fp.name}"
        shutil.copy2(fp, dst)
        copied.append(str(dst))


def import_legacy_results(payload_name: str, product_root: Path, root_dir: Path | None = None) -> Dict[str, object]:
    root = Path(root_dir) if root_dir is not None else Path(product_root).resolve().parent
    layout = DataLayout(product_root, payload_name)
    pid = _payload_id(payload_name)

    tb_out = root / "1415B_json" / "temp_bias_surface_output"
    ec_out = root / "1415B_json" / "xray_output"
    tbfit_dir = _payload_tbfit_dir(root, payload_name)

    copied = []

    # TB L1: 历史逐点拟合结果与图、fit window 配置
    _copy_files((tbfit_dir / "output").glob("data_fit_*.json"), layout.get_tb_l1_dir() / "json", copied)
    _copy_files((tbfit_dir / "output").glob("data_fit_*.jpg"), layout.get_tb_l1_dir() / "figures", copied)

    fit_window_cfg = tbfit_dir / "output" / "fit_windows_config.json"
    if fit_window_cfg.exists():
        dst = layout.get_tb_l1_json("legacy_fit_windows_config.json")
        shutil.copy2(fit_window_cfg, dst)
        copied.append(str(dst))

    # TB L2: 历史温偏拟合参数与图
    tb_candidates = [
        tb_out / f"{pid}_tbfit_params.json",
        tb_out / f"{pid}_tbfit_lt.json",
        tb_out / f"GRID-{payload_name}_Temp_Bias_fit_params.json",
    ]
    for fp in tb_candidates:
        if fp.exists():
            dst = layout.get_tb_l2_json(f"legacy_{fp.name}")
            shutil.copy2(fp, dst)
            copied.append(str(dst))

    _copy_files(tb_out.glob(f"{pid}_tbfit_ch*.png"), layout.get_tb_l2_dir() / "figures", copied)

    # EC L2/L3: 历史 xray 目录下分层结果
    ec_payload_dir = ec_out / pid
    if ec_payload_dir.exists():
        _copy_files((ec_payload_dir / "fits").glob("*.json"), layout.get_ec_l2_dir() / "json", copied)
        _copy_files((ec_payload_dir / "figures").glob("*.png"), layout.get_ec_l2_dir() / "figures", copied)

        _copy_files(ec_payload_dir.glob("*.json"), layout.get_ec_l3_dir() / "json", copied)
        _copy_files(ec_payload_dir.glob("*.png"), layout.get_ec_l3_dir() / "figures", copied)
        _copy_files(ec_payload_dir.glob("*.npy"), layout.get_ec_l3_dir() / "artifacts", copied)

    merged_candidates = [
        ec_out / "02_03_xray_ec_fit_params_merged.json",
        ec_out / "02_03_ec_fit_params_merged.json",
    ]
    for fp in merged_candidates:
        if fp.exists():
            dst = layout.get_ec_l3_json(f"legacy_{fp.name}")
            shutil.copy2(fp, dst)
            copied.append(str(dst))

    report = {
        "payload": payload_name,
        "layer": "LEGACY_IMPORT",
        "timestamp": get_timestamp(),
        "n_copied": len(copied),
        "copied_files": copied,
    }

    organize_report = organize_payload(product_root=product_root, payload=payload_name)
    report["organize"] = organize_report

    write_json(layout.get_ec_l3_json("legacy_import_manifest.json"), report)
    return report
