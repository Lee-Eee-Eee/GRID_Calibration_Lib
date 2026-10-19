"""温度偏压(TB)标定 - L0层处理器。"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ...common.legacy_parser import parse_event_hk_to_dataframe, to_hk_data
from ...common.utils import DataLayout, get_timestamp, write_json, write_parquet


class TBL0Processor:
    """TB L0层处理器"""

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
        self.raw_root: Path | None = None
        self.tb_event_pattern = re.compile(r"^(.+?)[-_]\d+\.event\.dat$")
        self.tb_hk_pattern = re.compile(r"^(.+?)[-_]ecu_\d+\.hk$")

    def match_tb_pairs(self, raw_dir: Path) -> list[tuple[Path, Path]]:
        """匹配 TB 原始文件对 (event.dat + hk)。"""
        hk_map: dict[str, Path] = {}
        for hk in sorted(raw_dir.glob("*.hk")):
            match = self.tb_hk_pattern.match(hk.name)
            if match:
                hk_map[match.group(1)] = hk
        
        pairs: list[tuple[Path, Path]] = []
        for event in sorted(raw_dir.glob("*.event.dat")):
            match = self.tb_event_pattern.match(event.name)
            if not match:
                continue
            prefix = match.group(1)
            hk = hk_map.get(prefix)
            if hk is not None:
                pairs.append((event, hk))

        return pairs

    def process(self, raw_dir: Path, only_stems: Iterable[str] | None = None) -> dict[str, Any]:
        """处理 TB L0。

        说明:
        1. 每个 parquet 行数据列为 amp/ch/utc/temp/bias。
        2. 原先独立 metadata.json 改为写入 parquet schema metadata。
        """
        observations: dict[str, dict[str, Any]] = {}
        if self.input_profile is not None:
            from ...workbench.acquisition import discover

            preview = discover(self.raw_root or raw_dir.parent, self.input_profile)
            if preview.get("errors") or preview.get("ambiguous"):
                raise ValueError(f"数据读取规则存在错误或歧义: {preview.get('errors', [])} {preview.get('ambiguous', [])}")
            rows = [row for row in preview["rows"] if row["kind"] == "tb"]
            pairs = [(Path(row["event"]), Path(row["hk"])) for row in rows]
            observations = {str(Path(row["event"])): row for row in rows}
        else:
            pairs = self.match_tb_pairs(raw_dir)
        if only_stems is not None:
            selected = set(only_stems)
            pairs = [(event, hk) for event, hk in pairs
                     if observations.get(str(event), {}).get("stem", event.name.removesuffix(".event.dat")) in selected]

        if not pairs:
            raise ValueError(f"No TB data files found in {raw_dir}")

        results = {
            "payload": self.payload_name,
            "layer": "TB_L0",
            "timestamp": get_timestamp(),
            "raw_dir": str(raw_dir),
            "n_files": len(pairs),
            "outputs": [],
            "errors": [],
            "metadata_in_parquet": True,
        }

        for event_path, hk_path in pairs:
            try:
                observation = observations.get(str(event_path), {})
                options: dict[str, Any] = {}
                if self.input_profile is not None:
                    options = {"input_profile": self.input_profile, "xml_paths": self.xml_paths, "observation": observation}
                df_data, metadata = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=False,
                    **options,
                )

                stem = observation.get("stem") or event_path.stem.replace(".event", "")

                hk_data = to_hk_data(metadata["channels"])
                parquet_path = self.layout.get_tb_l0_parquet(f"{stem}.l0.parquet")
                write_parquet(
                    parquet_path, df_data, metadata=hk_data, meta_key="hk_data",
                    schema_metadata={"acquisition": metadata.get("acquisition", {})},
                )

                results["outputs"].append({
                    "stem": stem,
                    "parquet": str(parquet_path.name),
                    "n_events": int(len(df_data)),
                    "acquisition": metadata,
                })

                print(f"✓ Processed: {stem}")

            except Exception as e:
                results["errors"].append({"file": event_path.name, "error": str(e)})
                print(f"✗ Error processing {event_path}: {e}")
                continue

        manifest_path = self.layout.get_tb_l0_json("L0_manifest.json")
        write_json(manifest_path, results)

        return results
