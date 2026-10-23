import json
from pathlib import Path
from types import SimpleNamespace

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
