"""Raw spectrum previews must not turn corrupt packet bytes into pulse tails."""

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import write_parquet
from calibration_lib.workbench.compute import ComputeAdapter


@pytest.mark.parametrize("layer", ["TB_L0", "EC_L0", "EC_L1"])
def test_raw_and_corrected_previews_exclude_crc_failed_pulses(tmp_path, layer):
    root = tmp_path / "products"
    field = "amp_corr" if layer == "EC_L1" else "amp"
    stem = "condition" if layer == "TB_L0" else "40keV_ch0"
    filename = f"{stem}_corr.l1.parquet" if layer == "EC_L1" else f"{stem}.l0.parquet"
    path = root / "14B" / layer[:2].lower() / layer[-2:] / "parquet" / filename
    write_parquet(path, pd.DataFrame({"channel": [0, 0, 0, 1], field: [12., 14., 800., 1000.],
                                      "crc_check": [True, True, False, False]}))
    adapter = ComputeAdapter(tmp_path)
    if layer == "TB_L0":
        result = adapter.spectrum("14B", root, stem, 0, layer)
    else:
        result = adapter.ec_spectrum("14B", root, layer, 0, stem=stem)
    assert result["n_events"] == 2
    assert sum(result["y"]) == 2
    assert result["input_quality"] == {"crc_available": True, "input_events": 4,
                                        "used_events": 2, "excluded_crc_count": 2}
    assert not np.asarray(result["y"])[np.asarray(result["x"]) >= 800].any()


@pytest.mark.parametrize("layer", ["TB_L0", "EC_L0", "EC_L1"])
def test_all_crc_failed_preview_requests_reparse_instead_of_showing_false_tail(tmp_path, layer):
    root = tmp_path / "products"
    field = "amp_corr" if layer == "EC_L1" else "amp"
    stem = "condition" if layer == "TB_L0" else "40keV_ch0"
    filename = f"{stem}_corr.l1.parquet" if layer == "EC_L1" else f"{stem}.l0.parquet"
    path = root / "14B" / layer[:2].lower() / layer[-2:] / "parquet" / filename
    write_parquet(path, pd.DataFrame({"channel": [0, 0], field: [900., 9000.], "crc_check": False}))
    adapter = ComputeAdapter(tmp_path)
    with pytest.raises(ValueError, match="CRC.*重新解析 L0"):
        if layer == "TB_L0":
            adapter.spectrum("14B", root, stem, 0, layer)
        else:
            adapter.ec_spectrum("14B", root, layer, 0, stem=stem)
