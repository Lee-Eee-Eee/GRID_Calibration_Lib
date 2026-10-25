"""Scientific interface regressions using real parquet I/O and peak fitting."""

from dataclasses import replace

import numpy as np
import pandas as pd
from calibration_lib.common.legacy_parser import _find_parser_resource_dir
from calibration_lib.common.utils import (
    read_json,
    read_parquet,
    read_parquet_metadata,
    write_json,
    write_parquet,
)
from calibration_lib.config.payload_config import get_payload_config
from calibration_lib.ec.processors.L1_processor import ECL1Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.workbench.compute import ComputeAdapter


def test_bundled_parser_resolves_independently_of_output_workspace(tmp_path):
    resources = _find_parser_resource_dir(tmp_path / "unrelated-data-workspace")
    assert (resources / "hnu_packet.xml").is_file()
    assert (resources / "parse_grid_data.py").is_file()


COEF_NAMES = ("EC_low", "EC_high", "resolution_low", "resolution_high")


def assert_channel_schema(record, channel):
    assert record["channel"] == channel
    for name in COEF_NAMES:
        assert name in record and f"{name}_err" in record
        assert record[name] is None or len(record[name]) == 3
        assert len(record[f"{name}_err"]) == 3
        if record[name] is None:
            assert record[f"{name}_err"] == [None, None, None]


def test_ec_background_records_the_exact_tb_dependency_and_excludes_failed_crc(tmp_path):
    products = tmp_path / "products"
    frame = pd.DataFrame(
        {
            "amp": [100.0, 200.0, 300.0, 400.0],
            "channel": [0, 1, 2, 3],
            "utc": [0.0, 1.0, 2.0, 3.0],
            "temp": [25.0] * 4,
            "bias": [28.5] * 4,
            "crc_check": [True, False, True, False],
        }
    )
    write_parquet(products / "14B/ec/L0/parquet/src_bkg_30m.l0.parquet", frame)
    tb_params = [{"G0": 0.001, "k": 0.0184, "V0": 24.11, "b": 54.31, "c": 23459.83}] * 4
    dependency = tmp_path / "2601010000_14B_TB.l2.json"
    write_json(dependency, tb_params)
    result = ECL1Processor("14B", products).process(
        replace(get_payload_config("14B"), source_specs=[]), dependency
    )
    assert not result["errors"]
    assert result["n_processed"] == 1
    assert result["outputs"][0]["data_type"] == "source_bkg"
    output = products / "14B/ec/L1/parquet/src_bkg_30m_corr.l1.parquet"
    actual = read_parquet(output)
    np.testing.assert_allclose(actual["amp_corr"], frame.loc[frame.crc_check, "amp"])
    assert actual["crc_check"].tolist() == [True, True]
    assert result["outputs"][0]["input_quality"]["excluded_crc_count"] == 2
    metadata = read_parquet_metadata(output, "tb_correction")
    assert metadata["tb_corrfile"] == dependency.name
    assert metadata["tb_params_used"] == {str(ch): tb_params[ch] for ch in range(4)}


def test_actual_l1_fitter_honors_saved_windows_and_spectrum_linkage(tmp_path):
    history = tmp_path / "products/14B/tb/L0/parquet/fixture.l0.parquet"
    rng = np.random.default_rng(721)
    frames = [
        pd.DataFrame(
            {
                "amp": rng.normal(300 + ch * 8, 12, 1400),
                "channel": ch,
                "utc": np.arange(1400),
                "crc_check": True,
            }
        )
        for ch in range(4)
    ]
    write_parquet(
        history,
        pd.concat(frames, ignore_index=True),
        metadata={"temp": [20.0] * 4, "bias": [28.5] * 4, "temp_err": [0.1] * 4, "bias_err": [0.01] * 4},
        meta_key="hk_data",
    )
    adapter = ComputeAdapter(tmp_path)
    output = tmp_path / "runs/test/products"
    windows = {"fixture": {str(ch): {"lo": 250, "hi": 370} for ch in range(4)}}
    result = adapter.execute("14B", "TB_L1", output, {"only": ["fixture.l0.parquet"], "windows": windows})
    assert result["success"], result["errors"]
    assert result["processor"]["n_processed"] == 1
    records = read_json(output / "14B/tb/L1/json/fixture_fit_results.l1.json")
    assert len(records) == 4
    for ch, record in enumerate(records):
        assert abs(record["center"] - (300 + ch * 8)) < 2
        assert record["window_lo"] == 250 and record["window_hi"] == 370
        assert record["fit_mode"] in {"gaus", "lin + gaus", "exp + gaus", "quad + gaus"}
    spectrum = adapter.spectrum("14B", output, "fixture", 2)
    assert spectrum["n_events"] == 1400
    assert spectrum["fit"]["center"] == records[2]["center"]
    assert len(spectrum["curve"]["x"]) == len(spectrum["curve"]["y"])
    assert sum(spectrum["y"]) == 1400
    assert spectrum["source"] == "run"


def l2_points(channels):
    return [
        {
            "channel": ch,
            "E": energy,
            "center": energy * 6 + ch,
            "center_err": 1.0,
            "sigma": 5.0,
            "sigma_err": 0.5,
            "resolution": 0.1,
            "resolution_err": 0.01,
            "fwhm": 11.7,
            "source": "x",
            "rsquared": 0.99,
        }
        for ch in channels
        for energy in (40.0, 45.0)
    ]


def test_l3_insufficient_segments_keep_explicit_null_error_vectors(tmp_path, monkeypatch):
    products = tmp_path / "products"
    l2 = products / "14B/ec/L2"
    write_json(l2 / "json/fixture_fit_results.l2.json", l2_points(range(4)))
    processor = ECL3Processor("14B", products)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *args, **kwargs: None)
    result = processor.process(l2)
    assert not result["errors"]
    summary = read_json(products / "14B/ec/L3/json" / result["output_json"])
    for channel in range(4):
        record = summary["calibration_params"][str(channel)]
        assert_channel_schema(record, channel)
        assert all(record[name] is None for name in COEF_NAMES)


def test_l3_missing_channels_report_failure_without_breaking_summary_schema(tmp_path, monkeypatch):
    products = tmp_path / "products"
    l2 = products / "14B/ec/L2"
    write_json(l2 / "json/fixture_fit_results.l2.json", l2_points([0]))
    processor = ECL3Processor("14B", products)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *args, **kwargs: None)
    result = processor.process(l2)
    assert len(result["errors"]) == 3
    for channel in range(4):
        assert_channel_schema(result["calibration_params"][str(channel)], channel)
