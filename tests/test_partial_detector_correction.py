"""The supported EC channels can use TB correction without a plastic model."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import (
    read_json,
    read_parquet,
    read_parquet_metadata,
    write_json,
    write_parquet,
)
from calibration_lib.config.payload_config import SourceSpec, get_payload_config
from calibration_lib.ec.processors.L1_processor import ECL1Processor

REASON = "塑料闪烁体 · 康普顿响应，拟合方案待定"
PARAMS = {"G0": .001, "k": .0184, "V0": 24.11, "b": 54.31, "c": 23459.83}


def tb_package(tmp_path, *, placeholder=False):
    path = tmp_path / "selected-TB.l2.json"
    records = [{"channel": ch, **PARAMS} for ch in range(3)]
    if placeholder:
        records.append({"channel": 3, "G0": 0., "k": 0., "V0": 0., "b": 0., "c": 0.})
    write_json(path, records)
    return path


def input_events():
    return pd.DataFrame({
        "channel": np.repeat(np.arange(4), 3),
        "amp": np.tile([100., 200., 300.], 4),
        "temp": np.tile([20., 25., 30.], 4),
        "bias": np.tile([28., 28.5, 29.], 4),
        "utc": np.tile([0., 30., 60.], 4), "crc_check": True,
    })


@pytest.mark.parametrize("payload,response_payload", [("10B", None), ("11B", None), ("TEST-10", "10B")])
@pytest.mark.parametrize("placeholder", [False, True])
def test_partial_detector_corrects_supported_source_background_and_xray_channels(
    tmp_path, payload, response_payload, placeholder
):
    products = tmp_path / "products"
    frame = input_events()
    stems = ["src_Na22_5m", "src_bkg_30m", "40keV_ch0"]
    for stem in stems:
        write_parquet(products / payload / f"ec/L0/parquet/{stem}.l0.parquet", frame)
    cfg = replace(get_payload_config("14B"), source_specs=[SourceSpec("Na22", "", "", 5, [511.])])
    dependency = tb_package(tmp_path, placeholder=placeholder)
    processor = ECL1Processor(payload, products, response_payload=response_payload)
    assert set(processor._load_tb_params(dependency)) == {0, 1, 2}
    result = processor.process(cfg, dependency)
    assert not result["errors"] and result["n_processed"] == 3
    assert result["skipped_channels"] == [{"channel": 3, "reason": REASON}]
    assert {row["stem"] for row in result["outputs"]} == set(stems)
    assert all(row["n_pulses"] == 9 for row in result["outputs"])
    original = frame.loc[frame.channel < 3].reset_index(drop=True)
    p = PARAMS
    reference_gain = p["G0"] * (28.5 - p["k"] * 25. - p["V0"]) ** 2 * (-25. ** 2 + p["b"] * 25. + p["c"])
    observed_gain = p["G0"] * (original.bias - p["k"] * original.temp - p["V0"]) ** 2 * (-original.temp ** 2 + p["b"] * original.temp + p["c"])
    expected = original.amp * reference_gain / observed_gain
    for stem in stems:
        path = products / payload / f"ec/L1/parquet/{stem}_corr.l1.parquet"
        corrected = read_parquet(path)
        assert corrected.channel.tolist() == original.channel.tolist()
        np.testing.assert_allclose(corrected.amp_corr, expected)
        np.testing.assert_allclose(corrected.amp, original.amp)
        metadata = read_parquet_metadata(path, "tb_correction")
        assert set(metadata["tb_params_used"]) == {"0", "1", "2"}
        assert metadata["tb_corrfile"] == dependency.name
    manifest = read_json(products / payload / "ec/L1/json/L1_manifest.json")
    assert manifest["skipped_channels"] == result["skipped_channels"]


@pytest.mark.parametrize("payload", ["14B", "15B", "CUSTOM"])
def test_generic_detector_still_requires_all_four_tb_channels(tmp_path, payload):
    with pytest.raises(ValueError, match="Incomplete TB parameters"):
        ECL1Processor(payload, tmp_path)._load_tb_params(tb_package(tmp_path))


def test_plastic_correction_never_uses_old_ch3_tb_coefficients(tmp_path):
    dependency = tb_package(tmp_path)
    # The deferred channel is ignored before even interpreting its old fields.
    records = read_json(dependency) + [{"channel": 3, "G0": "invalid old model"}]
    write_json(dependency, records)
    processor = ECL1Processor("TEST-10", tmp_path, response_payload="10B")
    params = processor._load_tb_params(dependency)
    assert set(params) == {0, 1, 2}
    corrected = processor._apply_correction(input_events(), {**params, 3: {"G0": float("nan")}})
    assert set(corrected.channel) == {0, 1, 2}
    assert np.isfinite(corrected.amp_corr).all()


def test_supported_channel_is_still_required_for_plastic_detector(tmp_path):
    dependency = tb_package(tmp_path)
    write_json(dependency, read_json(dependency)[:2])
    with pytest.raises(ValueError, match="Incomplete TB parameters"):
        ECL1Processor("10B", tmp_path)._load_tb_params(dependency)
