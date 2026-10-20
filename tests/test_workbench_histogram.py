"""High-ADC payload spectra use the same bins and bounds in UI and fitting."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common import legacy_parser
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.workbench.acquisition import default_profile, tb_histogram_config
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench

HIGH_HISTOGRAM = {"min": 0.0, "max": 16384.0, "bin_width": 6.0}


def seed_l0(root, payload="10B", *, acquisition=None, samples=None):
    if samples is None:
        rng = np.random.default_rng(161)
        samples = np.r_[rng.normal(2200, 85, 4000), rng.uniform(1300, 3000, 600)]
    frame = pd.DataFrame({"channel": np.repeat(np.arange(4), len(samples)),
                          "amp": np.tile(samples, 4),
                          "utc": np.tile(np.linspace(0, 60, len(samples)), 4)})
    path = root / payload / "tb/L0/parquet/case.l0.parquet"
    write_parquet(path, frame, metadata={"temp": [25.0] * 4, "bias": [28.5] * 4},
                  meta_key="hk_data", schema_metadata={"acquisition": acquisition} if acquisition else None)
    return path, frame


def seed_l1(root, payload="10B", *, histogram=None, stem="case", accepted=True):
    records = [{
        "channel": channel, "center": 2200.0, "center_err": 1.0,
        "sigma": 85.0, "sigma_err": 1.0, "amplitude": 10000.0, "amplitude_err": 100.0,
        "temp": 25.0, "temp_err": 0.1, "bias": 28.5, "bias_err": 0.01,
        "window_lo": 1850.0, "window_hi": 2550.0, "fit_mode": "gaus", "rsquared": 0.999,
        "fit_quality": {"accepted": accepted, "status": "resolved" if accepted else "rejected"},
        **({"histogram_config": histogram} if histogram else {}),
    } for channel in range(4)]
    write_json(root / payload / "tb/L1/json" / f"{stem}_fit_results.l1.json", records)
    return records


def test_high_adc_l0_display_uses_acquisition_histogram_without_cutting_at_1200(tmp_path):
    products = tmp_path / "runs/display/products"
    samples = np.array([1800.0, 2200.0, 2300.0, 14000.0, 16000.0])
    seed_l0(products, acquisition={"fitting": {"tb_histogram": HIGH_HISTOGRAM}}, samples=samples)
    spectrum = ComputeAdapter(tmp_path).spectrum("10B", products, "case", 0, "TB_L0")
    assert spectrum["histogram_config"] == HIGH_HISTOGRAM
    assert max(spectrum["x"]) > 16000
    assert sum(spectrum["y"]) == len(samples)
    assert spectrum["n_events"] == len(samples)


def test_saved_fit_histogram_takes_precedence_over_newer_l0_histogram(tmp_path):
    products = tmp_path / "runs/display/products"
    seed_l0(products, acquisition={"fitting": {"tb_histogram": {"min": 0, "max": 4096, "bin_width": 2}}},
            samples=np.array([2200.0, 16000.0]))
    seed_l1(products, histogram=HIGH_HISTOGRAM)
    spectrum = ComputeAdapter(tmp_path).spectrum("10B", products, "case", 0)
    assert spectrum["histogram_config"] == HIGH_HISTOGRAM
    assert sum(spectrum["y"]) == 2
    assert spectrum["curve"]["x"]
    assert min(spectrum["curve"]["x"]) > 1200


@pytest.mark.parametrize("payload", ["14B", "15B"])
def test_original_payload_histogram_keeps_1200_limit(payload, tmp_path):
    assert tb_histogram_config(default_profile(payload)) == {"min": 0, "max": 1200, "bin_width": 1}
    products = tmp_path / "runs/display/products"
    seed_l0(products, payload, samples=np.array([300.0, 1500.0]))
    spectrum = ComputeAdapter(tmp_path).spectrum(payload, products, "case", 0, "TB_L0")
    assert max(spectrum["x"]) <= 1200
    assert sum(spectrum["y"]) == 1


def test_high_adc_window_can_be_saved_and_reaches_the_actual_processor(tmp_path):
    history = tmp_path / "products"
    seed_l0(history)
    seed_l1(history)
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(product_dir=str(history / "10B"))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        service.save_acquisition(run["id"], default_profile("10B"), "10B test protocol")
        state = service.read(run["id"])
        seed_l0(history, acquisition={"profile_sha256": state["acquisition"]["sha256"],
                                      "fitting": {"tb_histogram": HIGH_HISTOGRAM}})
        bounds = {"lo": 1910.125, "hi": 2490.875}
        updated = service.patch(run["id"], {"windows": {"case": {"0": bounds}}})
        assert updated["config"]["windows"]["case"]["0"] == bounds
        adapter = service.adapter_for(service.read(run["id"]))
        products = service.root(run["id"]) / "products"
        window_file = adapter._windows("10B", updated["config"], products)
        assert read_json(window_file)["case"]["0"] == {**bounds, "manual": True}
        result = adapter.execute("10B", "TB_L1", products,
                                 {**updated["config"], "representative": False, "only": ["case"]})
        assert result["success"], result["errors"]
        records = read_json(products / "10B/tb/L1/json/case_fit_results.l1.json")
        fit = next(record for record in records if record["channel"] == 0)
        assert fit["window_lo"] == bounds["lo"]
        assert fit["window_hi"] == bounds["hi"]
        assert abs(fit["center"] - 2200) < 15
        assert fit["histogram_config"] == HIGH_HISTOGRAM
        spectrum = service.spectrum(run["id"], "case", 0)
        assert spectrum["histogram_config"] == HIGH_HISTOGRAM
        assert spectrum["draft_window"] == bounds
        state_path = service.root(run["id"]) / "state.json"
        before = state_path.read_bytes()
        with pytest.raises(ValueError):
            service.patch(run["id"], {"windows": {"case": {"0": {"lo": 16000, "hi": 17000}}}})
        assert state_path.read_bytes() == before
    finally:
        service.close()


def test_explicitly_rejected_high_r_squared_point_is_not_used_by_surface(tmp_path):
    products = tmp_path / "runs/display/products"
    seed_l1(products, accepted=False)
    seed_l1(products, stem="accepted", accepted=True)
    params = {"G0": 0.012, "k": 0.0184, "V0": 24.11, "b": 54.31, "c": 23459.83,
              "G0_err": 0.001, "k_err": 0.001, "V0_err": 0.01,
              "b_err": 0.1, "c_err": 1.0, "chisquare": 1.0}
    write_json(products / "10B/tb/L2/json/2610201200_10B_TB.l2.json", [params] * 4)
    surface = ComputeAdapter(tmp_path).surface("10B", products, 0)
    points = {point["stem"]: point for point in surface["points"]}
    assert points["case"]["rsquared"] > 0.99
    assert points["case"]["used"] is False
    assert points["accepted"]["used"] is True
    assert surface["diagnostics"]["n_points"] == 1


@pytest.mark.parametrize("crc", [[False] * 4, [False] * 3, []])
def test_crc_failures_or_incomplete_checks_never_become_accepted_science_input(tmp_path, monkeypatch, crc):
    event = {"channel_n": np.zeros(4), "data_max": np.full(4, 300.0),
             "data_base": np.full(4, 100.0), "crc_check": np.array(crc, dtype=bool)}

    def parse(_parse_fn, _path, tag, _xml_path, **_options):
        return event if tag == "grid1x_ft_packet" else {}

    monkeypatch.setattr(legacy_parser, "_parse_silent", parse)
    profile = default_profile("14B")
    profile["parser"]["conditions"] = {"mode": "fixed", "temp": 20, "bias": 28.5}
    with pytest.raises(ValueError, match="CRC"):
        legacy_parser.parse_event_hk_to_dataframe(
            tmp_path / "event.dat", tmp_path / "hk.hk", tmp_path,
            "grid1x_ft_packet", "hk_packet", False, input_profile=profile,
        )


def test_xml_repeat_inference_matches_the_actual_584_and_528_packet_geometry():
    resources = Path(legacy_parser.__file__).resolve().parents[1] / "resources/legacy"
    assert legacy_parser.packet_repeat_options(resources / "hnu_packet.xml", "grid1x_ft_packet") == {
        "multi_evt": 38, "multi_step": 14,
    }
    assert legacy_parser.packet_repeat_options(resources / "grid_packet.xml", "grid1x_ft_packet") == {
        "multi_evt": 41, "multi_step": 12,
    }
    json.dumps(default_profile("14B"), allow_nan=False)
