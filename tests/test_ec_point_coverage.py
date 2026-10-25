"""A local EC refit certifies only the point actually updated by the processor."""

import hashlib
from types import SimpleNamespace

import pytest
from calibration_lib.common.utils import read_json, write_json
from calibration_lib.workbench.service import Workbench

ENERGY = "40keV"
FILENAME = f"{ENERGY}_fit_results.l2.json"
RELATIVE = f"14B/ec/L2/json/{FILENAME}"
POINTS = [f"14B:ec:{ENERGY}:{channel}:40" for channel in range(4)]


@pytest.fixture
def point_run(tmp_path, monkeypatch):
    service = Workbench(tmp_path)
    run_id = service.create("14B")["id"]
    root = service.root(run_id)
    versions = []
    for revision in (1, 2):
        path = root / "dependencies" / f"tb-{revision}.json"
        write_json(path, {"revision": revision})
        versions.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    old_tb, current_tb = versions
    state = service.read(run_id)
    state.update(
        acquisition={"sha256": "current-reading-revision"},
        acquisition_products={"EC_L1": [f"{ENERGY}_ch{channel}" for channel in range(4)]},
        acquisition_pending_layers=["EC_L2"],
        dirty_layers=["EC_L2", "EC_L3"],
        pending_ec_windows=list(POINTS),
        tb_binding="pinned", selected_tb=current_tb, ec_dependency=current_tb, ec_scope="all",
    )
    state["inventory"]["raw"]["reading_preview"] = {"rows": [
        {"kind": "xray", "stem": f"{ENERGY}_ch{channel}", "channel": channel, "energy_keV": 40.0}
        for channel in range(4)
    ]}
    service.save(state)

    def execute(_payload, layer, output, config, _tb):
        assert layer == "EC_L2"
        path = output / RELATIVE
        records = read_json(path) if path.exists() else []
        point = config["ec_point_id"]
        channel = int(point.split(":")[3])
        updated = {"channel": channel, "E": 40.0, "center": 400.0 + channel}
        records = [row for row in records if row["channel"] != channel] + [updated]
        write_json(path, sorted(records, key=lambda row: row["channel"]))
        return {"success": True, "processor": {"outputs": [
            {"energy": ENERGY, "json": FILENAME, "points_updated": [point], "channels_updated": [channel]}
        ]}}

    monkeypatch.setattr(service, "adapter_for", lambda _state: SimpleNamespace(execute=execute))
    monkeypatch.setattr(service, "validate", lambda *_: {"available": True, "accepted": True})

    def refit(point):
        state = service.read(run_id)
        job_id = f"point-{len(state['jobs'])}"
        state["jobs"].append({
            "id": job_id, "layer": "EC_L2", "status": "queued", "scope": "selected",
            "config": {"ec_point_id": point}, "tb_version": current_tb,
            "acquisition": state["acquisition"],
        })
        service.save(state)
        service._execute(run_id, job_id)
        result = service.job(run_id, job_id)
        assert result["status"] == "succeeded", result.get("error")
        return service.read(run_id)

    try:
        yield service, run_id, old_tb, current_tb, refit
    finally:
        service.close()


def seed_old_records(service, run_id, old_tb):
    write_json(service.root(run_id) / "products" / RELATIVE,
               [{"channel": channel, "E": 40.0, "center": 100.0 + channel} for channel in range(4)])
    state = service.read(run_id)
    # This is the older, whole-file provenance format that must migrate without
    # assigning the new TB version to unchanged records.
    state["ec_l2_provenance"] = {RELATIVE: {"job_id": "old-job", "tb_version": old_tb}}
    service.save(state)


def test_single_point_preserves_other_pending_windows_and_does_not_complete_energy(point_run):
    service, run_id, old_tb, _current_tb, refit = point_run
    seed_old_records(service, run_id, old_tb)
    before = read_json(service.root(run_id) / "products" / RELATIVE)
    state = refit(POINTS[0])
    assert state["acquisition_ec_l2_points"] == {ENERGY: [POINTS[0]]}
    assert state["acquisition_products"]["EC_L2"] == []
    assert service.acquisition_coverage(state, "EC_L2")["current"] is False
    assert "EC_L2" in state["acquisition_pending_layers"]
    assert state["pending_ec_windows"] == POINTS[1:]
    assert "EC_L2" in state["dirty_layers"]
    after = read_json(service.root(run_id) / "products" / RELATIVE)
    assert after[1:] == before[1:]


def test_local_refit_does_not_relabel_unchanged_points_with_the_current_tb(point_run):
    service, run_id, old_tb, current_tb, refit = point_run
    seed_old_records(service, run_id, old_tb)
    state = refit(POINTS[0])
    versions = state["ec_l2_provenance"][RELATIVE]["point_versions"]
    assert versions[POINTS[0]]["sha256"] == current_tb["sha256"]
    assert all(versions[point]["sha256"] == old_tb["sha256"] for point in POINTS[1:])
    assert service.ec_coverage(state)["computed_files"] == 0
    assert service.ec_coverage(state)["mixed"] is True
    for point in POINTS[1:]:
        state = refit(point)
    assert service.ec_coverage(state)["computed_files"] == 1
    assert service.ec_coverage(state)["mixed"] is False
    assert service.acquisition_coverage(state, "EC_L2")["current"] is True
    assert state["pending_ec_windows"] == []


def test_first_local_fit_without_old_peer_records_does_not_certify_all_channels(point_run):
    service, _run_id, _old_tb, _current_tb, refit = point_run
    state = refit(POINTS[0])
    assert state["acquisition_ec_l2_points"] == {ENERGY: [POINTS[0]]}
    assert service.acquisition_coverage(state, "EC_L2")["current"] is False
    assert state["acquisition_products"]["EC_L2"] == []
    assert "EC_L2" in state["acquisition_pending_layers"]
    assert state["pending_ec_windows"] == POINTS[1:]
