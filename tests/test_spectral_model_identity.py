"""A run's output label does not change its physical detector response."""

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import write_json, write_parquet
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.datasets import DatasetCatalog
from calibration_lib.workbench.l1_review import overview
from calibration_lib.workbench.service import Workbench

REASON = "塑料闪烁体 · 康普顿响应，拟合方案待定"


def seed_products(root, payload):
    histogram = {"min": 0, "max": 16384, "bin_width": 6}
    frame = pd.DataFrame({"channel": np.repeat(np.arange(4), 12),
                          "amp": np.tile(np.linspace(1900, 2500, 12), 4),
                          "crc_check": True})
    write_parquet(root / payload / "tb/L0/parquet/case.l0.parquet", frame,
                  metadata={"temp": [25.] * 4, "bias": [28.5] * 4}, meta_key="hk_data",
                  schema_metadata={"acquisition": {"fitting": {"tb_histogram": histogram}}})
    write_json(root / payload / "tb/L1/json/case_fit_results.l1.json", [
        {"channel": ch, "center": 2200., "sigma": 85., "amplitude": 1000.,
         "window_lo": 1900., "window_hi": 2500., "fit_mode": "gaus",
         "histogram_config": histogram, "rsquared": .99, "fit_quality": {"accepted": True}}
        for ch in range(4)])


def test_compute_renamed_payload_keeps_dataset_response_and_hides_old_gaussian(tmp_path):
    history = tmp_path / "historical"
    seed_products(history, "10B")
    dataset = DatasetCatalog(tmp_path).resolve({"payload": "TEST-10", "profile": "10B",
                                               "product_dir": str(history / "10B")})
    adapter = ComputeAdapter(tmp_path, dataset)
    assert adapter.detector_payload("TEST-10") == "10B"
    products = tmp_path / "runs/review/products"
    seed_products(products, "TEST-10")
    points = adapter.points("TEST-10", products)
    plastic = next(point for point in points if point["channel"] == 3)
    assert plastic["fit_status"] == "model_pending" and plastic["center"] is None
    assert plastic["unsupported_spectral_model"] == REASON
    assert all(point["fit_status"] == "fitted" for point in points if point["channel"] < 3)
    spectrum = adapter.spectrum("TEST-10", products, "case", 3)
    assert spectrum["unsupported_spectral_model"] == REASON
    assert spectrum["fit"] is None and spectrum["curve"] is None
    assert spectrum["n_events"] == 12 and sum(spectrum["y"]) == 12
    # An explicit manual window still cannot select Gaussian for this channel.
    with pytest.raises(ValueError, match="康普顿响应"):
        adapter.execute("TEST-10", "TB_L1", products,
                        {"representative": False, "only": ["case"], "fit_channels": [3],
                         "windows": {"case": {"3": {"lo": 100, "hi": 300}}}})


@pytest.mark.parametrize("payload", ["10B", "11B"])
def test_workbench_renamed_output_uses_preset_identity_before_dataset_profile(tmp_path, payload):
    history = tmp_path / "historical"
    seed_products(history, "10B")
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(product_dir=str(history / "10B"))["datasets"][0]
        run = service.create("TEST-10", dataset_id=dataset["id"])
        state = service.read(run["id"])
        # A reusable detector preset supplies physical identity even when the
        # editable output name and the directory-derived profile differ.
        state["dataset"]["profile"] = "14B"
        state["acquisition_preset"] = {"payload": payload}
        service.save(state)
        adapter = service.adapter_for(state)
        assert adapter.detector_payload("TEST-10") == payload
        spectrum = service.spectrum(run["id"], "case", 3)
        assert spectrum["unsupported_spectral_model"] == REASON
        assert spectrum["fit"] is None and spectrum["curve"] is None
        file = overview(service, run["id"])["files"][0]
        assert file["status"] == "fitted" and file["fitted"] == 3
        assert file["channels"][3]["fit_status"] == "model_pending"
        assert file["channels"][3]["center"] is None
        with pytest.raises(ValueError, match="康普顿响应"):
            TBL1Processor("TEST-10", service.root(run["id"]) / "products",
                          response_payload=adapter.detector_payload("TEST-10")).process(
                history / "10B/tb/L0", {"fit_channels": [3]})
    finally:
        service.close()
