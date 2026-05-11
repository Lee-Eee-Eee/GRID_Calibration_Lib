"""能量标定(EC)处理 - L0层处理器。"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...common.legacy_parser import parse_event_hk_to_dataframe
from ...common.utils import DataLayout, get_timestamp, write_json, write_parquet
from ...config.payload_config import PayloadConfig, SourceSpec


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
                if key not in hk_map or hk.suffix == ".cut":
                    hk_map[key] = hk

        event_map: Dict[str, Path] = {}
        for event in sorted(xray_dir.glob("*.event.dat*")):
            m = self.xray_event_pattern.match(event.name)
            if not m:
                continue
            key = f"{m.group('energy')}-ch{m.group('ch')}"
            if key not in event_map or event.suffix == ".cut":
                event_map[key] = event

        pairs: List[Tuple[Path, Path, str]] = []
        for key in sorted(set(event_map.keys()) & set(hk_map.keys())):
            event = event_map[key]
            m = self.xray_event_pattern.match(event.name)
            if m:
                pairs.append((event, hk_map[key], f"{m.group('energy')}keV"))
        return pairs

    def process(
        self,
        config: PayloadConfig,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "EC_L0",
            "timestamp": get_timestamp(),
            "xray_dir": str(config.ec_xray_dir),
            "source_dir": str(config.ec_source_dir),
            "n_xray_files": 0,
            "n_source_files": 0,
            "outputs": [],
            "errors": [],
        }

        pairs = self.match_xray_file_pairs(config.ec_xray_dir)
        results["n_xray_files"] = len(pairs)

        for event_path, hk_path, energy_name in pairs:
            try:
                df, _metadata = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=True,
                )

                stem = f"{energy_name}_{event_path.name.split('-')[1]}"
                parquet_path = self.layout.get_ec_l0_parquet(f"{stem}.l0.parquet")
                write_parquet(parquet_path, df)

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

        if config.source_specs:
            self._process_sources(config, results)

        write_json(self.layout.get_ec_l0_json("L0_manifest.json"), results)
        return results

    def _process_sources(
        self,
        config: PayloadConfig,
        results: Dict[str, Any],
    ) -> None:
        source_dir = config.ec_source_dir

        bkg_event_path = source_dir / config.bkg_event_file
        bkg_hk_path = source_dir / config.bkg_hk_file

        bkg_parquet_name: Optional[str] = None
        if bkg_event_path.exists() and bkg_hk_path.exists():
            try:
                df_bkg, _meta_bkg = parse_event_hk_to_dataframe(
                    event_path=bkg_event_path,
                    hk_path=bkg_hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=True,
                )
                bkg_stem = f"src_bkg_{config.bkg_exposure_minutes}m"
                bkg_parquet_path = self.layout.get_ec_l0_parquet(f"{bkg_stem}.l0.parquet")
                write_parquet(bkg_parquet_path, df_bkg)
                bkg_parquet_name = bkg_parquet_path.name

                results["outputs"].append({
                    "stem": bkg_stem,
                    "data_type": "source_bkg",
                    "exposure_minutes": config.bkg_exposure_minutes,
                    "n_pulses": int(len(df_bkg)),
                    "parquet": bkg_parquet_name,
                })
                print(f"EC L0: bkg {bkg_stem} ({len(df_bkg)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": config.bkg_event_file, "error": str(exc)})
                print(f"Error: bkg {config.bkg_event_file} - {exc}")
        else:
            print(f"Warning: bkg files not found ({config.bkg_event_file}, {config.bkg_hk_file})")

        for spec in config.source_specs:
            event_path = source_dir / spec.event_file
            hk_path = source_dir / spec.hk_file

            if not event_path.exists() or not hk_path.exists():
                results["errors"].append({"file": str(event_path), "error": "File not found"})
                print(f"Error: source file not found: {event_path} or {hk_path}")
                continue

            try:
                df_signal, _meta_signal = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=True,
                )
                signal_stem = f"src_{spec.name}_{spec.exposure_minutes}m"
                signal_parquet = self.layout.get_ec_l0_parquet(f"{signal_stem}.l0.parquet")
                write_parquet(signal_parquet, df_signal)

                results["outputs"].append({
                    "stem": signal_stem,
                    "data_type": "source",
                    "source_name": spec.name,
                    "exposure_minutes": spec.exposure_minutes,
                    "peak_energies_keV": list(spec.peak_energies_keV),
                    "n_pulses": int(len(df_signal)),
                    "parquet": signal_parquet.name,
                    "bkg_parquet": bkg_parquet_name,
                })
                results["n_source_files"] += 1
                print(f"EC L0: source {signal_stem} ({len(df_signal)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: source signal {event_path.name} - {exc}")
                continue