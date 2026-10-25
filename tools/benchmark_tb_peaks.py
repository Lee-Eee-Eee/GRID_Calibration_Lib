"""Repeat TB spectral fitting from an original-count histogram audit cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

import numpy as np
from calibration_lib.common.tb_peak_context import gain_guides
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.workbench.service import clean


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--channels", default="0,1,2")
    parser.add_argument("--settings", default="{}", help="JSON peak_detection settings")
    args = parser.parse_args()
    cache = np.load(args.cache)
    settings = json.loads(args.settings)
    processor = TBL1Processor("study", args.output.parent / "products")
    rows, start = [], perf_counter()
    stems = cache["stems"].tolist()
    channels = [int(ch) for ch in args.channels.split(",")]
    if not channels or any(channel not in range(4) for channel in channels):
        parser.error("--channels must contain channel numbers 0–3")
    context = {ch: gain_guides(cache["x"], cache["counts"][:, ch],
                              cache["temp"][:, ch], cache["bias"][:, ch], settings)
               if settings.get("expected_center") is None else [None] * len(stems) for ch in channels}
    print({ch: sum(item is not None for item in guides) for ch, guides in context.items()}, flush=True)
    for index, stem in enumerate(stems):
        if args.limit and index >= args.limit:
            break
        for channel in channels:
            started = perf_counter()
            try:
                guide = context[channel][index]
                fit = processor._fit_histogram(stem, channel, cache["x"], cache["counts"][index, channel],
                                               {}, True, {**settings, **({key: guide[key] for key in
                                               ("expected_center", "expected_tolerance")} if guide else {})})
                row = {key: getattr(fit, key) for key in (
                    "center", "center_err", "sigma", "sigma_err", "fwhm", "fit_mode", "rsquared",
                    "lo", "hi", "peak_detection", "fit_quality",
                )}
            except (RuntimeError, ValueError) as exc:
                row = {"error": str(exc), "peak_detection": getattr(exc, "diagnostics", {})}
            row.update(stem=stem, channel=channel, file_index=index,
                       gain_guide=context[channel][index],
                       temp=float(cache["temp"][index, channel]), bias=float(cache["bias"][index, channel]),
                       elapsed_seconds=perf_counter() - started)
            rows.append(row)
            print(f"{index+1}/{len(stems)} {stem} CH{channel}: "
                  f"{row.get('fit_quality', {}).get('accepted', False)} {row.get('center', row.get('error'))}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(clean({"settings": settings, "rows": rows,
                                               "elapsed_seconds": perf_counter()-start}), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
