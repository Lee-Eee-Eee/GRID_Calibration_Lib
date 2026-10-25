"""Source energy identities and per-channel ADC windows are editable together."""

import copy

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_peaks import catalog, draft_points, normalize_peak_sets


def corrected_source(products, *, name="Test", metadata=None, signal=None):
    path = products / f"Test/ec/L1/parquet/src_{name}_2m_corr.l1.parquet"
    if signal is None:
        signal = pd.DataFrame({"channel": [0, 1, 2, 3] * 4, "amp_corr": np.arange(16), "utc": np.arange(16)})
    write_parquet(path, signal, metadata=metadata or {}, meta_key="tb_correction")
    return path


def test_filename_preloads_source_lines_without_event_reads(tmp_path, monkeypatch):
    products = tmp_path / "runs/review/products"
    path = corrected_source(products, name="Na-22")
    import pyarrow.parquet as pq

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("Catalog must not load corrected pulse tables")

    monkeypatch.setattr(pq, "read_table", unexpected_read)
    result = catalog(ComputeAdapter(tmp_path), "Test", products, profile={})["spectra"]
    assert len(result) == 1
    source = result[0]
    assert source["energy_key"] == "src_Na-22" and source["source_name"] == "Na22"
    assert source["signal_file"] == path.name and source["source_path"] == str(path)
    assert source["source_files"] == [{"name": path.name, "path": str(path), "exposure_minutes": 2.}]
    assert [line["energy_keV"] for line in source["lines"]] == [511., 1274.5]
    assert all(line["windows"] == {} and line["centers"] == {} for line in source["lines"])


def test_preset_aliases_override_default_source_lines_and_metadata(tmp_path):
    products = tmp_path / "products"
    corrected_source(products, name="Na-22", metadata={"peak_energies_keV": [100.]})
    profile = {"discovery": {"sources": [{"name": "Na22", "aliases": ["Na-22"], "energies_keV": [510.9, 1274.6]}]}}
    source = catalog(ComputeAdapter(tmp_path), "Test", products, profile=profile)["spectra"][0]
    assert source["origin"] == "preset"
    assert [line["energy_keV"] for line in source["lines"]] == [510.9, 1274.6]


def test_custom_source_line_can_be_selected_before_any_fit_exists(tmp_path):
    products = tmp_path / "products"
    path = corrected_source(products, name="Unknown")
    config = {"ec_peak_sets": {"src_Unknown": {"source_name": "New source", "lines": [
        {"energy_keV": 80., "windows": {"2": {"lo": 330., "hi": 450.}}, "centers": {"2": 390.}}
    ]}}}
    before = copy.deepcopy(config)
    sources = catalog(ComputeAdapter(tmp_path), "Test", products, config=config)
    points = draft_points(sources, "Test", 2)
    assert len(points) == 1
    point = points[0]
    assert point["id"] == "Test:ec:src_Unknown:2:80"
    assert point["source_path"] == str(path) and point["signal_file"] == path.name
    assert point["lo"] == 330 and point["hi"] == 450 and point["draft_center"] == 390
    assert point["fit_status"] == "not_fitted" and not point["fit_performed"]
    assert point["center"] is None and not point["quality_pass"]
    assert config == before


def test_removed_line_is_hidden_and_current_window_override_is_explicit(tmp_path):
    products = tmp_path / "products"
    corrected_source(products, name="Test", metadata={"peak_energies_keV": [60., 122.]})
    config = {"ec_peak_sets": {"src_Test": {"source_name": "Test", "lines": [{"energy_keV": 122.}]}},
              "ec_windows": {"Test:ec:src_Test:1:122": {"lo": 550., "hi": 670.}}}
    existing = [{"id": f"Test:ec:src_Test:1:{energy:g}", "stem": "src_Test", "energy_key": "src_Test",
                 "E": energy, "channel": 1, "center": 610., "bin_width": 8., "quality_pass": True}
                for energy in (60., 122.)]
    points = draft_points(catalog(ComputeAdapter(tmp_path), "Test", products, config=config), "Test", 1, existing)
    assert len(points) == 1 and points[0]["E"] == 122
    assert points[0]["center"] == 610 and points[0]["bin_width"] == 8
    assert points[0]["draft_window"] == {"lo": 550., "hi": 670.}


@pytest.mark.parametrize("value", [
    [], {"../src_X": {"lines": []}}, {"src_bkg_2m": {"lines": []}},
    {"src_X": {"lines": [{"energy_keV": 0.}]}},
    {"src_X": {"lines": [{"energy_keV": True}]}},
    {"src_X": {"lines": [{"energy_keV": 60.}, {"energy_keV": 60.}]}},
    {"src_X": {"lines": [{"energy_keV": 60., "windows": {"4": {"lo": 0., "hi": 40.}}}]}},
    {"src_X": {"lines": [{"energy_keV": 60., "windows": {"0": {"lo": 40., "hi": 40.}}}]}},
    {"src_X": {"lines": [{"energy_keV": 60., "centers": {"0": float("nan")}}]}},
])
def test_invalid_source_configuration_is_rejected_before_processing(value):
    with pytest.raises(ValueError):
        normalize_peak_sets(value)


def fitting_fixture(tmp_path, monkeypatch):
    products = tmp_path / "products"
    rng = np.random.default_rng(184)
    frames, backgrounds = [], []
    for channel in range(4):
        values = np.r_[rng.normal(300., 9., 2000), rng.normal(610., 12., 2500), rng.uniform(50, 750, 400)]
        frames.append(pd.DataFrame({"channel": channel, "amp_corr": values, "crc_check": True,
                                    "utc": np.linspace(0, 120, len(values))}))
        backgrounds.append(pd.DataFrame({"channel": channel, "amp_corr": rng.uniform(50, 750, 500), "crc_check": True,
                                         "utc": np.linspace(0, 120, 500)}))
    corrected_source(products, metadata={"peak_energies_keV": [60., 122.]}, signal=pd.concat(frames))
    write_parquet(products / "Test/ec/L1/parquet/src_bkg_2m_corr.l1.parquet", pd.concat(backgrounds))
    previous = [{"channel": channel, "E": energy, "center": 100. + channel, "review_note": "keep"}
                for channel in range(4) for energy in (60., 122.)]
    output = products / "Test/ec/L2/json/src_Test_fit_results.l2.json"
    write_json(output, previous)
    processor = ECL2Processor("Test", products)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *_: None)
    return processor, products / "Test/ec/L1", output, previous


def test_manual_added_line_is_fitted_at_its_assigned_energy_and_preserves_peer_records(tmp_path, monkeypatch):
    processor, inputs, output, previous = fitting_fixture(tmp_path, monkeypatch)
    result = processor.process(inputs, {
        "only_point": "Test:ec:src_Test:1:130",
        "ec_peak_sets": {"src_Test": {"source_name": "Test", "lines": [
            {"energy_keV": 60.}, {"energy_keV": 122.},
            {"energy_keV": 130., "windows": {"1": {"lo": 555., "hi": 665.}}, "centers": {"1": 610.}}
        ]}},
    })
    assert not result["errors"], result["errors"]
    assert result["outputs"][0]["points_updated"] == ["Test:ec:src_Test:1:130"]
    after = read_json(output)
    assert after[:len(previous)] == previous
    added = next(row for row in after if row["E"] == 130.)
    assert added["channel"] == 1 and added["center"] == pytest.approx(610., abs=2.)
    assert added["lo"] == 555 and added["hi"] == 665


def test_current_channel_source_fit_handles_all_configured_lines_and_removes_disabled_energies(tmp_path, monkeypatch):
    processor, inputs, output, previous = fitting_fixture(tmp_path, monkeypatch)
    config = {"fit_channels": [1], "ec_peak_sets": {"src_Test": {"source_name": "Test", "lines": [
        {"energy_keV": 60., "windows": {"1": {"lo": 250., "hi": 350.}}},
        {"energy_keV": 130., "windows": {"1": {"lo": 555., "hi": 665.}}}
    ]}}}
    result = processor.process(inputs, config)
    assert not result["errors"], result["errors"]
    assert result["outputs"][0]["points_updated"] == ["Test:ec:src_Test:1:60", "Test:ec:src_Test:1:130"]
    after = read_json(output)
    assert all(row["E"] != 122. for row in after)
    assert {(row["channel"], row["E"]) for row in after} == {(channel, 60.) for channel in range(4)} | {(1, 130.)}
    for row in previous:
        if row["channel"] != 1 and row["E"] == 60.:
            assert row in after


@pytest.mark.parametrize("active_energies", [[40., 60., 80., 100.], []])
def test_ec_l3_uses_only_current_source_lines_without_changing_l2_or_dropping_warnings(tmp_path, monkeypatch, active_energies):
    root = tmp_path / "products"

    def rows(energies, source):
        return [{"channel": channel, "E": energy, "center": energy * 6 + channel, "center_err": 1.,
                 "sigma": 5., "sigma_err": .2, "resolution": 12., "resolution_err": .5,
                 "fwhm": 11.77, "source": source, "rsquared": .99,
                 "fit_quality": {"accepted": energy != 80., "warnings": ["review residual"] if energy == 80. else []}}
                for channel in range(4) for energy in energies]

    source = root / "Test/ec/L2/json/src_Test_fit_results.l2.json"
    xray = root / "Test/ec/L2/json/xray_fit_results.l2.json"
    write_json(source, rows([40., 60., 80., 100., 130.], "src"))
    write_json(xray, rows([45., 65., 85.], "x"))
    before = {path: path.read_bytes() for path in (source, xray)}
    processor = ECL3Processor("Test", root)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *_: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *_: None)
    original = processor._fit_channel
    used = []

    def capture(channel, points, *args):
        used.extend((row["source"], row["E"], row["channel"]) for row in points)
        return original(channel, points, *args)

    monkeypatch.setattr(processor, "_fit_channel", capture)
    result = processor.process(root / "Test/ec/L2", {
        "ec_peak_sets": {"src_Test": {"source_name": "Test", "lines": [{"energy_keV": energy} for energy in active_energies]}},
        "ec_excluded": ["Test:ec:src_Test:0:40"],
    })
    assert not result["errors"], result["errors"]
    assert {energy for kind, energy, _ in used if kind == "src"} == set(active_energies)
    assert ("src", 40., 0) not in used
    assert {energy for kind, energy, _ in used if kind == "x"} == {45., 65., 85.}
    assert len(result["inactive_points"]) == (4 if active_energies else 20)
    if active_energies:
        assert ("src", 80., 0) in used
        assert any("review residual" in warning for warning in result["warnings"])
    assert all(value["EC_low"] is not None for value in result["calibration_params"].values())
    assert before == {path: path.read_bytes() for path in (source, xray)}
