"""N5 official observations and explicit processing capability restrictions."""

import json

import pytest
from calibration_lib.workbench.acquisition import validate_profile
from calibration_lib.workbench.acquisition_store import prepare_profile
from calibration_lib.workbench.n5_preset import build_n5_profile


@pytest.fixture
def n5_archive(tmp_path):
    directory = tmp_path / "温度偏压/N5"
    directory.mkdir(parents=True)
    for temp in (-20, -10, 0, 10, 20, 30):
        for bias in (26.5, 27, 27.5, 28, 28.3, 28.5, 28.7, 29):
            base = f"T{temp}_B{bias}_Am241_N5_run"
            metadata = {"official": True, "payload": "N5", "temp": temp, "bias": bias, "base": base}
            (directory / f"{base}_manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
            (directory / f"{base}_sci_001.event.dat").write_bytes(b"event")
            (directory / f"{base}_hk_ecu_002.hk").write_bytes(b"hk")
    extra = {"official": False, "payload": "N5", "temp": 40, "bias": 30, "base": "unused"}
    (directory / "test_manifest.json").write_text(json.dumps(extra), encoding="utf-8")
    xray = tmp_path / "x/N5"
    xray.mkdir(parents=True)
    for channel in range(4):
        base = f"X20keV_CH{channel}_N5_run"
        (xray / f"{base}_sci_003.event.dat").write_bytes(b"event")
        (xray / f"{base}_hk_ecu_004.hk").write_bytes(b"hk")
    return tmp_path


def test_n5_profile_indexes_only_official_48_and_all_unique_xray_pairs(n5_archive):
    profile, summary = build_n5_profile(n5_archive)
    assert summary["counts"] == {"tb": 48, "xray": 4}
    assert profile["parser"]["conditions"]["mode"] == "hk"
    assert profile["parser"]["transform"]["bias_mode"] == "affine"
    assert profile["processing_capabilities"]["EC_L2"]["supported"] is False
    assert "同物理通道" in profile["processing_capabilities"]["EC_L2"]["reason"]
    assert profile["discovery"]["sources"][0]["energies_keV"] == [59.54]
    tb = [row for row in profile["discovery"]["manifest"] if row["kind"] == "tb"]
    assert len({(row["temp"], row["bias"]) for row in tb}) == 48
    assert all(row["energy_keV"] == 59.54 for row in tb)
    assert all(not row["event"].startswith(str(n5_archive)) for row in tb)
    prepared = prepare_profile(n5_archive, profile)
    assert '<hk_packet packet_len="182"' in prepared["xml"]["hk"]["text"]
    assert prepared["parser"]["event_multi_evt"] == 38
    assert prepared["fitting"]["tb_histogram"]["max"] == 65536


def test_missing_official_condition_is_reported(n5_archive):
    next((n5_archive / "温度偏压/N5").glob("T-20_B26.5*_manifest.json")).unlink()
    with pytest.raises(ValueError, match="48.*缺失"):
        build_n5_profile(n5_archive)


def test_multiple_xray_exposures_require_explicit_selection(n5_archive):
    directory = n5_archive / "x/N5"
    (directory / "X20keV_CH0_N5_repeat_sci_005.event.dat").write_bytes(b"event")
    (directory / "X20keV_CH0_N5_repeat_hk_ecu_006.hk").write_bytes(b"hk")
    with pytest.raises(ValueError, match="多次采集"):
        build_n5_profile(n5_archive)


@pytest.mark.parametrize("capability", [{"supported": 0}, {"supported": False},
                                       {"supported": False, "reason": ""}, "invalid"])
def test_capability_restrictions_require_boolean_and_actionable_reason(capability):
    with pytest.raises(ValueError):
        validate_profile({"processing_capabilities": {"EC_L2": capability}})


def test_unknown_future_layer_does_not_invent_restrictions_for_other_layers():
    profile = validate_profile({"processing_capabilities": {"FUTURE": {"supported": True}}})
    assert "EC_L2" not in profile["processing_capabilities"]
