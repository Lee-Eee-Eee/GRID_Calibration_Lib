import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from calibration_lib.workbench import l1_review
from calibration_lib.workbench.l1_review import overview


def test_overview_attributes_batch_failures_to_the_correct_file_and_channel(tmp_path):
    products = tmp_path / "products" / "12B" / "tb" / "L1" / "json"
    products.mkdir(parents=True)
    records = [{"channel": ch, "center": 1700 + ch, "rsquared": .99} for ch in range(4)]
    (products / "101_fit_results.l1.json").write_text(json.dumps(records))
    state = {
        "payload": "12B", "config": {}, "pending_windows": ["12B:101:0"],
        "jobs": [{"id": "job1", "layer": "TB_L1", "scope": "all", "config": {},
                  "status": "partial", "result": {"errors": [{
                      "stem": "101", "channel": 3, "error": "ambiguous peak",
                  }]}}],
    }
    adapter = SimpleNamespace(
        tb_files=lambda *_: {"094": Path("094.parquet"), "101": Path("101.parquet")},
        historical_layer=lambda *_: tmp_path / "history",
    )
    workbench = SimpleNamespace(read=lambda _: state, root=lambda _: tmp_path, adapter_for=lambda _: adapter)
    files = {file["stem"]: file for file in overview(workbench, "run")["files"]}
    assert files["094"]["channels"][0]["error"] is None
    assert files["101"]["fitted"] == 2
    assert files["101"]["status"] == "attention"
    assert files["101"]["channels"][3]["error"] == "ambiguous peak"
    assert files["101"]["channels"][0]["pending_window"]
    # A newer fit of CH0 must not erase the CH3 failure or apply itself to CH3.
    state["jobs"].append({"id": "job2", "layer": "TB_L1", "scope": "selected", "status": "succeeded",
                          "config": {"only": ["101"], "fit_channels": [0]}, "result": {"errors": []}})
    channels = overview(workbench, "run")["files"][1]["channels"]
    assert channels[0]["attempt"]["job_id"] == "job2"
    assert channels[3]["attempt"]["job_id"] == "job1"
    state["jobs"].append({"id": "job3", "layer": "TB_L1", "scope": "selected", "status": "failed",
                          "config": {"only": ["101"]}, "result": {"errors": [
                              {"stem": "101", "channel": None, "error": "unreadable event data"},
                          ]}})
    file = overview(workbench, "run")["files"][1]
    assert file["fitted"] == 0
    assert all(channel["error"] == "unreadable event data" for channel in file["channels"])
    state["jobs"].append({"id": "job4", "layer": "TB_L1", "scope": "selected", "status": "succeeded",
                          "config": {"only": ["101"]}, "result": {"errors": []}})
    file = overview(workbench, "run")["files"][1]
    assert file["status"] == "pending"
    assert file["fitted"] == 3


def test_overview_reads_old_job_result_for_a_failed_batch(tmp_path):
    directory = tmp_path / "jobs" / "old"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({"errors": [
        {"file": "101.l0.parquet", "error": "unresolved"},
    ]}))
    state = {"payload": "12B", "config": {}, "jobs": [
        {"id": "old", "layer": "TB_L1", "status": "failed", "scope": "all", "config": {}},
    ]}
    adapter = SimpleNamespace(tb_files=lambda *_: {"101": Path("l0")},
                              historical_layer=lambda *_: tmp_path / "history")
    workbench = SimpleNamespace(read=lambda _: state, root=lambda _: tmp_path, adapter_for=lambda _: adapter)
    file = overview(workbench, "run")["files"][0]
    assert file["status"] == "attention"
    assert file["channels"][0]["error"] == "unresolved"


def quality_workbench(tmp_path, records, *, payload="10B", jobs=None, failures=None, stem="m20C_290"):
    directory = tmp_path / "products" / payload / "tb" / "L1" / "json"
    directory.mkdir(parents=True)
    (directory / f"{stem}_fit_results.l1.json").write_text(json.dumps(records), encoding="utf-8")
    state = {"payload": payload, "config": {}, "jobs": jobs or [], "tb_l1_failures": failures or {}}
    adapter = SimpleNamespace(tb_files=lambda *_: {stem: tmp_path / f"{stem}.l0.parquet"},
                              historical_layer=lambda *_: tmp_path / "history")
    return SimpleNamespace(read=lambda _: state, root=lambda _: tmp_path, adapter_for=lambda _: adapter)


def test_quality_overview_maps_each_channel_to_measured_conditions_and_saved_metrics(tmp_path, monkeypatch):
    quality = {"accepted": False, "redchi": 4.75, "warnings": ["residual excess"], "status": "rejected"}
    records = [
        {"channel": 0, "temp": 19.625, "bias": 28.734, "center": 2200, "fit_quality": quality},
        {"channel": 1, "center": 2100, "fit_quality": {"accepted": True, "redchi": 0.0}},
        {"channel": 2, "center": 2000, "fit_quality": {"accepted": True}},
        {"channel": 3, "temp": 20.125, "bias": 28.731, "placeholder": True,
         "fit_quality": {"accepted": False, "status": "model_pending"}},
    ]
    measured = {"temp": [19.5, 19.75, 19.875, 20.0], "bias": [28.7, 28.71, 28.72, 28.73]}
    calls = []

    def metadata(path, key):
        calls.append((path, key))
        return measured

    monkeypatch.setattr(l1_review, "read_parquet_metadata", metadata)
    service = quality_workbench(tmp_path, records)
    channels = overview(service, "run")["files"][0]["channels"]
    assert [item["id"] for item in channels] == [f"10B:m20C_290:{channel}" for channel in range(4)]
    assert [item["channel"] for item in channels] == list(range(4))
    assert [item["temp"] for item in channels] == [19.625, 19.75, 19.875, 20.125]
    assert [item["bias"] for item in channels] == [28.734, 28.71, 28.72, 28.731]
    assert len(calls) == 1 and calls[0][1] == "hk_data"
    assert channels[0]["chi2_reduced"] == 4.75
    assert channels[0]["rejection_reasons"] == ["residual excess"]
    assert channels[0]["fit_quality"] == quality
    assert channels[0]["quality_accepted"] is False
    assert channels[1]["chi2_reduced"] == 0.0
    assert channels[1]["quality_accepted"] is True
    assert channels[2]["chi2_reduced"] is None
    assert channels[3]["fit_status"] == "model_pending"
    assert channels[3]["quality_accepted"] is None
    assert channels[3]["chi2_reduced"] is None and channels[3]["fit_quality"] is None
    assert channels[3]["rejection_reasons"] == []


def test_quality_overview_preserves_unknown_quality_and_missing_measured_conditions(tmp_path, monkeypatch):
    def no_hk(*_args):
        return {}

    monkeypatch.setattr(l1_review, "read_parquet_metadata", no_hk)
    service = quality_workbench(tmp_path, [{"channel": 0, "center": 2200, "rsquared": .999,
                                          "fit_quality": {"redchi": float("nan")}}])
    channels = overview(service, "run")["files"][0]["channels"]
    assert channels[0]["fit_status"] == "fitted"  # Existing legacy fit, with no saved verdict.
    assert channels[0]["quality_accepted"] is None
    assert channels[0]["chi2_reduced"] is None
    assert all(item["temp"] is None and item["bias"] is None for item in channels)
    assert channels[1]["fit_status"] == "not_fitted"
    assert channels[3]["fit_status"] == "model_pending"


def test_quality_overview_does_not_read_l0_when_record_conditions_are_complete(tmp_path, monkeypatch):
    def no_metadata(*_args):
        pytest.fail("Complete L1 conditions must not open L0")

    monkeypatch.setattr(l1_review, "read_parquet_metadata", no_metadata)
    records = [{"channel": channel, "temp": 0.0 + channel, "bias": 28.5,
                "center": 2100, "fit_quality": {"accepted": True}} for channel in range(4)]
    service = quality_workbench(tmp_path, records)
    channels = overview(service, "run")["files"][0]["channels"]
    assert channels[0]["temp"] == 0.0
    assert channels[3]["temp"] == 3.0


@pytest.mark.parametrize("metadata", [{}, {"temp": [1., 2., 3., 4.], "bias": [28.5] * 4}])
def test_plastic_placeholder_zeros_require_hk_before_becoming_coordinates(tmp_path, monkeypatch, metadata):
    monkeypatch.setattr(l1_review, "read_parquet_metadata", lambda *_: metadata)
    service = quality_workbench(tmp_path, [{"channel": 3, "placeholder": True, "temp": 0., "bias": 0.,
                                          "center": 0., "fit_quality": {"accepted": False, "redchi": 0.}}])
    plastic = overview(service, "run")["files"][0]["channels"][3]
    assert plastic["temp"] == (4. if metadata else None)
    assert plastic["bias"] == (28.5 if metadata else None)
    assert plastic["chi2_reduced"] is None and plastic["center"] is None
    assert plastic["fit_status"] == "model_pending"


@pytest.mark.parametrize("failure_source", ["attempt", "persistent", "record"])
def test_calculation_failure_is_distinct_from_quality_rejection_and_stale_acceptance(tmp_path, monkeypatch, failure_source):
    monkeypatch.setattr(l1_review, "read_parquet_metadata", lambda *_: {})
    record = {"channel": 0, "center": 2200, "temp": 20., "bias": 28.5,
              "fit_quality": {"accepted": True, "redchi": 1.1}}
    jobs, failures = [], {}
    if failure_source == "attempt":
        jobs = [{"id": "failed", "layer": "TB_L1", "status": "failed", "scope": "selected",
                 "config": {"only": ["m20C_290"], "fit_channels": [0]}}]
    elif failure_source == "persistent":
        failures = {"10B:m20C_290:0": {"job_id": "failed", "error": "unresolved peak"}}
    else:
        record["fit_quality"]["status"] = "fit_failed"
        record["fit_quality"]["accepted"] = False
        record["fit_failure"] = {"job_id": "failed", "error": "unresolved peak"}
    service = quality_workbench(tmp_path, [record], jobs=jobs, failures=failures)
    channel = overview(service, "run")["files"][0]["channels"][0]
    assert channel["fit_status"] == "failed"
    assert channel["error"]
    assert channel["quality_accepted"] is None and channel["chi2_reduced"] is None
    assert channel["center"] == record["center"]  # The earlier curve remains inspectable.
    assert channel["fit_quality"] == record["fit_quality"]
    assert channel["rejection_reasons"] == []
