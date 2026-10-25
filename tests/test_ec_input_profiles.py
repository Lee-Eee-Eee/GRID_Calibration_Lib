"""Configured energy/source labels survive the actual EC correction and fits."""

import hashlib
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
from calibration_lib.ec.processors.L2_processor import ECL2Processor


def params(tmp_path):
    path = tmp_path / "selected-TB.json"
    write_json(path, [{"G0": 0.001, "k": 0.0184, "V0": 24.11, "b": 54.31, "c": 23459.83}] * 4)
    return path


def frame(channel, amplitude, duration=90):
    return pd.DataFrame({
        "channel": channel, "amp": amplitude, "utc": np.linspace(0, duration, len(amplitude)),
        "temp": 25.0, "bias": 28.5, "crc_check": np.arange(len(amplitude)) % 2 == 0,
    })


def test_l1_decimal_xray_scope_does_not_process_unrelated_background(tmp_path):
    root = tmp_path / "products"
    directory = root / "Custom/ec/L0/parquet"
    exact_energy = 42.500000000123
    acquisition = {"profile_sha256": "run-profile", "revision": 4, "xml": {"event": {"sha256": "event-xml"}}, "observation": {"energy_keV": exact_energy}}
    original = frame(0, np.linspace(100, 300, 60))
    input_path = directory / "42.5keV_ch0.l0.parquet"
    write_parquet(input_path, original, schema_metadata={"acquisition": acquisition})
    unrelated = directory / "src_bkg_2.5m.l0.parquet"
    unrelated.write_bytes(b"unrelated broken old background must not be read")
    cfg = replace(get_payload_config("14B"), source_specs=[], bkg_exposure_minutes=2.5)
    result = ECL1Processor("Custom", root).process(cfg, params(tmp_path), only_stems=["42.5keV_ch0"])
    assert not result["errors"]
    assert [row["stem"] for row in result["outputs"]] == ["42.5keV_ch0"]
    output = root / "Custom/ec/L1/parquet/42.5keV_ch0_corr.l1.parquet"
    corrected = read_parquet(output)
    np.testing.assert_allclose(corrected["amp_corr"], original.loc[original.crc_check, "amp"])
    assert corrected["crc_check"].tolist() == [True] * int(original.crc_check.sum())
    provenance = read_parquet_metadata(output, "acquisition")
    assert provenance["profile_sha256"] == "run-profile" and provenance["revision"] == 4
    assert provenance["l0_file"] == input_path.name
    assert provenance["l0_sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    correction = read_parquet_metadata(output, "tb_correction")
    assert correction["acquisition"] == provenance
    assert correction["energy_keV"] == exact_energy


def source_inputs(tmp_path):
    root = tmp_path / "products"
    directory = root / "Custom/ec/L0/parquet"
    rng = np.random.default_rng(701)
    signal = pd.concat([
        frame(channel, np.concatenate([rng.normal(500 + 12 * channel, 15, 2400), rng.uniform(100, 900, 300)]))
        for channel in range(4)
    ], ignore_index=True)
    background = pd.concat([frame(channel, rng.uniform(100, 900, 250), duration=150) for channel in range(4)], ignore_index=True)
    metadata = {"profile_sha256": "custom-source-profile", "revision": 2}
    write_parquet(directory / "src_Am_241_1.5m.l0.parquet", signal, schema_metadata={"acquisition": metadata})
    write_parquet(directory / "src_bkg_2.5m.l0.parquet", background, schema_metadata={"acquisition": metadata})
    cfg = replace(get_payload_config("14B"), source_specs=[SourceSpec("Am_241", "", "", 1.5, [59.54])], bkg_exposure_minutes=2.5)
    return root, cfg


def test_custom_source_name_decimal_exposure_and_energy_reach_actual_peak_fitter(tmp_path, monkeypatch):
    root, cfg = source_inputs(tmp_path)
    corrected = ECL1Processor("Custom", root).process(cfg, params(tmp_path), only_stems=["src_Am_241_1.5m"])
    assert not corrected["errors"]
    assert {row["stem"] for row in corrected["outputs"]} == {"src_Am_241_1.5m", "src_bkg_2.5m"}
    source_path = root / "Custom/ec/L1/parquet/src_Am_241_1.5m_corr.l1.parquet"
    source_meta = read_parquet_metadata(source_path, "tb_correction")
    assert source_meta["source_name"] == "Am_241"
    assert source_meta["exposure_minutes"] == 1.5 and source_meta["peak_energies_keV"] == [59.54]
    processor = ECL2Processor("Custom", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    result = processor.process(root / "Custom/ec/L1", {"only_energies": ["src_Am_241"], "background_stem": "src_bkg_2.5m"})
    assert not result["errors"], result["errors"]
    records = read_json(root / "Custom/ec/L2/json/src_Am_241_fit_results.l2.json")
    assert len(records) == 4
    for row in records:
        assert row["source_name"] == "Am_241" and row["E"] == 59.54
        assert row["center"] == pytest.approx(500 + row["channel"] * 12, abs=5)
        assert row["window_source"] == "auto" and row["window_manual"] is False
        assert row["signal_exposure_minutes"] == 1.5 and row["background_exposure_minutes"] == 2.5
        assert row["normalization"] == "utc_duration"
        assert row["signal_acquisition"]["profile_sha256"] == "custom-source-profile"
        assert row["background_acquisition"]["profile_sha256"] == "custom-source-profile"
        assert row["signal_acquisition"]["l0_sha256"] == source_meta["acquisition"]["l0_sha256"]
    assert result["warnings"]


def test_new_decimal_xray_energy_uses_existing_auto_window_without_packaged_config(tmp_path, monkeypatch):
    root = tmp_path / "products"
    rng = np.random.default_rng(602)
    directory = root / "Custom/ec/L1/parquet"
    exact_energy = 42.500000000123
    for channel in range(4):
        corrected = frame(channel, np.concatenate([rng.normal(350 + 100 * channel, 16, 1800), rng.uniform(100, 850, 200)]))
        corrected["amp_corr"] = corrected["amp"]
        write_parquet(directory / f"42.5keV_ch{channel}_corr.l1.parquet", corrected, metadata={"energy_keV": exact_energy}, meta_key="tb_correction")
    # Unrelated source backgrounds must not affect an X-ray-only calculation.
    for duration in (2.5, 3.5):
        (directory / f"src_bkg_{duration}m_corr.l1.parquet").write_bytes(b"unrelated old background")
    processor = ECL2Processor("Custom", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    monkeypatch.setattr(processor, "_xray_consensus_center", lambda *args: None)
    result = processor.process(root / "Custom/ec/L1", {"only_energies": ["42.5keV"]})
    assert not result["errors"], result["errors"]
    records = read_json(root / "Custom/ec/L2/json/42.5keV_fit_results.l2.json")
    assert len(records) == 4
    assert {row["channel"] for row in records} == {0, 1, 2, 3}
    assert all(row["E"] == exact_energy and row["window_source"] == "auto" for row in records)
    assert all(row["background_file"].startswith("42.5keV_") for row in records)
    assert result["warnings"]


def test_source_multiple_backgrounds_require_explicit_selection(tmp_path, monkeypatch):
    root, cfg = source_inputs(tmp_path)
    input_directory = root / "Custom/ec/L0/parquet"
    copy = read_parquet(input_directory / "src_bkg_2.5m.l0.parquet")
    write_parquet(input_directory / "src_bkg_3.5m.l0.parquet", copy)
    # Configured exposure selects one L0 background, independently of sorting.
    corrected = ECL1Processor("Custom", root).process(cfg, params(tmp_path))
    assert not corrected["errors"]
    assert not (root / "Custom/ec/L1/parquet/src_bkg_3.5m_corr.l1.parquet").exists()
    l1_other = root / "Custom/ec/L1/parquet/src_bkg_3.5m_corr.l1.parquet"
    copy["amp_corr"] = copy["amp"]
    write_parquet(l1_other, copy)
    processor = ECL2Processor("Custom", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    with pytest.raises(ValueError, match="Multiple EC backgrounds"):
        processor.process(root / "Custom/ec/L1", {"only_energies": ["src_Am_241"]})
    result = processor.process(root / "Custom/ec/L1", {"only_energies": ["src_Am_241"], "background_stem": "src_bkg_2.5m"})
    assert not result["errors"]


def test_missing_source_spec_is_a_failure_instead_of_successful_skip(tmp_path):
    root, cfg = source_inputs(tmp_path)
    result = ECL1Processor("Custom", root).process(replace(cfg, source_specs=[]), params(tmp_path), only_stems=["src_Am_241_1.5m"])
    assert any("configure its peak energies" in record["error"] for record in result["errors"])
    assert not (root / "Custom/ec/L1/parquet/src_Am_241_1.5m_corr.l1.parquet").exists()


def test_scientific_exposure_precision_is_kept_separately_from_canonical_filename(tmp_path):
    root, cfg = source_inputs(tmp_path)
    source_duration, background_duration = 1.5000000000123, 2.5000000000123
    cfg = replace(cfg, source_specs=[replace(cfg.source_specs[0], exposure_minutes=source_duration)], bkg_exposure_minutes=background_duration)
    result = ECL1Processor("Custom", root).process(cfg, params(tmp_path), only_stems=["src_Am_241_1.5m"])
    assert not result["errors"]
    directory = root / "Custom/ec/L1/parquet"
    assert read_parquet_metadata(directory / "src_Am_241_1.5m_corr.l1.parquet", "tb_correction")["exposure_minutes"] == source_duration
    assert read_parquet_metadata(directory / "src_bkg_2.5m_corr.l1.parquet", "tb_correction")["exposure_minutes"] == background_duration


def test_source_exposure_selection_does_not_silently_replace_another_observation(tmp_path, monkeypatch):
    root, cfg = source_inputs(tmp_path)
    ECL1Processor("Custom", root).process(cfg, params(tmp_path))
    directory = root / "Custom/ec/L1/parquet"
    original = directory / "src_Am_241_1.5m_corr.l1.parquet"
    alternate = directory / "src_Am_241_4.5m_corr.l1.parquet"
    write_parquet(alternate, read_parquet(original), metadata=read_parquet_metadata(original, "tb_correction"), meta_key="tb_correction")
    processor = ECL2Processor("Custom", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    ambiguous = processor.process(root / "Custom/ec/L1", {"only_energies": ["src_Am_241"]})
    assert any("Multiple observations" in row["error"] for row in ambiguous["errors"])
    selected = processor.process(root / "Custom/ec/L1", {
        "only_energies": ["src_Am_241"], "only_stems": ["src_Am_241_1.5m"],
        "background_stem": "src_bkg_2.5m",
    })
    assert not selected["errors"]
    rows = read_json(root / "Custom/ec/L2/json/src_Am_241_fit_results.l2.json")
    assert all(row["signal_file"] == original.name for row in rows)


def test_source_missing_background_cannot_fall_back_to_another_signal_channel(tmp_path, monkeypatch):
    root, cfg = source_inputs(tmp_path)
    ECL1Processor("Custom", root).process(cfg, params(tmp_path))
    processor = ECL2Processor("Custom", root)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *args: None)
    result = processor.process(root / "Custom/ec/L1", {
        "only_energies": ["src_Am_241"], "background_stem": "src_bkg_99.5m",
    })
    assert result["n_processed"] == 0
    assert any("requires an explicitly available corrected background" in row["error"] for row in result["errors"])
