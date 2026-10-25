"""Changing energy lines reuses counts, while changed inputs/results stay live."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_interactive import ECInspector, _clean, point_id
from calibration_lib.workbench.read_cache import EC_READ_CACHE, ReadCache, file_fingerprint


@pytest.fixture(autouse=True)
def clear_reader_cache():
    EC_READ_CACHE.clear()
    yield
    EC_READ_CACHE.clear()


@pytest.fixture
def spectra(tmp_path):
    products = tmp_path / "runs/review/products"
    directory = products / "14B/ec/L1/parquet"
    rng = np.random.default_rng(731)
    signal = pd.concat([pd.DataFrame({
        "channel": ch, "amp_corr": np.r_[rng.normal(400 + 10 * ch, 12, 2000), rng.normal(850 + 10 * ch, 20, 1600)],
        "utc": np.linspace(0, 120, 3600), "crc_check": np.arange(3600) != 0,
        "unused_waveform": np.zeros(3600),
    }) for ch in range(4)], ignore_index=True)
    background = pd.concat([pd.DataFrame({
        "channel": ch, "amp_corr": rng.uniform(0, 900 - 50 * ch, 800),
        "utc": np.linspace(0, 80, 800), "crc_check": True,
    }) for ch in range(4)], ignore_index=True)
    signal_path = directory / "src_Na22_5m_corr.l1.parquet"
    background_path = directory / "src_bkg_30m_corr.l1.parquet"
    write_parquet(signal_path, signal, metadata={"tb_corrfile": "selected-TB.json"}, meta_key="tb_correction")
    write_parquet(background_path, background)
    records = [{
        "channel": ch, "E": energy, "center": center + 10 * ch, "sigma": 12., "amplitude": 50.,
        "lo": center - 50., "hi": center + 50., "bin_width": 4., "fit_mode": "gaus", "background_channel": ch,
        "signal_file": signal_path.name, "background_file": background_path.name,
        "signal_sha256": hashlib.sha256(signal_path.read_bytes()).hexdigest(),
        "background_sha256": hashlib.sha256(background_path.read_bytes()).hexdigest(),
    } for ch in range(4) for energy, center in ((511., 400.), (1274.5, 850.))]
    record_path = products / "14B/ec/L2/json/src_Na22_fit_results.l2.json"
    write_json(record_path, records)
    return products, signal, background, signal_path, background_path, record_path


def test_switching_lines_reuses_same_counting_histogram_across_inspector_instances(spectra, tmp_path, monkeypatch):
    products, signal, background, *_ = spectra
    calls = {"prepare": 0, "read": 0, "evidence": 0}
    original_prepare, original_read, original_evidence = ECL2Processor._prepare_spectrum, pq.read_table, ECL2Processor.input_evidence

    def prepare(self, *args):
        calls["prepare"] += 1
        return original_prepare(self, *args)

    def read(*args, **kwargs):
        calls["read"] += 1
        assert kwargs["columns"] == ["channel", "amp_corr", "utc", "crc_check"]
        return original_read(*args, **kwargs)

    def evidence(*args):
        calls["evidence"] += 1
        return original_evidence(*args)

    monkeypatch.setattr(ECL2Processor, "_prepare_spectrum", prepare)
    monkeypatch.setattr(pq, "read_table", read)
    monkeypatch.setattr(ECL2Processor, "input_evidence", evidence)
    first = ECInspector(ComputeAdapter(tmp_path)).spectrum("14B", products, point_id("14B", "src_Na22", 0, 511))
    second = ECInspector(ComputeAdapter(tmp_path)).spectrum("14B", products, point_id("14B", "src_Na22", 0, 1274.5))
    assert calls == {"prepare": 1, "read": 2, "evidence": 2}
    expected = original_prepare(ECL2Processor("14B", products), signal[signal.channel == 0], background[background.channel == 0], 4.)
    for key, prepared_key in (("x", "mids"), ("y", "net"), ("yerr", "net_err")):
        np.testing.assert_array_equal(first[key], expected[prepared_key])
        np.testing.assert_array_equal(second[key], expected[prepared_key])
    assert first["curve"]["x"] != second["curve"]["x"]
    assert first["n_events"] == second["n_events"] == 3599
    first["y"][0] = -999.
    again = ECInspector(ComputeAdapter(tmp_path)).spectrum("14B", products, second["id"])
    assert again["y"][0] != -999.
    assert EC_READ_CACHE.bytes <= EC_READ_CACHE.max_bytes


def test_channel_background_and_bin_width_are_distinct_cache_inputs(spectra, tmp_path, monkeypatch):
    products, *_rest, record_path = spectra
    inspector = ECInspector(ComputeAdapter(tmp_path))
    first = inspector.spectrum("14B", products, point_id("14B", "src_Na22", 0, 511))
    other = inspector.spectrum("14B", products, point_id("14B", "src_Na22", 1, 511))
    assert first["y"] != other["y"]
    records = read_json(record_path)
    records[0].update(bin_width=8., background_channel=1)
    write_json(record_path, records)
    changed = inspector.spectrum("14B", products, first["id"])
    np.testing.assert_allclose(np.diff(changed["x"]), 8.)
    assert len(changed["x"]) != len(first["x"])
    assert changed["window_bounds"]["minimum_width"] == 64.


def test_replacing_signal_or_tb_metadata_invalidates_counts_and_evidence(spectra, tmp_path):
    products, signal, _background, signal_path, *_ = spectra
    inspector = ECInspector(ComputeAdapter(tmp_path))
    before = inspector.spectrum("14B", products, point_id("14B", "src_Na22", 0, 511))
    assert before["fit_input_verified"] is True
    replacement = signal.copy()
    replacement["amp_corr"] += 25
    write_parquet(signal_path, replacement, metadata={"tb_corrfile": "new-TB.json"}, meta_key="tb_correction")
    after = inspector.spectrum("14B", products, before["id"])
    assert after["fit_input_verified"] is False
    assert after["tb_corrfile"] == "new-TB.json"
    assert after["y"] != before["y"]
    replacement["crc_check"] = False
    write_parquet(signal_path, replacement)
    with pytest.raises(ValueError, match="CRC"):
        inspector.spectrum("14B", products, before["id"])


def test_changed_fit_window_and_model_are_not_masked_by_cached_counts(spectra, tmp_path):
    products, *_rest, record_path = spectra
    inspector = ECInspector(ComputeAdapter(tmp_path))
    before = inspector.spectrum("14B", products, point_id("14B", "src_Na22", 0, 511))
    records = read_json(record_path)
    records[0].update(lo=365., hi=460., center=410., amplitude=70.)
    write_json(record_path, records)
    after = inspector.spectrum("14B", products, before["id"])
    assert after["x"] == before["x"] and after["y"] == before["y"]
    assert after["lo"] == 365. and after["hi"] == 460.
    assert after["curve"]["y"] != before["curve"]["y"]
    json.dumps(after, allow_nan=False)


def test_unfitted_draft_line_uses_real_spectrum_without_a_saved_l2_record(spectra, tmp_path, monkeypatch):
    products, *_rest, record_path = spectra
    inspector = ECInspector(ComputeAdapter(tmp_path))
    known = inspector.spectrum("14B", products, point_id("14B", "src_Na22", 0, 511))
    record_path.unlink()
    monkeypatch.setattr(inspector, "points", lambda *_: pytest.fail("Provided draft point must not rebuild all L2 points"))
    draft = {"id": "14B:ec:src_Na22:0:750", "stem": "src_Na22", "channel": 0, "E": 750.,
             "center": None, "sigma": None, "amplitude": None, "fit_status": "not_fitted"}
    spectrum = inspector.spectrum("14B", products, draft["id"], point=draft)
    assert spectrum["fit"] is None and spectrum["curve"] is None
    assert spectrum["point"] == draft
    assert len(spectrum["x"]) > 100 and spectrum["n_events"] == 3599
    assert spectrum["fit_input_note"] is None and spectrum["fit_input_verified"] is None
    assert spectrum["window_bounds"]["minimum_width"] == 32.
    for key in ("x", "y", "yerr"):
        assert spectrum[key] == known[key]
    summary = inspector.overview("14B", products, 0, points=[draft])
    assert len(summary["points"]) == 1
    assert summary["points"][0]["quality_reasons"] == []
    assert not summary["points"][0]["quality_suspect"]
    assert not summary["points"][0]["used"]
    with pytest.raises(ValueError, match="通道"):
        inspector.spectrum("14B", products, draft["id"], 1, point=draft)


def test_index_keeps_current_source_file_priority_over_different_historical_exposure(spectra, tmp_path, monkeypatch):
    products, signal, _background, _signal_path, _background_path, record_path = spectra
    write_parquet(tmp_path / "products/14B/ec/L1/parquet/src_Na22_99m_corr.l1.parquet", signal)
    records = read_json(record_path)
    for row in records:
        row.pop("signal_file")
    write_json(record_path, records)
    inspector = ECInspector(ComputeAdapter(tmp_path))
    monkeypatch.setattr(inspector, "_parquet", lambda *_: pytest.fail("Index must not glob once per point"))
    points = inspector.points("14B", products, 0)
    assert all(Path(point["source_path"]).name == "src_Na22_5m_corr.l1.parquet" for point in points)


def test_cache_is_bounded_and_deduplicates_concurrent_loads(tmp_path):
    cache = ReadCache(max_bytes=32, max_entries=2)
    loaded = []

    def load():
        loaded.append(True)
        sleep(.01)
        return b"1234"

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: cache.get("same", load), range(4))) == [b"1234"] * 4
    assert len(loaded) == 1
    for key in range(8):
        cache.get(key, lambda: b"12345678")
    assert cache.bytes <= 32 and len(cache._entries) <= 2 and cache.evictions
    cache.get("oversized", lambda: b"x" * 40)
    assert "oversized" not in cache._entries
    path = tmp_path / "input"
    path.write_bytes(b"first")
    before = file_fingerprint(path)
    replaced = tmp_path / "next"
    replaced.write_bytes(b"other")
    os.utime(replaced, ns=(before[2], before[2]))
    replaced.replace(path)
    assert file_fingerprint(path) != before


def test_vectorized_spectrum_normalization_keeps_strict_json():
    value = _clean({"counts": np.asarray([1., np.nan, np.inf, -np.inf]),
                    "integer": np.asarray([1, 2], dtype=np.int64),
                    "scalar": np.float64(np.nan), "zero_dimensional": np.asarray(4.)})
    assert value == {"counts": [1., None, None, None], "integer": [1, 2], "scalar": None, "zero_dimensional": 4.}
    json.dumps(value, allow_nan=False)
