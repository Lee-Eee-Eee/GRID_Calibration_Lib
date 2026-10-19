"""能量标定(EC)处理 - L1层处理器。"""

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ...common.utils import (
    DataLayout,
    get_timestamp,
    read_json,
    read_parquet,
    read_parquet_metadata,
    write_json,
    write_parquet,
)
from ...config.payload_config import PayloadConfig


class ECL1Processor:
    """EC L1层处理器 - 温偏矫正。"""

    REF_TEMP = 25.0
    REF_BIAS = 28.5

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)

    def process(
        self,
        config: PayloadConfig,
        tb_l2_json_path: Path,
        only_stems: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L1",
            "timestamp": get_timestamp(),
            "tb_corrfile": str(tb_l2_json_path.name),
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        tb_params = self._load_tb_params(tb_l2_json_path)
        tb_params_used = {
            str(ch): {
                "G0": float(p["G0"]),
                "k": float(p["k"]),
                "V0": float(p["V0"]),
                "b": float(p["b"]),
                "c": float(p["c"]),
            }
            for ch, p in tb_params.items()
        }

        parquet_dir = self.layout.get_ec_l0_dir() / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {parquet_dir}")

        src_re = re.compile(r"^src_(?P<name>.+)_(?P<min>\d+(?:\.\d+)?)m\.l0\.parquet$")
        bkg_re = re.compile(r"^src_bkg_(?P<min>\d+(?:\.\d+)?)m\.l0\.parquet$")

        source_spec_map: dict[str, list[Any]] = {}
        selected = set(only_stems) if only_stems is not None else None
        for spec in config.source_specs:
            source_spec_map.setdefault(spec.name, []).append(spec)

        bkg_stem: str | None = None
        needs_background = selected is None or any(stem.startswith("src_") for stem in selected)
        if needs_background:
            candidates = [pf for pf in sorted(parquet_dir.glob("src_bkg_*.l0.parquet")) if bkg_re.fullmatch(pf.name)]
            explicit = [pf for pf in candidates if selected is not None and pf.name.removesuffix(".l0.parquet") in selected]
            configured = [pf for pf in candidates if f"{float(bkg_re.fullmatch(pf.name).group('min')):g}" == f"{float(config.bkg_exposure_minutes):g}"]
            choices = explicit or configured or candidates
            if len(choices) > 1:
                raise ValueError("Multiple EC backgrounds found; select one background exposure before correction")
            if choices:
                bkg_stem = choices[0].name.removesuffix(".l0.parquet")

        bkg_df_corrected: pd.DataFrame | None = None
        if bkg_stem:
            bkg_path = parquet_dir / f"{bkg_stem}.l0.parquet"
            try:
                df_bkg = read_parquet(bkg_path)
                bkg_df_corrected = self._apply_correction(df_bkg, tb_params)
                acquisition = self._l0_acquisition(bkg_path)
                duration = float(bkg_re.fullmatch(bkg_path.name).group("min"))
                if f"{duration:g}" == f"{float(config.bkg_exposure_minutes):g}":
                    duration = config.bkg_exposure_minutes
                duration = acquisition.get("observation", {}).get("exposure_minutes", duration)
                bkg_out_path = self.layout.get_ec_l1_parquet(f"{bkg_stem}_corr.l1.parquet")
                write_parquet(bkg_out_path, bkg_df_corrected,
                              metadata={"tb_corrfile": str(tb_l2_json_path.name),
                                        "tb_params_used": tb_params_used,
                                        "exposure_minutes": duration,
                                        "acquisition": acquisition},
                              meta_key="tb_correction", schema_metadata={"acquisition": acquisition})
                results["outputs"].append({"stem": bkg_stem, "data_type": "source_bkg",
                                           "n_pulses": int(len(bkg_df_corrected)),
                                           "parquet": bkg_out_path.name, "acquisition": acquisition})
                results["n_processed"] += 1
                print(f"EC L1: bkg {bkg_stem}_corr ({len(bkg_df_corrected)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": str(bkg_path), "error": str(exc)})
                print(f"Error: bkg {bkg_stem} - {exc}")

        for parquet_file in sorted(parquet_dir.glob("*.l0.parquet")):
            if bkg_re.fullmatch(parquet_file.name):
                continue
            stem = parquet_file.stem.replace(".l0", "")
            if selected is not None and stem not in selected:
                continue

            src_match = src_re.match(parquet_file.name)
            if src_match:
                source_name = src_match.group("name")
                source_candidates = source_spec_map.get(source_name, [])
                spec = next((candidate for candidate in source_candidates
                             if f"{float(candidate.exposure_minutes):g}" == f"{float(src_match.group('min')):g}"), None)
                if spec is None and len(source_candidates) == 1:
                    spec = source_candidates[0]
                if spec is None:
                    results["errors"].append({"file": parquet_file.name, "error": f"Unknown source {source_name}; configure its peak energies before correction"})
                    print(f"Error: unknown source {source_name}, configure its peak energies")
                    continue
                data_type = "source"
            else:
                if parquet_file.name.startswith("src_"):
                    results["errors"].append({"file": parquet_file.name, "error": "Invalid canonical source filename"})
                    continue
                data_type = "xray"
                spec = None

            try:
                df_l0 = read_parquet(parquet_file)
                df_l1 = self._apply_correction(df_l0, tb_params)
                acquisition = self._l0_acquisition(parquet_file)

                file_metadata = {
                    "tb_corrfile": str(tb_l2_json_path.name),
                    "tb_params_used": tb_params_used,
                    "acquisition": acquisition,
                }
                if spec is not None:
                    file_metadata["source_name"] = spec.name
                    file_metadata["peak_energies_keV"] = list(spec.peak_energies_keV)
                    duration = float(src_match.group("min"))
                    if f"{duration:g}" == f"{float(spec.exposure_minutes):g}":
                        duration = spec.exposure_minutes
                    file_metadata["exposure_minutes"] = acquisition.get("observation", {}).get("exposure_minutes", duration)
                elif acquisition.get("observation", {}).get("energy_keV") is not None:
                    file_metadata["energy_keV"] = float(acquisition["observation"]["energy_keV"])

                parquet_out = self.layout.get_ec_l1_parquet(f"{stem}_corr.l1.parquet")
                write_parquet(parquet_out, df_l1, metadata=file_metadata, meta_key="tb_correction",
                              schema_metadata={"acquisition": acquisition})

                results["outputs"].append({
                    "stem": stem,
                    "data_type": data_type,
                    "n_pulses": int(len(df_l1)),
                    "parquet": parquet_out.name,
                    "acquisition": acquisition,
                })
                results["n_processed"] += 1
                print(f"EC L1: {stem} ({len(df_l1)} pulses, type={data_type})")
            except Exception as exc:
                results["errors"].append({"file": parquet_file.name, "error": str(exc)})
                print(f"Error: {parquet_file.name} - {exc}")

        write_json(self.layout.get_ec_l1_json("L1_manifest.json"), results)
        return results

    @staticmethod
    def _l0_acquisition(path: Path) -> dict[str, Any]:
        """Keep the exact decoder revision and L0 input behind corrected data."""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            **read_parquet_metadata(path, "acquisition"),
            "l0_file": path.name, "l0_sha256": digest.hexdigest(),
        }

    def _load_tb_params(self, tb_l2_json_path: Path) -> dict[int, dict[str, float]]:
        raw = read_json(tb_l2_json_path)
        if isinstance(raw, dict):
            rows = raw.get("fit_params", raw)
        else:
            rows = raw
        if not isinstance(rows, list):
            raise ValueError(f"Invalid TB L2 json format: {tb_l2_json_path}")

        out: dict[int, dict[str, float]] = {}
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            ch = int(row["channel"]) if "channel" in row else i
            G0 = float(row.get("G0", 0.0))
            if G0 == 0.0:
                continue
            out[ch] = {
                "G0": G0,
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
        params: dict[str, float],
    ) -> np.ndarray:
        k = params["k"]
        return (
            params["G0"]
            * (bias - k * temp - params["V0"]) ** 2
            * (-(temp**2) + params["b"] * temp + params["c"])
        )

    def _apply_correction(self, df: pd.DataFrame, tb_params: dict[int, dict[str, float]]) -> pd.DataFrame:
        out = df.copy()

        ch_arr = out["channel"].to_numpy(dtype=int)
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

        valid = np.isfinite(out["amp_corr"]) & (out["amp_corr"] > 0)
        out = out[valid].reset_index(drop=True)
        return out
