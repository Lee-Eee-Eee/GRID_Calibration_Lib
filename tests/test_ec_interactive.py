"""Interactive EC regression coverage with real histograms and peak fitting."""

import hashlib

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_interactive import ECInspector, point_id


def source_fixture(root):
    rng = np.random.default_rng(919)
    signals, backgrounds = [], []
    for ch in range(4):
        amp = np.concatenate([rng.normal(500 + 5 * ch, 14, 3000), rng.uniform(0, 800, 400)])
        signals.append(
            pd.DataFrame(
                {
                    "channel": ch,
                    "amp_corr": amp,
                    "utc": np.linspace(0, 60, len(amp)),
                    "temp": 25.0,
                    "bias": 28.5,
                }
            )
        )
        backgrounds.append(
            pd.DataFrame(
                {
                    "channel": ch,
                    "amp_corr": rng.uniform(0, 800, 1000),
                    "utc": np.linspace(0, 90, 1000),
                    "temp": 25.0,
                    "bias": 28.5,
                }
            )
        )
    signal, background = pd.concat(signals), pd.concat(backgrounds)
    l1 = root / "14B/ec/L1/parquet"
    write_parquet(
        l1 / "src_Na22_5m_corr.l1.parquet",
        signal,
        metadata={"peak_energies_keV": [511.0], "tb_corrfile": "selected_TB.l2.json"},
        meta_key="tb_correction",
    )
    write_parquet(l1 / "src_bkg_30m_corr.l1.parquet", background)
    return signal, background


def test_source_manual_windows_use_actual_fitter_and_inspector_same_rate_spectrum(tmp_path, monkeypatch):
    root = tmp_path / "runs/review/products"
    signal, background = source_fixture(root)
    processor = ECL2Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    windows = {point_id("14B", "src_Na22", ch, 511.0): {"lo": 440, "hi": 575} for ch in range(4)}
    result = processor.process(root / "14B/ec/L1", {"ec_windows": windows})
    assert not result["errors"], result["errors"]
    assert result["n_processed"] == 1
    records = read_json(root / "14B/ec/L2/json/src_Na22_fit_results.l2.json")
    assert len(records) == 4
    for ch, record in enumerate(records):
        assert record["lo"] == 440 and record["hi"] == 575
        assert record["window_manual"]
        assert abs(record["center"] - (500 + 5 * ch)) < 2
        assert record["model_parameters"]["g_center"] == record["center"]
        assert (
            record["signal_sha256"]
            == hashlib.sha256(
                (root / "14B/ec/L1/parquet/src_Na22_5m_corr.l1.parquet").read_bytes()
            ).hexdigest()
        )
        assert (
            record["background_sha256"]
            == hashlib.sha256(
                (root / "14B/ec/L1/parquet/src_bkg_30m_corr.l1.parquet").read_bytes()
            ).hexdigest()
        )
        assert record["signal_tb_correction"]["tb_corrfile"] == "selected_TB.l2.json"
        assert record["background_tb_correction"] == {}
    inspector = ECInspector(ComputeAdapter(tmp_path))
    selected = point_id("14B", "src_Na22", 2, 511.0)
    spectrum = inspector.spectrum("14B", root, selected)
    expected = processor._prepare_spectrum(
        signal[signal.channel == 2], background[background.channel == 2], 4.0
    )
    np.testing.assert_allclose(spectrum["x"], expected["mids"])
    np.testing.assert_allclose(spectrum["y"], expected["net"])
    np.testing.assert_allclose(spectrum["background"], expected["bkg_spec"])
    assert spectrum["curve"]["exact"]
    np.testing.assert_allclose(
        np.array(spectrum["curve"]["y"]),
        np.array(spectrum["curve"]["gaussian"]) + spectrum["curve"]["background"],
    )
    assert spectrum["tb_corrfile"] == "selected_TB.l2.json"
    assert spectrum["n_events"] == 3400
    assert spectrum["fit_input_verified"] is True
    assert spectrum["input_evidence"]["signal"]["verified"] is True
    assert spectrum["input_evidence"]["background"]["verified"] is True


def test_historical_curve_missing_background_is_explicit_and_reads_do_not_fit(tmp_path, monkeypatch):
    root = tmp_path / "products"
    source_fixture(root)
    record = {
        "channel": 0,
        "E": 511.0,
        "center": 500.0,
        "sigma": 14.0,
        "amplitude": 100.0,
        "lo": 440.0,
        "hi": 575.0,
        "fit_mode": "lin + gaus",
        "background_channel": 0,
    }
    write_json(root / "14B/ec/L2/json/src_Na22_fit_results.l2.json", [record])
    monkeypatch.setattr(
        ECL2Processor, "_fit_energy_spectrum", lambda *args, **kwargs: pytest.fail("GET must not fit")
    )
    spectrum = ECInspector(ComputeAdapter(tmp_path)).spectrum(
        "14B", tmp_path / "runs/empty/products", point_id("14B", "src_Na22", 0, 511.0)
    )
    assert spectrum["source"] == "historical"
    assert not spectrum["curve"]["exact"]
    assert spectrum["curve"]["y"] == []
    assert spectrum["curve"]["note"]
    assert len(spectrum["curve"]["gaussian"]) > 0
    assert spectrum["fit_input_verified"] is None
    assert "unverified" in spectrum["fit_input_note"]


@pytest.mark.parametrize("role", ["signal", "background"])
def test_same_name_corrected_input_replacement_is_detected_before_curve_is_claimed_current(
    tmp_path, monkeypatch, role
):
    root = tmp_path / "runs/review/products"
    signal, background = source_fixture(root)
    processor = ECL2Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    windows = {point_id("14B", "src_Na22", ch, 511.0): {"lo": 440, "hi": 575} for ch in range(4)}
    result = processor.process(root / "14B/ec/L1", {"ec_windows": windows})
    assert not result["errors"], result["errors"]
    inspector = ECInspector(ComputeAdapter(tmp_path))
    selected = point_id("14B", "src_Na22", 0, 511.0)
    before = inspector.spectrum("14B", root, selected)
    assert before["fit_input_verified"] is True
    name = "src_Na22_5m_corr.l1.parquet" if role == "signal" else "src_bkg_30m_corr.l1.parquet"
    frame = (signal if role == "signal" else background).copy()
    frame["amp_corr"] += 2
    write_parquet(
        root / "14B/ec/L1/parquet" / name,
        frame,
        metadata={"tb_corrfile": "replacement_TB.l2.json"},
        meta_key="tb_correction",
    )
    after = inspector.spectrum("14B", root, selected)
    assert after["fit_input_verified"] is False
    assert after["curve"]["input_verified"] is False
    assert "changed after this fit" in after["fit_input_note"]
    assert after["input_evidence"][role]["verified"] is False
    assert after["input_evidence"][role]["sha256"] != after["input_evidence"][role]["expected_sha256"]
    assert after["input_evidence"][role]["tb_correction"]["tb_corrfile"] == "replacement_TB.l2.json"
    assert after["fit"]["center"] == before["fit"]["center"]
    assert after["fit"][f"{role}_tb_correction"] == before["fit"][f"{role}_tb_correction"]


def test_explicit_xray_windows_survive_cross_channel_consensus_recovery(tmp_path, monkeypatch):
    root = tmp_path / "products"
    rng = np.random.default_rng(1107)
    windows = {}
    for ch in range(4):
        center = 300 + ch * 150
        amplitude = np.concatenate([rng.normal(center, 10, 2200), rng.uniform(0, 950, 400)])
        frame = pd.DataFrame(
            {
                "amp_corr": amplitude,
                "utc": np.linspace(0, 60, len(amplitude)),
                "channel": ch,
                "temp": 25.0,
                "bias": 28.5,
            }
        )
        write_parquet(root / f"14B/ec/L1/parquet/40keV_ch{ch}_corr.l1.parquet", frame)
        windows[point_id("14B", "40keV", ch, 40.0)] = {"lo": center - 45, "hi": center + 45}
    processor = ECL2Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    # An inconsistent consensus must never overwrite an intentional window.
    monkeypatch.setattr(processor, "_xray_consensus_center", lambda fits: 9999.0)
    result = processor.process(root / "14B/ec/L1", {"ec_windows": windows})
    assert not result["errors"], result["errors"]
    records = read_json(root / "14B/ec/L2/json/40keV_fit_results.l2.json")
    for record in records:
        window = windows[point_id("14B", "40keV", record["channel"], 40.0)]
        assert record["lo"] == window["lo"] and record["hi"] == window["hi"]
        assert record["window_manual"]
        assert abs(record["center"] - (300 + record["channel"] * 150)) < 2


def good_points():
    return [
        {
            "channel": ch,
            "E": energy,
            "center": energy * 6 + ch,
            "center_err": 1.0,
            "sigma": 5.0,
            "sigma_err": 0.5,
            "resolution": 10.0,
            "resolution_err": 1.0,
            "fwhm": 11.77,
            "source": "x",
            "rsquared": 0.99,
        }
        for ch in range(4)
        for energy in (40.0, 45.0, 60.0, 80.0)
    ]


def test_l3_exclusion_uses_stable_peak_identity_and_does_not_modify_l2(tmp_path, monkeypatch):
    root = tmp_path / "products"
    path = root / "14B/ec/L2/json/fixture_fit_results.l2.json"
    write_json(path, good_points())
    before = path.read_bytes()
    processor = ECL3Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *args: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *args: None)
    seen = []
    fit = processor._fit_quadratic_weighted

    def capture(x, y, err):
        seen.append(list(y))
        return fit(x, y, err)

    monkeypatch.setattr(processor, "_fit_quadratic_weighted", capture)
    excluded = point_id("14B", "fixture", 0, 60.0)
    result = processor.process(root / "14B/ec/L2", {"ec_excluded": [excluded]})
    assert result["excluded_points"] == [excluded]
    assert seen[0] == [40.0, 45.0, 80.0]
    assert seen[1] == [40.0, 45.0, 60.0, 80.0]
    assert path.read_bytes() == before
    overview = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0)
    point = next(row for row in overview["points"] if row["id"] == excluded)
    assert point["excluded"] and not point["used"]
    assert overview["diagnostics"]["n_used"] == 3
    assert len(overview["curve"]["x"]) == 250
    assert abs(overview["points"][0]["residual_keV"]) < 1e-6


def test_invalid_manual_ec_window_is_reported_without_faking_success(tmp_path, monkeypatch):
    root = tmp_path / "products"
    source_fixture(root)
    processor = ECL2Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    windows = {point_id("14B", "src_Na22", ch, 511.0): {"lo": 440, "hi": 575} for ch in range(4)}
    windows[point_id("14B", "src_Na22", 0, 511.0)] = {"lo": 570, "hi": 440}
    result = processor.process(
        root / "14B/ec/L1", {"ec_windows": windows}
    )
    assert result["n_processed"] == 1
    assert "Invalid manual EC window" in str(result["errors"])
    assert result["outputs"][0]["channels_updated"] == [1, 2, 3]


def test_quality_warnings_do_not_exclude_numerically_usable_points():
    rows = good_points()
    rows[0]["resolution"] = 99
    rows[1]["center_err"] = 0
    rows[2]["rsquared"] = 0.2
    retained = ECL3Processor._filter_quality(rows)
    assert rows[0] in retained and ECInspector.quality_reasons(rows[0])
    assert rows[2] in retained and ECInspector.quality_reasons(rows[2])
    assert rows[1] in retained and ECInspector.quality_reasons(rows[1])
    assert all(ECInspector.quality_reasons(row) == row["quality_warnings"] for row in rows)
