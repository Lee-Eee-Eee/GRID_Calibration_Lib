"""能量标定(EC)处理 - L1层处理器。"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ...common.utils import DataLayout, get_timestamp, read_json, read_parquet, read_parquet_metadata, write_json, write_parquet


class ECL1Processor:
    """EC L1层处理器 - 温偏矫正。"""

    REF_TEMP = 25.0
    REF_BIAS = 28.5

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(
        self,
        ec_l0_dir: Path,
        tb_l2_json_path: Path,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L1",
            "timestamp": get_timestamp(),
            "tb_corrfile": str(tb_l2_json_path.name),
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        tb_params = self._load_tb_params(tb_l2_json_path)
        parquet_dir = ec_l0_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {parquet_dir}")

        # Find background parquets for source data
        bkg_parquets: Dict[str, Path] = {}
        for pf in sorted(parquet_dir.glob("src_bkg_*.parquet")):
            bkg_parquets[pf.stem] = pf

        for parquet_file in sorted(parquet_dir.glob("*.parquet")):
            # Skip background files (they're used as input, not processed independently)
            if parquet_file.stem.startswith("src_bkg_"):
                continue

            try:
                df_l0 = read_parquet(parquet_file)
                stem = parquet_file.stem
                meta = read_parquet_metadata(parquet_file)
                data_type = meta.get("data_type", "xray") if isinstance(meta, dict) else "xray"

                df_l1 = self._apply_correction(df_l0, tb_params)

                # For source data, find and correct the background too
                bkg_info: Dict[str, Any] = {"bkg_mean": 0.0, "bkg_std": 0.0, "n_bkg_events": 0}
                bkg_parquet_name = None
                if data_type == "source":
                    bkg_df_corrected = self._find_and_correct_background(
                        parquet_dir, bkg_parquets, tb_params
                    )
                    if bkg_df_corrected is not None and len(bkg_df_corrected) > 0:
                        # Save corrected background alongside the signal
                        bkg_out_path = self.layout.get_ec_l1_parquet(f"{stem}_bkg_corrected.parquet")
                        write_parquet(bkg_out_path, bkg_df_corrected)
                        bkg_parquet_name = bkg_out_path.name
                        bkg_info = {
                            "bkg_mean": float(bkg_df_corrected["amp_corr"].mean()),
                            "bkg_std": float(bkg_df_corrected["amp_corr"].std()),
                            "n_bkg_events": int(len(bkg_df_corrected)),
                            "bkg_parquet": bkg_parquet_name,
                        }

                file_metadata = {
                    "stem": stem,
                    "data_type": data_type,
                    "energy_keV": meta.get("energy_keV") if isinstance(meta, dict) else None,
                    "source_name": meta.get("source_name") if isinstance(meta, dict) else None,
                    "n_pulses": int(len(df_l1)),
                    "tb_corrfile": str(tb_l2_json_path.name),
                    "background_info": bkg_info,
                    "columns": list(df_l1.columns),
                }

                parquet_out = self.layout.get_ec_l1_parquet(f"{stem}_corrected.parquet")
                write_parquet(parquet_out, df_l1, metadata=file_metadata)

                results["outputs"].append({
                    "stem": stem,
                    "data_type": data_type,
                    "n_pulses": int(len(df_l1)),
                    "parquet": parquet_out.name,
                    "bkg_parquet": bkg_parquet_name,
                })
                results["n_processed"] += 1
                print(f"EC L1: {stem} ({len(df_l1)} pulses, type={data_type})")
            except Exception as exc:
                results["errors"].append({"file": parquet_file.name, "error": str(exc)})
                print(f"Error: {parquet_file.name} - {exc}")

        write_json(self.layout.get_ec_l1_json("L1_manifest.json"), results)
        return results

    def _load_tb_params(self, tb_l2_json_path: Path) -> Dict[int, Dict[str, float]]:
        raw = read_json(tb_l2_json_path)
        # Handle both old format (dict with fit_params key) and new format (list directly)
        if isinstance(raw, dict):
            rows = raw.get("fit_params", raw)
        else:
            rows = raw
        if not isinstance(rows, list):
            raise ValueError(f"Invalid TB L2 json format: {tb_l2_json_path}")

        out: Dict[int, Dict[str, float]] = {}
        for row in rows:
            if not isinstance(row, dict) or "channel" not in row:
                continue
            ch = int(row["channel"])
            out[ch] = {
                "G0": float(row["G0"]),
                "V0": float(row["V0"]),
                "k": float(row.get("k", 0.0215)),
                "b": float(row["b"]),
                "c": float(row["c"]),
            }
        if set(out) != {0, 1, 2, 3}:
            raise ValueError(f"Incomplete TB parameters in {tb_l2_json_path}")
        return out

    def _gain_temp_bias_model(
        self,
        temp: np.ndarray,
        bias: np.ndarray,
        params: Dict[str, float],
    ) -> np.ndarray:
        k = params["k"]
        return (
            params["G0"]
            * (bias - k * temp - params["V0"]) ** 2
            * (-(temp**2) + params["b"] * temp + params["c"])
        )

    def _apply_correction(self, df: pd.DataFrame, tb_params: Dict[int, Dict[str, float]]) -> pd.DataFrame:
        out = df.copy()

        ch_arr = out["ch"].to_numpy(dtype=int)
        temp_arr = out["temp"].to_numpy(dtype=float)
        bias_arr = out["bias"].to_numpy(dtype=float)
        amp_arr = out["amp"].to_numpy(dtype=float)
        amp_corr = np.full_like(amp_arr, np.nan, dtype=float)

        for ch in range(4):
            mask = ch_arr == ch
            if not np.any(mask):
                continue
            params = tb_params[ch]
            current_gain = self._gain_temp_bias_model(temp_arr[mask], bias_arr[mask], params)
            current_gain = np.maximum(current_gain, 1e-12)
            ref_gain = self._gain_temp_bias_model(
                np.full(np.count_nonzero(mask), self.REF_TEMP, dtype=float),
                np.full(np.count_nonzero(mask), self.REF_BIAS, dtype=float),
                params,
            )
            corr = ref_gain / current_gain
            amp_corr[mask] = amp_arr[mask] * corr

        out["amp_corr"] = amp_corr

        # Filter invalid values
        valid = np.isfinite(out["amp_corr"]) & (out["amp_corr"] > 0)
        out = out[valid].reset_index(drop=True)
        return out

    def _find_and_correct_background(
        self,
        l0_parquet_dir: Path,
        bkg_parquets: Dict[str, Path],
        tb_params: Dict[int, Dict[str, float]],
    ) -> Optional[pd.DataFrame]:
        """Find and apply TB correction to background data."""
        if not bkg_parquets:
            return None
        # Use the first (usually only) background file
        bkg_path = next(iter(bkg_parquets.values()))
        try:
            df_bkg = read_parquet(bkg_path)
            if len(df_bkg) == 0:
                return None
            return self._apply_correction(df_bkg, tb_params)
        except Exception:
            return None
