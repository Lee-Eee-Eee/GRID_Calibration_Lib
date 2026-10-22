"""A failed channel must leave the other real fits usable and inspectable."""

import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench, peak_job_settings


def seed(products, stem, profile_sha=None):
    rng = np.random.default_rng(20261022)
    amp = np.r_[rng.normal(2200, 85, 5000), rng.uniform(1300, 3000, 800)]
    frame = pd.DataFrame({"channel": np.repeat(np.arange(4), len(amp)),
                          "amp": np.tile(amp, 4), "crc_check": True,
                          "utc": np.tile(np.linspace(0, 60, len(amp)), 4)})
    write_parquet(products / f"10B/tb/L0/parquet/{stem}.l0.parquet", frame,
                  metadata={"temp": [25.] * 4, "temp_err": [0.1] * 4,
                            "bias": [28.5] * 4, "bias_err": [0.01] * 4}, meta_key="hk_data",
                  schema_metadata={"acquisition": {"profile_sha256": profile_sha, "fitting": {"tb_histogram":
                                   {"min": 0, "max": 16384, "bin_width": 6}}}})


def wait(service, run_id, job):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = service.job(run_id, job["id"])
        if current["status"] not in ("queued", "running"):
            return current
        time.sleep(0.02)
    pytest.fail("L1 job timed out")


def test_current_channel_refit_merges_untouched_channels_and_skips_their_bad_windows(tmp_path):
    products = tmp_path / "products"
    seed(products, "case")
    processor = TBL1Processor("10B", products)
    before_figures = plt.get_fignums()
    first = processor.process(products / "10B/tb/L0")
    assert first["channels_fitted"] == 4
    path = products / "10B/tb/L1/json/case_fit_results.l1.json"
    before = read_json(path)
    windows = tmp_path / "windows.json"
    write_json(windows, {"case": {"0": {"lo": 1930.125, "hi": 2480.875},
                                 "3": {"lo": 12000, "hi": 13000}}})
    second = processor.process(products / "10B/tb/L0", {"fit_channels": [0],
                               "fit_windows_config": str(windows)}, only_stems=["case.l0"])
    assert second["channels_fitted"] == 1 and second["errors"] == []
    assert second["outputs"][0]["channels_updated"] == [0]
    after = read_json(path)
    assert after[1:] == before[1:]
    assert after[0]["window_lo"] == 1930.125
    assert after[0]["window_hi"] == 2480.875
    assert second["outputs"][0]["figure"] == "case_ch0_fit.l1.jpg"
    assert plt.get_fignums() == before_figures


def test_mixed_batch_publishes_good_channels_and_records_exact_failure_and_progress(tmp_path):
    products = tmp_path / "products"
    seed(products, "good")
    seed(products, "bad")
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(product_dir=str(products / "10B"))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        service.patch(run["id"], {"windows": {"bad": {"0": {"lo": 500, "hi": 600}}}})
        job = wait(service, run["id"], service.submit(run["id"], {"layer": "TB_L1", "representative": False}))
        assert job["status"] == "partial", job.get("error")
        assert job["result"]["summary"] == {"files_total": 2, "channels_succeeded": 7, "channels_failed": 1}
        errors = job["result"]["errors"]
        assert len(errors) == 1 and errors[0]["stem"] == "bad" and errors[0]["channel"] == 0
        output = service.root(run["id"]) / "products/10B/tb/L1/json"
        assert len(read_json(output / "good_fit_results.l1.json")) == 4
        assert [row["channel"] for row in read_json(output / "bad_fit_results.l1.json")] == [1, 2, 3]
        assert service.spectrum(run["id"], "good", 0)["last_fit_attempt"]["status"] == "succeeded"
        assert service.spectrum(run["id"], "bad", 1)["last_fit_attempt"]["status"] == "succeeded"
        assert service.spectrum(run["id"], "bad", 0)["last_fit_attempt"]["status"] == "failed"
        state = service.read(run["id"])
        assert state["pending_windows"] == ["10B:bad:0"]
        assert "TB_L1" in state["dirty_layers"]
        log = (service.root(run["id"]) / "jobs" / job["id"] / "processor.log").read_text(encoding="utf-8")
        assert 'L1_PROGRESS {"completed": 2, "total": 2' in log
        assert not plt.get_fignums()
    finally:
        service.close()


@pytest.mark.parametrize("channels", [[], [True], [-1], [4], [0.5], "0"])
def test_invalid_channel_scope_fails_before_job_creation(channels):
    with pytest.raises(ValueError, match="fit_channels"):
        peak_job_settings("TB_L1", {"fit_channels": channels})


def test_channel_scope_is_specific_to_tb_l1():
    assert peak_job_settings("TB_L1", {"fit_channels": [2, 0, 2]}) == {"fit_channels": [0, 2]}
    with pytest.raises(ValueError, match="fit_channels"):
        peak_job_settings("EC_L2", {"fit_channels": [0]})


def test_four_current_channels_must_accumulate_after_reading_revision(tmp_path):
    products = tmp_path / "products"
    seed(products, "case")
    TBL1Processor("10B", products).process(products / "10B/tb/L0")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "case.event.bin").write_bytes(b"test raw event")
    (raw / "case.hk.bin").write_bytes(b"test raw hk")
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(products / "10B"), payload="10B")["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        profile = service.acquisition(run["id"])["profile"]
        profile["discovery"].update(mode="manifest", manifest=[
            {"kind": "tb", "stem": "case", "event": "case.event.bin", "hk": "case.hk.bin"}])
        service.save_acquisition(run["id"], profile)
        sha = service.read(run["id"])["acquisition"]["sha256"]
        seed(products, "case", sha)
        for channel in range(4):
            job = wait(service, run["id"], service.submit(run["id"],
                       {"layer": "TB_L1", "stem": "case", "representative": False, "fit_channels": [channel]}))
            assert job["status"] == "succeeded", job.get("error")
            state = service.read(run["id"])
            assert state["acquisition_l1_channels"]["case"] == list(range(channel + 1))
            assert service.acquisition_coverage(state, "TB_L1")["current"] is (channel == 3)
        # Three retained, complete old records do not count as newly read.
        profile["fitting"]["tb_histogram"]["bin_width"] = 8
        service.save_acquisition(run["id"], profile)
        sha = service.read(run["id"])["acquisition"]["sha256"]
        seed(service.root(run["id"]) / "products", "case", sha)
        job = wait(service, run["id"], service.submit(run["id"],
                   {"layer": "TB_L1", "stem": "case", "representative": False, "fit_channels": [0]}))
        assert job["status"] == "succeeded", job.get("error")
        state = service.read(run["id"])
        assert state["acquisition_l1_channels"]["case"] == [0]
        assert not service.acquisition_coverage(state, "TB_L1")["current"]
        records = read_json(service.root(run["id"]) / "products/10B/tb/L1/json/case_fit_results.l1.json")
        assert len(records) == 4
        assert records[0]["acquisition_profile_sha256"] == sha
        assert all(record["acquisition_profile_sha256"] != sha for record in records[1:])
    finally:
        service.close()


def test_legacy_parquet_stem_is_selected_and_preserved(tmp_path):
    products = tmp_path / "products"
    seed(products, "case")
    parquet = products / "10B/tb/L0/parquet/case.l0.parquet"
    parquet.rename(parquet.with_name("case.parquet"))
    result = TBL1Processor("10B", products).process(products / "10B/tb/L0",
                                                  {"fit_channels": [0]}, only_stems=["case.l0"])
    assert result["errors"] == []
    assert result["outputs"][0]["stem"] == "case"
    assert (products / "10B/tb/L1/json/case_fit_results.l1.json").is_file()


def test_preparing_fit_windows_does_not_read_unfitted_spectra_metadata(tmp_path, monkeypatch):
    products = tmp_path / "products"
    seed(products, "case")
    seed(products, "unfitted")
    write_json(products / "10B/tb/L1/json/case_fit_results.l1.json", [
        {"channel": 0, "window_lo": 1900.125, "window_hi": 2500.875}])

    def unexpected_metadata_read(*_args, **_kwargs):
        raise AssertionError("Preparing saved fit windows does not require L0 metadata")

    monkeypatch.setattr("calibration_lib.workbench.compute.read_parquet_metadata", unexpected_metadata_read)
    path = ComputeAdapter(tmp_path)._windows("10B", {}, products)
    assert read_json(path)["case"]["0"] == {"lo": 1900.125, "hi": 2500.875, "manual": True}


def test_failed_old_fit_remains_rejected_after_other_file_success_and_recovers_only_itself(tmp_path):
    products = tmp_path / "products"
    raw = tmp_path / "raw"
    raw.mkdir()
    for stem in ("case", "other"):
        seed(products, stem)
        (raw / f"{stem}.event.bin").write_bytes(b"test raw event")
        (raw / f"{stem}.hk.bin").write_bytes(b"test raw hk")
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(products / "10B"), payload="10B")["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        profile = service.acquisition(run["id"])["profile"]
        profile["discovery"].update(mode="manifest", manifest=[
            {"kind": "tb", "stem": stem, "event": f"{stem}.event.bin", "hk": f"{stem}.hk.bin"}
            for stem in ("case", "other")])
        service.save_acquisition(run["id"], profile)
        sha = service.read(run["id"])["acquisition"]["sha256"]
        for stem in ("case", "other"):
            seed(products, stem, sha)
        first = wait(service, run["id"], service.submit(run["id"], {"layer": "TB_L1", "representative": False}))
        assert first["status"] == "succeeded", first.get("error")
        output = service.root(run["id"]) / "products"
        path = output / "10B/tb/L1/json/case_fit_results.l1.json"
        before = read_json(path)
        service.patch(run["id"], {"windows": {"case": {"0": {"lo": 500, "hi": 600}}}})
        partial = wait(service, run["id"], service.submit(run["id"],
                       {"layer": "TB_L1", "stem": "case", "representative": False}))
        assert partial["status"] == "partial", partial.get("error")
        assert partial["invalidated_l1_points"] == ["10B:case:0"]
        records = read_json(path)
        assert records[0]["center"] == before[0]["center"]
        assert records[0]["fit_quality"]["accepted"] is False
        assert records[0]["fit_quality"]["status"] == "fit_failed"
        assert records[0]["previous_fit_quality"] == before[0]["fit_quality"]
        assert read_json(service.root(run["id"]) / "jobs" / partial["id"] / "previous-results" / path.name)[0]["fit_quality"] == before[0]["fit_quality"]
        other = wait(service, run["id"], service.submit(run["id"],
                     {"layer": "TB_L1", "stem": "other", "fit_channels": [0], "representative": False}))
        assert other["status"] == "succeeded", other.get("error")
        state = service.read(run["id"])
        assert "10B:case:0" in state["tb_l1_failures"]
        assert state["acquisition_l1_channels"]["case"] == [1, 2, 3]
        assert "case" not in state["acquisition_products"]["TB_L1"]
        assert "TB_L1" in state["dirty_layers"]
        collected = TBL2Processor("10B", output)._collect_fit_data(output / "10B/tb/L1", products / "10B/tb/L0")
        assert not any(row["file"] == "case" and row["channel"] == 0 for row in collected)
        assert any(row["file"] == "other" and row["channel"] == 0 for row in collected)
        # Rejected weak points do not force a manual mask for every spectrum.
        # This tiny fixture reaches the real L2 processor, which reports that
        # its surface has too few points instead of reusing the failed old fit.
        aggregate = wait(service, run["id"], service.submit(run["id"], {"layer": "TB_L2", "representative": False}))
        assert aggregate["status"] == "failed"
        assert any("Channel 0: requires at least 8 usable points, got 1" in error
                   for error in aggregate["result"]["processor"]["errors"])
        repaired = wait(service, run["id"], service.submit(run["id"],
                        {"layer": "TB_L1", "stem": "case", "fit_channels": [0],
                         "automatic_window": True, "representative": False}))
        assert repaired["status"] == "succeeded", repaired.get("error")
        state = service.read(run["id"])
        assert "10B:case:0" not in state["tb_l1_failures"]
        assert state["acquisition_l1_channels"]["case"] == [0, 1, 2, 3]
        assert read_json(path)[0]["fit_quality"]["accepted"] is True
    finally:
        service.close()


def test_failed_global_channel_scope_quarantines_all_existing_scope_without_new_fits(tmp_path):
    products = tmp_path / "products"
    for stem in ("case", "other"):
        seed(products, stem)
    TBL1Processor("10B", products).process(products / "10B/tb/L0")
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(product_dir=str(products / "10B"))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        state = service.read(run["id"])
        job = {"id": "global-failure", "layer": "TB_L1", "scope": "all", "status": "failed",
               "config": {"fit_channels": [2]}, "error": "input read failed"}
        service._record_tb_l1_attempt(state, job)
        assert set(state["tb_l1_failures"]) == {"10B:case:2", "10B:other:2"}
        assert set(job["invalidated_l1_points"]) == {"10B:case:2", "10B:other:2"}
        assert len(job["outputs"]) == 2
        import hashlib
        for artifact in job["outputs"]:
            path = service.root(run["id"]) / "products" / artifact["path"]
            attempt = service.root(run["id"]) / "jobs" / job["id"] / "products" / artifact["path"]
            assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            assert attempt.read_bytes() == path.read_bytes()
            records = read_json(path)
            assert records[2]["fit_quality"]["status"] == "fit_failed"
            assert all(record["fit_quality"]["accepted"] for record in records if record["channel"] != 2)
    finally:
        service.close()
