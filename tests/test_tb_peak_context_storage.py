"""Exercise guide-cache identity and isolation from broken peer L0 inputs."""

import json
import shutil
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from calibration_lib.tb.processors import L1_processor as tb_l1

HISTOGRAM = {"min": 0.0, "max": 512.0, "bin_width": 4.0}


def write_input(path, index=0, *, kind="valid", offset=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "corrupt_parquet":
        path.write_bytes(b"This is not a parquet file")
        return
    values = {
        "channel": np.repeat(np.arange(4), 16),
        "amp": np.tile(np.arange(16) + 112 + offset, 4),
        "crc_check": np.full(64, kind != "failed_crc"),
    }
    if kind == "missing_amplitude":
        del values["amp"]
    hk = {"temp": [float(index)] * 4, "bias": [27.0 + index / 20] * 4}
    metadata = {
        b"hk_data": json.dumps(hk).encode() if kind != "invalid_metadata" else b"{incomplete",
        b"acquisition": json.dumps({"profile_sha256": "shared-reading-profile"}).encode(),
    }
    pq.write_table(pa.table(values).replace_schema_metadata(metadata), path)


@pytest.fixture
def context_inputs(tmp_path):
    paths = [tmp_path / "source-l0" / "parquet" / f"spectrum-{index:02}.l0.parquet"
             for index in range(16)]
    for index, path in enumerate(paths):
        write_input(path, index)
    return paths


@pytest.fixture
def guide_calls(monkeypatch):
    calls = []

    def synthetic_guides(x, spectra, temperature, bias, config):
        calls.append({"spectra": spectra.copy(), "temperature": temperature.copy(), "bias": bias.copy()})
        assert spectra.shape == (16, len(x))
        return [{"expected_center": 120.0, "expected_tolerance": .15, "target_excluded": True,
                 "anchor_indices": [anchor for anchor in range(len(spectra)) if anchor != target]}
                for target in range(len(spectra))]

    monkeypatch.setattr(tb_l1, "gain_guides", synthetic_guides)
    return calls


def test_cache_reuses_identical_l0_content_across_job_directories_and_overlay_order(
    tmp_path, context_inputs, guide_calls, monkeypatch,
):
    source = tb_l1.TBL1Processor("10B", tmp_path / "run-products")
    config = {"histogram_config": HISTOGRAM}
    original = source._learn_peak_guides(context_inputs, config, [0])
    assert len(guide_calls) == 1

    # A selected-file job overlays one copied stem on the unchanged run L0s.
    # Its absolute-path sort order differs from the run's canonical stem order.
    selected = tmp_path / "z-job-products" / "parquet" / context_inputs[9].name
    selected.parent.mkdir(parents=True)
    shutil.copyfile(context_inputs[9], selected)
    overlay = list(reversed(context_inputs[:9] + [selected] + context_inputs[10:]))
    overlay.append(selected)  # Overlay enumeration must not duplicate a physical condition.
    job = tb_l1.TBL1Processor("10B", tmp_path / "job-products")
    shutil.copyfile(source.layout.get_tb_l1_json("auto_peak_context.json"),
                    job.layout.get_tb_l1_json("auto_peak_context.json"))

    def no_parquet_reread(*args, **kwargs):
        pytest.fail("A cache hit must not decode peer parquet files again")

    monkeypatch.setattr(pq, "ParquetFile", no_parquet_reread)
    assert job._learn_peak_guides(overlay, config, [0]) == original
    assert len(guide_calls) == 1


def test_l0_content_change_invalidates_a_relocated_cache(tmp_path, context_inputs, guide_calls):
    processor = tb_l1.TBL1Processor("10B", tmp_path / "products")
    config = {"histogram_config": HISTOGRAM}
    processor._learn_peak_guides(context_inputs, config, [0])
    cache_path = processor.layout.get_tb_l1_json("auto_peak_context.json")
    before = json.loads(cache_path.read_text(encoding="utf-8"))
    write_input(context_inputs[5], 5, offset=40)
    processor._learn_peak_guides(list(reversed(context_inputs)), config, [0])
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(guide_calls) == 2
    assert before["signature"] != after["signature"]
    old_input = next(row for row in before["inputs"] if row["stem"] == "spectrum-05")
    new_input = next(row for row in after["inputs"] if row["stem"] == "spectrum-05")
    assert old_input["sha256"] != new_input["sha256"]
    assert not np.array_equal(guide_calls[0]["spectra"][5], guide_calls[1]["spectra"][5])


@pytest.mark.parametrize("kind", ["corrupt_parquet", "failed_crc", "missing_amplitude", "invalid_metadata"])
def test_bad_unselected_context_peer_does_not_abort_a_valid_selected_file(
    tmp_path, context_inputs, guide_calls, monkeypatch, kind,
):
    tb_l1.plt.switch_backend("Agg")
    bad_peer = tmp_path / "peer-l0" / "unselected-bad.l0.parquet"
    write_input(bad_peer, kind=kind)
    processor = tb_l1.TBL1Processor("10B", tmp_path / "products")
    fitted_stems = []

    def fit_selected(stem, channel, amplitudes, windows, auto_window, settings, histogram):
        fitted_stems.append(stem)
        assert channel == 0 and len(amplitudes) == 16
        assert settings["expected_center"] == 120.0
        return SimpleNamespace(
            center=120.0, center_err=.1, sigma=8.0, sigma_err=.1,
            amplitude=1000.0, amplitude_err=10.0, fwhm=18.84, height=50.0,
            lo=95.0, hi=145.0, fit_mode="gaus", rsquared=.99,
            peak_detection={}, fit_quality={"accepted": True}, histogram_config=HISTOGRAM,
            slope=None, intercept=None, exp_amplitude=None, exp_decay=None,
            quad_a=None, quad_b=None, quad_c=None,
        )

    monkeypatch.setattr(processor, "_fit_channel", fit_selected)
    monkeypatch.setattr(processor, "_plot_channel", lambda *args: None)
    result = processor.process(
        context_inputs[0].parent.parent,
        {"fit_channels": [0], "automatic_window": True, "histogram_config": HISTOGRAM,
         "peak_context_l0_paths": [bad_peer, *reversed(context_inputs)]},
        only_stems=["spectrum-00"],
    )
    # The context is cached for all three supported detector channels, even
    # when this operation fits only CH0 of one selected spectrum.
    assert len(guide_calls) == 3
    assert fitted_stems == ["spectrum-00"]
    assert result["n_processed"] == result["channels_fitted"] == 1
    assert result["channels_failed"] == 0
    assert result["errors"] == []
    assert result["outputs"][0]["channels_updated"] == [0]
    saved = json.loads(processor.layout.get_tb_l1_json("spectrum-00_fit_results.l1.json").read_text(encoding="utf-8"))
    assert saved[0]["peak_detection"]["gain_guide"]["target_excluded"] is True
    cache = json.loads(processor.layout.get_tb_l1_json("auto_peak_context.json").read_text(encoding="utf-8"))
    assert list(cache["guides"]) == [f"spectrum-{index:02}" for index in range(16)]
    assert len(cache["skipped_inputs"]) == 1
    assert cache["skipped_inputs"][0]["stem"] == "unselected-bad"
    assert cache["skipped_inputs"][0]["error"]
