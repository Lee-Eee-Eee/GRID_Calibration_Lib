"""Editing one EC peak never refits another channel or source line."""

import json

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor


def old_record(channel, energy):
    return {"channel": channel, "E": float(energy), "center": 111. + channel,
            "fit_quality": {"accepted": True}, "signal_sha256": "previous-input",
            "signal_acquisition": {"revision": 7}, "review_note": f"keep CH{channel}/{energy}",
            "model_parameters": {"g_center": {"value": 111. + channel}}}


def record_bytes(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def source_fixture(tmp_path, monkeypatch, *, payload="Test", response_payload=None):
    root = tmp_path / "products"
    directory = root / payload / "ec/L1/parquet"
    rng = np.random.default_rng(184)
    signal, background = [], []
    for channel in range(4):
        amp = np.r_[rng.normal(300., 9., 2200), rng.normal(610., 12., 2800), rng.uniform(50, 750, 500)]
        signal.append(pd.DataFrame({"channel": channel, "amp_corr": amp,
                                    "utc": np.linspace(0, 120, len(amp)), "crc_check": True}))
        background.append(pd.DataFrame({"channel": channel, "amp_corr": rng.uniform(50, 750, 500),
                                        "utc": np.linspace(0, 120, 500), "crc_check": True}))
    write_parquet(directory / "src_Test_2m_corr.l1.parquet", pd.concat(signal),
                  metadata={"peak_energies_keV": [60., 122.], "exposure_minutes": 2}, meta_key="tb_correction")
    write_parquet(directory / "src_bkg_2m_corr.l1.parquet", pd.concat(background))
    path = root / payload / "ec/L2/json/src_Test_fit_results.l2.json"
    records = [old_record(channel, energy) for channel in range(4) for energy in (60., 122.)]
    write_json(path, records)
    processor = ECL2Processor(payload, root, response_payload=response_payload)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *_: None)
    return processor, root / payload / "ec/L1", path, records


@pytest.mark.parametrize("automatic", [False, True])
def test_source_single_point_fits_only_the_selected_channel_and_line(tmp_path, monkeypatch, automatic):
    processor, inputs, path, before = source_fixture(tmp_path, monkeypatch)
    point = "Test:ec:src_Test:1:122"
    original_fit = processor._fit_energy_spectrum
    calls = []

    def capture(**kwargs):
        calls.append((kwargs["channel"], kwargs["forced_peak_energy"]))
        return original_fit(**kwargs)

    monkeypatch.setattr(processor, "_fit_energy_spectrum", capture)
    result = processor.process(inputs, {
        "only_point": point, "automatic_window": automatic,
        "peak_detection": {"expected_center": 610., "expected_energy_keV": 122., "infer_noise": False},
        "ec_windows": {point: {"lo": 555.125, "hi": 665.875},
                       "Test:ec:src_Test:0:60": {"lo": 0, "hi": 0},
                       "Test:ec:src_Test:1:60": {"lo": 0, "hi": 0},
                       "Test:ec:src_Test:2:122": {"lo": 0, "hi": 0}},
    })
    assert not result["errors"], result["errors"]
    assert calls == [(1, 122.)]
    assert result["outputs"][0]["points_updated"] == [point]
    assert result["outputs"][0]["channels_updated"] == [1]
    after = read_json(path)
    assert len(after) == len(before)
    for old, current in zip(before, after, strict=True):
        if (old["channel"], old["E"]) == (1, 122.):
            assert current["center"] == pytest.approx(610., abs=2)
            assert current["window_manual"] is (not automatic)
            assert current["window_source"] == ("auto" if automatic else "manual")
            if not automatic:
                assert (current["lo"], current["hi"]) == (555.125, 665.875)
        else:
            assert record_bytes(current) == record_bytes(old)


def test_xray_single_point_reads_other_channel_background_without_fitting_it(tmp_path, monkeypatch):
    root = tmp_path / "products"
    rng = np.random.default_rng(602)
    for channel in (0, 2):
        amp = np.r_[rng.normal(350. + 100. * channel, 16., 3000), rng.uniform(100, 850, 300)]
        frame = pd.DataFrame({"channel": channel, "amp_corr": amp,
                              "utc": np.linspace(0, 120, len(amp)), "crc_check": True})
        write_parquet(root / f"Test/ec/L1/parquet/42.5keV_ch{channel}_corr.l1.parquet", frame,
                      metadata={"energy_keV": 42.5}, meta_key="tb_correction")
    # An unrelated broken source must not affect the selected X-ray spectrum.
    (root / "Test/ec/L1/parquet/src_other_2m_corr.l1.parquet").write_bytes(b"unrelated")
    path = root / "Test/ec/L2/json/42.5keV_fit_results.l2.json"
    before = [old_record(channel, 42.5) for channel in range(4)]
    write_json(path, before)
    processor = ECL2Processor("Test", root)
    original_fit = processor._fit_energy_spectrum
    calls = []

    def capture(**kwargs):
        calls.append((kwargs["channel"], kwargs["background_channel"]))
        return original_fit(**kwargs)

    monkeypatch.setattr(processor, "_fit_energy_spectrum", capture)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *_: None)
    monkeypatch.setattr(processor, "_xray_consensus_center", lambda *_: pytest.fail("A single point cannot alter other channels"))
    point = "Test:ec:42.5keV:2:42.5"
    result = processor.process(root / "Test/ec/L1", {
        "only_point": point, "ec_windows": {point: {"lo": 490.125, "hi": 610.875},
                                              "Test:ec:42.5keV:0:42.5": {"lo": 0, "hi": 0}},
        "peak_detection": {"infer_noise": False},
    })
    assert not result["errors"], result["errors"]
    assert calls == [(2, 0)]
    assert result["outputs"][0]["points_updated"] == [point]
    assert result["outputs"][0]["channels_updated"] == [2]
    after = read_json(path)
    assert after[2]["center"] == pytest.approx(550., abs=3)
    assert (after[2]["lo"], after[2]["hi"]) == (490.125, 610.875)
    assert after[2]["background_file"] == "42.5keV_ch0_corr.l1.parquet"
    assert all(record_bytes(after[ch]) == record_bytes(before[ch]) for ch in (0, 1, 3))


def test_failed_selected_fit_does_not_publish_preserved_old_record_as_a_new_success(tmp_path, monkeypatch):
    processor, inputs, path, _ = source_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    point = "Test:ec:src_Test:1:122"
    result = processor.process(inputs, {"only_point": point,
                                       "ec_windows": {point: {"lo": 1200, "hi": 1300}}})
    assert result["errors"] and result["outputs"] == [] and result["n_processed"] == 0
    assert any(error.get("channel") == 1 and error.get("peak_keV") == 122. for error in result["errors"])
    assert path.read_bytes() == before


def test_single_point_keeps_deferred_placeholders_without_evaluating_them(tmp_path, monkeypatch):
    processor, inputs, path, before = source_fixture(tmp_path, monkeypatch, payload="TEST-10", response_payload="10B")
    for record in before:
        if record["channel"] == 3:
            record.update(center=0., placeholder=True, fit_performed=False,
                          fit_quality={"accepted": False, "status": "model_pending"})
    write_json(path, before)
    original_fit = processor._fit_energy_spectrum
    calls = []

    def capture(**kwargs):
        calls.append(kwargs["channel"])
        return original_fit(**kwargs)

    monkeypatch.setattr(processor, "_fit_energy_spectrum", capture)
    point = "TEST-10:ec:src_Test:0:60"
    result = processor.process(inputs, {"only_point": point,
                                       "ec_windows": {point: {"lo": 255.125, "hi": 345.875}}})
    assert not result["errors"], result["errors"]
    assert calls == [0]
    assert result["outputs"][0]["points_updated"] == [point]
    after = read_json(path)
    assert all(record_bytes(old) == record_bytes(new) for old, new in zip(before, after, strict=True) if old["channel"] == 3)
    with pytest.raises(ValueError, match="康普顿响应"):
        processor.process(inputs, {"only_point": "TEST-10:ec:src_Test:3:60"})
    assert calls == [0]


@pytest.mark.parametrize("point", [
    "Other:ec:src_Test:0:60", "Test:tb:src_Test:0:60", "Test:ec:src_Test:4:60",
    "Test:ec:src_Test:-1:60", "Test:ec:src_Test:0.0:60", "Test:ec:src_Test:0:nan",
    "Test:ec:src_Test:0:inf", "Test:ec:src_Test:0:0", "Test:ec:src_Test:0:-60",
    "Test:ec:src_Test:0:abc", "Test:ec:src_Test:0", "Test:ec::0:60",
    "Test:ec:../source:0:60", "Test:ec:src\\other:0:60", "Test:ec:src_Test:0:60:extra", True,
])
def test_invalid_single_point_is_rejected_before_input_reads(tmp_path, point):
    with pytest.raises(ValueError, match="only_point"):
        ECL2Processor("Test", tmp_path).process(tmp_path / "absent", {"only_point": point})


@pytest.mark.parametrize("point", ["Test:ec:src_Test:1:80", "Test:ec:src_absent:1:60"])
def test_single_point_must_have_a_matching_corrected_input_and_configured_line(tmp_path, monkeypatch, point):
    processor, inputs, path, _ = source_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr(processor, "_fit_energy_spectrum", lambda **_: pytest.fail("No matching line may be fitted"))
    result = processor.process(inputs, {"only_point": point})
    assert result["outputs"] == [] and result["n_processed"] == 0
    assert any("only_point" in error["error"] for error in result["errors"])
    assert path.read_bytes() == before
