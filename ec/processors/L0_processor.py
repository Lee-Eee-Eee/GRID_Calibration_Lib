"""能量标定(EC)处理 - L0层处理器。"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...common.legacy_parser import parse_event_hk_to_dataframe
from ...common.utils import DataLayout, get_timestamp, write_json, write_parquet
from ...config.payload_config import SourceSpec


class ECL0Processor:
    """EC L0层处理器。"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)
        self.root_dir = Path(product_root).resolve().parent
        self.xray_event_pattern = re.compile(r"^(?P<energy>\d+)-ch(?P<ch>\d).+\.event\.dat(\.cut)?$")
        self.xray_hk_pattern = re.compile(r"^(?P<energy>\d+)-ch(?P<ch>\d).*-ecu_.+\.hk(\.cut)?$")

    def match_xray_file_pairs(self, xray_dir: Path) -> List[Tuple[Path, Path, str]]:
        hk_map: Dict[str, Path] = {}
        for hk in sorted(xray_dir.glob("*.hk*")):
            m = self.xray_hk_pattern.match(hk.name)
            if m:
                key = f"{m.group('energy')}-ch{m.group('ch')}"
                hk_map[key] = hk

        pairs: List[Tuple[Path, Path, str]] = []
        for event in sorted(xray_dir.glob("*.event.dat*")):
            m = self.xray_event_pattern.match(event.name)
            if not m:
                continue
            key = f"{m.group('energy')}-ch{m.group('ch')}"
            if key in hk_map:
                pairs.append((event, hk_map[key], f"{m.group('energy')}keV"))
        return pairs

    def process(
        self,
        xray_dir: Path,
        source_dir: Optional[Path] = None,
        source_specs: Optional[Sequence[SourceSpec]] = None,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L0",
            "timestamp": get_timestamp(),
            "xray_dir": str(xray_dir),
            "source_dir": str(source_dir) if source_dir else None,
            "n_xray_files": 0,
            "n_source_files": 0,
            "outputs": [],
            "errors": [],
        }

        # Process xray files
        pairs = self.match_xray_file_pairs(xray_dir)
        results["n_xray_files"] = len(pairs)

        for event_path, hk_path, energy_name in pairs:
            try:
                df, metadata = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=True,
                )

                stem = f"{energy_name}_{event_path.name.split('-')[1]}"
                parquet_path = self.layout.get_ec_l0_parquet(f"{stem}.parquet")
                metadata["energy_keV"] = int(energy_name.replace("keV", ""))
                metadata["data_type"] = "xray"
                metadata["columns"] = list(df.columns)
                write_parquet(parquet_path, df, metadata=metadata)

                results["outputs"].append({
                    "stem": stem,
                    "energy": energy_name,
                    "data_type": "xray",
                    "n_pulses": int(len(df)),
                    "parquet": parquet_path.name,
                })
                print(f"EC L0: xray {stem} ({len(df)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: {event_path.name} - {exc}")

        # Process source files (Na22, Cs137, Co60 + backgrounds)
        if source_dir and source_specs:
            self._process_sources(source_dir, source_specs, results)

        write_json(self.layout.get_ec_l0_json("L0_manifest.json"), results)
        return results

    def _process_sources(
        self,
        source_dir: Path,
        source_specs: Sequence[SourceSpec],
        results: Dict[str, Any],
    ) -> None:
        """Process source calibration files (Na22, Cs137, Co60) with dedicated backgrounds."""
        processed_bkg: Dict[str, str] = {}  # bkg_event_name -> parquet_name

        for spec in source_specs:
            energy_tag = f"src_{int(spec.energy_keV)}keV"
            event_path = source_dir / spec.event_file
            hk_path = source_dir / spec.hk_file
            bkg_event_path = source_dir / spec.background_event_file
            bkg_hk_path = source_dir / spec.background_hk_file

            # Check files exist
            for p in (event_path, hk_path, bkg_event_path, bkg_hk_path):
                if not p.exists():
                    results["errors"].append({"file": str(p), "error": "File not found"})
                    print(f"Error: source file not found: {p}")
                    continue

            # Process signal
            try:
                df_signal, meta_signal = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=True,
                )
                signal_stem = f"{energy_tag}_{spec.name}"
                signal_parquet = self.layout.get_ec_l0_parquet(f"{signal_stem}.parquet")
                meta_signal["energy_keV"] = spec.energy_keV
                meta_signal["data_type"] = "source"
                meta_signal["source_name"] = spec.name
                meta_signal["columns"] = list(df_signal.columns)
                write_parquet(signal_parquet, df_signal, metadata=meta_signal)

                results["outputs"].append({
                    "stem": signal_stem,
                    "energy": f"{spec.energy_keV}keV",
                    "data_type": "source",
                    "source_name": spec.name,
                    "n_pulses": int(len(df_signal)),
                    "parquet": signal_parquet.name,
                })
                results["n_source_files"] += 1
                print(f"EC L0: source {signal_stem} ({len(df_signal)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: source signal {event_path.name} - {exc}")
                continue

            # Process background (shared across sources, only process once)
            bkg_key = spec.background_event_file
            if bkg_key not in processed_bkg:
                try:
                    df_bkg, meta_bkg = parse_event_hk_to_dataframe(
                        event_path=bkg_event_path,
                        hk_path=bkg_hk_path,
                        root_dir=self.root_dir,
                        event_tag="grid1x_ft_packet",
                        hk_tag="hk_packet",
                        include_per_point_tv=True,
                    )
                    bkg_stem = f"src_bkg_{bkg_event_path.stem}"
                    bkg_parquet = self.layout.get_ec_l0_parquet(f"{bkg_stem}.parquet")
                    meta_bkg["data_type"] = "source_bkg"
                    meta_bkg["columns"] = list(df_bkg.columns)
                    write_parquet(bkg_parquet, df_bkg, metadata=meta_bkg)
                    processed_bkg[bkg_key] = bkg_parquet.name

                    results["outputs"].append({
                        "stem": bkg_stem,
                        "data_type": "source_bkg",
                        "n_pulses": int(len(df_bkg)),
                        "parquet": bkg_parquet.name,
                    })
                    print(f"EC L0: source bkg {bkg_stem} ({len(df_bkg)} pulses)")
                except Exception as exc:
                    results["errors"].append({"file": bkg_event_path.name, "error": str(exc)})
                    print(f"Error: source bkg {bkg_event_path.name} - {exc}")
