"""温度偏压(TB)标定 - L0层处理器。"""

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ...common.legacy_parser import parse_event_hk_to_dataframe
from ...common.utils import DataLayout, get_timestamp, write_json, write_parquet


class TBL0Processor:
    """TB L0层处理器"""

    def __init__(self, payload_name: str, product_root: Path):
        self.payload_name = payload_name
        self.layout = DataLayout(product_root, payload_name)
        self.root_dir = Path(product_root).resolve().parent
        self.tb_event_pattern = re.compile(r"^(.+?)[-_]\d+\.event\.dat$")
        self.tb_hk_pattern = re.compile(r"^(.+?)[-_]ecu_\d+\.hk$")

    def match_tb_pairs(self, raw_dir: Path) -> List[Tuple[Path, Path]]:
        """匹配 TB 原始文件对 (event.dat + hk)。"""
        hk_map: Dict[str, Path] = {}
        for hk in sorted(raw_dir.glob("*.hk")):
            match = self.tb_hk_pattern.match(hk.name)
            if match:
                hk_map[match.group(1)] = hk
        
        pairs: List[Tuple[Path, Path]] = []
        for event in sorted(raw_dir.glob("*.event.dat")):
            match = self.tb_event_pattern.match(event.name)
            if not match:
                continue
            prefix = match.group(1)
            hk = hk_map.get(prefix)
            if hk is not None:
                pairs.append((event, hk))

        return pairs

    def process(self, raw_dir: Path) -> Dict[str, Any]:
        """处理 TB L0。

        说明:
        1. 每个 parquet 行数据列为 amp/ch/utc/temp/bias。
        2. 原先独立 metadata.json 改为写入 parquet schema metadata。
        """
        pairs = self.match_tb_pairs(raw_dir)

        if not pairs:
            raise ValueError(f"No TB data files found in {raw_dir}")

        results = {
            "payload": self.payload_name,
            "layer": "TB_L0",
            "timestamp": get_timestamp(),
            "raw_dir": str(raw_dir),
            "n_files": len(pairs),
            "outputs": [],
            "metadata_in_parquet": True,
        }

        for event_path, hk_path in pairs:
            try:
                df_data, metadata = parse_event_hk_to_dataframe(
                    event_path=event_path,
                    hk_path=hk_path,
                    root_dir=self.root_dir,
                    event_tag="grid1x_ft_packet",
                    hk_tag="hk_packet",
                    include_per_point_tv=False,
                )

                stem = event_path.stem.replace(".event", "")

                parquet_path = self.layout.get_tb_l0_parquet(f"{stem}.l0.parquet")
                write_parquet(parquet_path, df_data, metadata=metadata)

                results["outputs"].append({
                    "stem": stem,
                    "parquet": str(parquet_path.name),
                    "n_events": int(len(df_data)),
                })

                print(f"✓ Processed: {stem}")

            except Exception as e:
                print(f"✗ Error processing {event_path}: {e}")
                continue

        manifest_path = self.layout.get_tb_l0_json("L0_manifest.json")
        write_json(manifest_path, results)

        return results
