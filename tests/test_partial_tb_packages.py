"""A deferred detector channel must not block the remaining calibration chain."""

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor
from calibration_lib.workbench.dependencies import tb_package

PARAMETERS = {"G0": .01, "k": .02, "V0": 24., "b": 50., "c": 23000.}


def test_zero_placeholder_is_only_allowed_for_explicitly_deferred_channels(tmp_path):
    path = tmp_path / "TB.l2.json"
    rows = [dict(PARAMETERS) for _ in range(3)] + [{key: 0. for key in PARAMETERS}]
    write_json(path, rows)
    with pytest.raises(ValueError, match="G0"):
        tb_package(path)
    assert tb_package(path, required_channels=(0, 1, 2))["path"] == str(path.resolve())
    rows[1]["G0"] = 0
    write_json(path, rows)
    with pytest.raises(ValueError, match="G0"):
        tb_package(path, required_channels=(0, 1, 2))


def test_legacy_plastic_peak_is_not_collected_when_temperature_comes_from_l0(tmp_path):
    rows = [{"channel": ch, "center": 500., "center_err": 1., "rsquared": .99} for ch in range(4)]
    write_json(tmp_path / "TEST/tb/L1/json/case_fit_results.l1.json", rows)
    write_parquet(tmp_path / "TEST/tb/L0/parquet/case.l0.parquet", pd.DataFrame({"amp": [1.]}),
                  metadata={"temp": [25.] * 4, "bias": [28.] * 4}, meta_key="hk_data")
    points = TBL2Processor("TEST", tmp_path, response_payload="10B")._collect_fit_data(
        tmp_path / "TEST/tb/L1", tmp_path / "TEST/tb/L0")
    assert {row["channel"] for row in points} == {0, 1, 2}


def test_ec_l2_processes_the_three_corrected_photopeak_channels(tmp_path):
    rng = np.random.default_rng(983)
    source, background = [], []
    for ch in range(3):
        amp = np.r_[rng.normal(120., 8., 5000), rng.uniform(0, 240, 1000)]
        source.append(pd.DataFrame({"channel": ch, "amp_corr": amp, "utc": np.linspace(0, 120, len(amp))}))
        background.append(pd.DataFrame({"channel": ch, "amp_corr": rng.uniform(0, 240, 1000),
                                        "utc": np.linspace(0, 120, 1000)}))
    root = tmp_path / "TEST/ec/L1"
    write_parquet(root / "parquet/src_Test_2m_corr.l1.parquet", pd.concat(source),
                  metadata={"peak_energies_keV": [60.], "exposure_minutes": 2}, meta_key="tb_correction")
    write_parquet(root / "parquet/src_bkg_2m_corr.l1.parquet", pd.concat(background))
    result = ECL2Processor("TEST", tmp_path, response_payload="10B").process(root, {
        "automatic_window": True, "peak_detection": {"expected_center": 120., "expected_energy_keV": 60.}})
    assert not result["errors"], result["errors"]
    assert result["n_processed"] == 1
    records = read_json(tmp_path / "TEST/ec/L2/json/src_Test_fit_results.l2.json")
    fitted = [row for row in records if not row.get("placeholder")]
    assert {row["channel"] for row in fitted} == {0, 1, 2}
    assert all(abs(row["center"] - 120.) < 2 for row in fitted)
    deferred = [row for row in records if row.get("placeholder")]
    assert all(row["channel"] == 3 for row in deferred)
    assert all(row["fit_performed"] is False and row["center"] == 0 for row in deferred)
    assert all(row["fit_quality"]["accepted"] is False and row["fit_quality"]["status"] == "model_pending"
               for row in deferred)
