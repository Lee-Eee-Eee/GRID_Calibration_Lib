"""能量标定(EC)处理 - L0层处理器。"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...common.legacy_parser import parse_event_hk_to_dataframe
from ...common.utils import DataLayout, get_timestamp, write_json, write_parquet
from ...config.payload_config import PayloadConfig


class ECL0Processor:
    """EC L0层处理器。"""

    def __init__(
        self, payload_name: str, product_root: Path,
        input_profile: dict[str, Any] | None = None,
        xml_paths: dict[str, Path] | None = None,
    ):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)
        self.root_dir = Path(product_root).resolve().parent
        self.input_profile = input_profile
        self.xml_paths = xml_paths
        self.xray_event_pattern = re.compile(r"^(?P<energy>\d+)-ch(?P<ch>\d).+\.event\.dat(\.cut)?$")
        self.xray_hk_pattern = re.compile(r"^(?P<energy>\d+)-ch(?P<ch>\d).*-ecu_.+\.hk(\.cut)?$")

    def match_xray_file_pairs(self, xray_dir: Path) -> list[tuple[Path, Path, str]]:
        hk_map: dict[str, Path] = {}
        for hk in sorted(xray_dir.glob("*.hk*")):
            m = self.xray_hk_pattern.match(hk.name)
            if m:
                key = f"{m.group('energy')}-ch{m.group('ch')}"
                if key not in hk_map or hk.suffix == ".cut":
                    hk_map[key] = hk

        event_map: dict[str, Path] = {}
        for event in sorted(xray_dir.glob("*.event.dat*")):
            m = self.xray_event_pattern.match(event.name)
            if not m:
                continue
            key = f"{m.group('energy')}-ch{m.group('ch')}"
            if key not in event_map or event.suffix == ".cut":
                event_map[key] = event

        pairs: list[tuple[Path, Path, str]] = []
        for key in sorted(set(event_map.keys()) & set(hk_map.keys())):
            event = event_map[key]
            m = self.xray_event_pattern.match(event.name)
            if m:
                pairs.append((event, hk_map[key], f"{m.group('energy')}keV"))
        return pairs

    def process(
        self,
        config: PayloadConfig,
        only_stems: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if self.input_profile is not None:
            return self._process_discovered(config, only_stems)
        results: dict[str, Any] = {
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
        if only_stems is not None:
            selected = set(only_stems)
            pairs = [(event, hk, energy) for event, hk, energy in pairs
                     if f"{energy}_{event.name.split('-')[1]}" in selected]
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
                write_parquet(parquet_path, df, schema_metadata={"acquisition": _metadata.get("acquisition", {})})

                results["outputs"].append({
                    "stem": stem,
                    "energy": energy_name,
                    "data_type": "xray",
                    "n_pulses": int(len(df)),
                    "parquet": parquet_path.name,
                    "acquisition": _metadata,
                })
                print(f"EC L0: xray {stem} ({len(df)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: {event_path.name} - {exc}")

        if config.source_specs and only_stems is None:
            self._process_sources(config, results)

        write_json(self.layout.get_ec_l0_json("L0_manifest.json"), results)
        return results

    def _process_discovered(
        self, config: PayloadConfig, only_stems: Sequence[str] | None,
    ) -> dict[str, Any]:
        """Route configured input pairs through the existing binary decoder.

        Discovery determines scientific labels; filenames are never reparsed here.
        Output names remain canonical so existing L1/L2 processors can read them.
        """
        from ...workbench.acquisition import discover

        preview = discover(config.raw_root, self.input_profile)
        if preview.get("errors") or preview.get("ambiguous"):
            raise ValueError(f"数据读取规则存在错误或歧义: {preview.get('errors', [])} {preview.get('ambiguous', [])}")
        rows = [dict(row) for row in preview["rows"] if row["kind"] in {"xray", "source", "background"}]
        for row in rows:
            if row["kind"] == "xray":
                row["output_stem"] = f"{float(row['energy_keV']):g}keV_ch{int(row['channel'])}"
            else:
                name = "bkg" if row["kind"] == "background" else row["source"]
                minutes = float(row["exposure_minutes"])
                row["output_stem"] = f"src_{name}_{minutes:g}m"
        selected = set(only_stems) if only_stems is not None else None
        if selected is not None:
            available = {row["output_stem"] for row in rows}
            missing = selected - available
            if missing:
                raise ValueError(f"读取规则中缺少匹配的 EC 工况: {', '.join(sorted(missing))}")
            needs_background = any(row["kind"] == "source" and row["output_stem"] in selected for row in rows)
            rows = [row for row in rows if row["output_stem"] in selected or (needs_background and row["kind"] == "background")]
        if not rows:
            raise ValueError(f"读取规则未匹配 EC 原始文件: {config.raw_root}")
        stems = [row["output_stem"] for row in rows]
        if len(set(stems)) != len(stems):
            raise ValueError("多个 EC 原始文件映射到同一输出名，请修改配对规则或清单以避免覆盖")
        bkg_names = [f"{row['output_stem']}.l0.parquet" for row in rows if row["kind"] == "background"]
        if len(bkg_names) > 1:
            raise ValueError("本次 EC 输入包含多个本底；请在读取清单中明确保留所用本底")
        result: dict[str, Any] = {
            "payload": self.payload_name, "layer": "EC_L0", "timestamp": get_timestamp(),
            "xray_dir": str(config.ec_xray_dir), "source_dir": str(config.ec_source_dir),
            "n_xray_files": sum(row["kind"] == "xray" for row in rows),
            "n_source_files": 0, "outputs": [], "errors": [],
        }
        for row in sorted(rows, key=lambda item: (item["kind"] != "background", item["output_stem"])):
            event_path, hk_path = Path(row["event"]), Path(row["hk"])
            stem = row["output_stem"]
            try:
                frame, metadata = parse_event_hk_to_dataframe(
                    event_path=event_path, hk_path=hk_path, root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet", hk_tag="hk_packet", include_per_point_tv=True,
                    input_profile=self.input_profile, xml_paths=self.xml_paths, observation=row,
                )
                parquet = self.layout.get_ec_l0_parquet(f"{stem}.l0.parquet")
                write_parquet(parquet, frame, schema_metadata={"acquisition": metadata.get("acquisition", {})})
                output = {
                    "stem": stem, "parquet": parquet.name, "n_pulses": int(len(frame)),
                    "data_type": {"xray": "xray", "source": "source", "background": "source_bkg"}[row["kind"]],
                    "acquisition": metadata,
                }
                if row["kind"] == "xray":
                    output["energy"] = f"{float(row['energy_keV']):g}keV"
                else:
                    output["exposure_minutes"] = row["exposure_minutes"]
                    if row["kind"] == "source":
                        output.update({"source_name": row["source"], "peak_energies_keV": row["peak_energies_keV"], "bkg_parquet": bkg_names[0] if bkg_names else None})
                        result["n_source_files"] += 1
                result["outputs"].append(output)
                print(f"EC L0: {row['kind']} {stem} ({len(frame)} pulses)")
            except Exception as exc:
                result["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: {event_path.name} - {exc}")
        write_json(self.layout.get_ec_l0_json("L0_manifest.json"), result)
        return result

    def _process_sources(
        self,
        config: PayloadConfig,
        results: dict[str, Any],
    ) -> None:
        source_dir = config.ec_source_dir

        bkg_event_path = source_dir / config.bkg_event_file
        bkg_hk_path = source_dir / config.bkg_hk_file

        bkg_parquet_name: str | None = None
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
                write_parquet(bkg_parquet_path, df_bkg, schema_metadata={"acquisition": _meta_bkg.get("acquisition", {})})
                bkg_parquet_name = bkg_parquet_path.name

                results["outputs"].append({
                    "stem": bkg_stem,
                    "data_type": "source_bkg",
                    "exposure_minutes": config.bkg_exposure_minutes,
                    "n_pulses": int(len(df_bkg)),
                    "parquet": bkg_parquet_name,
                    "acquisition": _meta_bkg,
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
                write_parquet(signal_parquet, df_signal, schema_metadata={"acquisition": _meta_signal.get("acquisition", {})})

                results["outputs"].append({
                    "stem": signal_stem,
                    "data_type": "source",
                    "source_name": spec.name,
                    "exposure_minutes": spec.exposure_minutes,
                    "peak_energies_keV": list(spec.peak_energies_keV),
                    "n_pulses": int(len(df_signal)),
                    "parquet": signal_parquet.name,
                    "bkg_parquet": bkg_parquet_name,
                    "acquisition": _meta_signal,
                })
                results["n_source_files"] += 1
                print(f"EC L0: source {signal_stem} ({len(df_signal)} pulses)")
            except Exception as exc:
                results["errors"].append({"file": event_path.name, "error": str(exc)})
                print(f"Error: source signal {event_path.name} - {exc}")
                continue
