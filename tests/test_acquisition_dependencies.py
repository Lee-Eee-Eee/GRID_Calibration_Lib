"""Revisions and local reruns cannot certify untouched, obsolete inputs."""

import hashlib
import json
import shutil
import time

import pandas as pd
import pytest
from calibration_lib.common.utils import write_parquet
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench


@pytest.fixture
def configured_run(tmp_path):
    raw = tmp_path / "measurement"
    raw.mkdir()
    manifest = []
    for stem in ("condition_a", "condition_b"):
        for role in ("event", "hk"):
            (raw / f"{stem}.{role}.bin").write_bytes(b"real-input-placeholder")
        manifest.append({"kind": "tb", "event": f"{stem}.event.bin", "hk": f"{stem}.hk.bin", "stem": stem})
    ec_manifest = []
    for kind, name, exposure in (("source", "Na22", 5), ("background", "bkg", 30)):
        for role in ("event", "hk"):
            (raw / f"{name}.{role}.bin").write_bytes(b"source-input-placeholder")
        ec_manifest.append(
            {
                "kind": kind,
                "source": name,
                "exposure_minutes": exposure,
                "event": f"{name}.event.bin",
                "hk": f"{name}.hk.bin",
            }
        )
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), payload="P")["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        profile = service.acquisition(run["id"])["profile"]
        profile["discovery"].update(mode="manifest", manifest=manifest + ec_manifest)
        service.save_acquisition(run["id"], profile)
        yield service, run["id"], manifest
    finally:
        service.close()


def settled(service, run_id, submitted):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = service.job(run_id, submitted["id"])
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "succeeded", job.get("error")
            return job
        time.sleep(0.02)
    pytest.fail("Metadata-only dependency job did not settle")


def test_revision_drops_pending_work_for_observations_outside_new_scope(configured_run):
    service, run_id, manifest = configured_run
    state = service.read(run_id)
    state.update(
        pending_windows=["P:condition_b:0"],
        pending_ec_windows=["P:ec:80keV:0:80"],
        pending_ec_l0=["80keV_ch0", "src_old_source_5m"],
    )
    service.save(state)
    profile = service.acquisition(run_id)["profile"]
    profile["discovery"]["manifest"] = [manifest[0]]
    revised = service.save_acquisition(run_id, profile)
    assert "P:condition_b:0" not in revised.get("pending_windows", [])
    assert "P:ec:80keV:0:80" not in revised.get("pending_ec_windows", [])
    assert not set(revised.get("pending_ec_l0", [])) & {"80keV_ch0", "src_old_source_5m"}


def test_rerunning_one_l0_requires_its_own_l1_before_surface(configured_run, monkeypatch):
    service, run_id, manifest = configured_run
    stems = [row["stem"] for row in manifest]

    def execute(adapter, payload, layer, output, config, _tb):
        for stem in config.get("only") or stems:
            if layer == "TB_L0":
                write_parquet(
                    output / payload / "tb/L0/parquet" / (stem + ".l0.parquet"),
                    pd.DataFrame({"channel": [0], "amp": [100.0]}),
                    metadata={"profile_sha256": adapter.acquisition["_sha256"]},
                    meta_key="acquisition",
                )
            elif layer == "TB_L1":
                path = output / payload / "tb/L1/json" / (stem + "_fit_results.l1.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps([{"channel": ch, "center": 100.0} for ch in range(4)]), encoding="utf-8"
                )
            else:
                raise AssertionError("A stale aggregate must be rejected before processor execution")
        return {"success": True}

    monkeypatch.setattr(ComputeAdapter, "execute", execute)
    monkeypatch.setattr(service, "validate", lambda *_args: {"available": True, "accepted": True})
    for layer in ("TB_L0", "TB_L1"):
        settled(service, run_id, service.submit(run_id, {"layer": layer, "representative": False}))
    assert service.acquisition_coverage(service.read(run_id), "TB_L1")["current"]
    settled(service, run_id, service.submit(run_id, {"layer": "TB_L0", "stem": stems[1]}))
    settled(service, run_id, service.submit(run_id, {"layer": "TB_L1", "stem": stems[0]}))
    # Refitting A cannot certify B after B's raw observation was reprocessed.
    with pytest.raises(ValueError, match="TB L1|TB_L1|上游"):
        service.submit(run_id, {"layer": "TB_L2"})


def test_selected_source_fit_cannot_ignore_a_pending_raw_background(configured_run, monkeypatch):
    service, run_id, _manifest = configured_run
    root = service.root(run_id)
    source = root / "products/P/ec/L1/parquet/src_Na22_5m_corr.l1.parquet"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"existing-corrected-source")
    tb = root / "dependencies/test-tb.json"
    tb.parent.mkdir(parents=True)
    tb.write_text("[]", encoding="utf-8")
    selected = {"path": str(tb), "sha256": hashlib.sha256(tb.read_bytes()).hexdigest()}
    state = service.read(run_id)
    state.update(
        tb_binding="pinned",
        selected_tb=selected,
        ec_dependency=selected,
        ec_scope="all",
        pending_ec_l0=["src_bkg_30m"],
    )
    service.save(state)
    # This test examines submission guards; the invalid old-background job must
    # never be dispatched to the actual processor.
    monkeypatch.setattr(service.executor, "submit", lambda *_args: None)
    with pytest.raises(ValueError, match="EC L0.*更新|EC L1|校正"):
        service.submit(run_id, {"layer": "EC_L2", "stem": "src_Na22_5m", "representative": False})


def test_tb_surface_aggregate_uses_only_profile_stems_in_isolated_inputs(configured_run, monkeypatch):
    service, run_id, manifest = configured_run
    state = service.read(run_id)
    history = service.workspace / "historical-inputs/P"
    json_dir = history / "tb/L1/json"
    json_dir.mkdir(parents=True)
    configured_names = {row["stem"] + "_fit_results.l1.json" for row in manifest}
    for name in configured_names | {"obsolete_condition_fit_results.l1.json"}:
        (json_dir / name).write_text(
            json.dumps([{"channel": ch, "rsquared": 0.99} for ch in range(4)]), encoding="utf-8"
        )
    before = {path.name: path.read_bytes() for path in json_dir.iterdir()}
    output = service.root(run_id) / "jobs/tb-scope-check/products"
    isolated = output / "P/tb/L1/json"
    isolated.mkdir(parents=True)
    for source in json_dir.iterdir():
        shutil.copy2(source, isolated / source.name)
    acquisition = service.adapter_for(state).acquisition
    adapter = ComputeAdapter(
        service.workspace, {**state["dataset"], "product_dir": str(history)}, acquisition
    )
    observed = []

    def inspect_scope(_processor, l1_dir, _l0_dir, excluded_points=None):
        names = {path.name for path in (l1_dir / "json").glob("*_fit_results.l1.json")}
        observed.append(names)
        assert names == configured_names
        assert l1_dir == output / "P/tb/L1"
        raise RuntimeError("aggregate scope inspected")

    monkeypatch.setattr(TBL2Processor, "process", inspect_scope)
    with pytest.raises(RuntimeError, match="aggregate scope inspected"):
        adapter.execute("P", "TB_L2", output, {"representative": False})
    assert observed == [configured_names]
    assert {path.name: path.read_bytes() for path in json_dir.iterdir()} == before
    assert (json_dir / "obsolete_condition_fit_results.l1.json").is_file()


def test_ec_aggregate_uses_only_profile_energy_keys_in_isolated_inputs(configured_run, monkeypatch):
    service, run_id, _manifest = configured_run
    state = service.read(run_id)
    history = service.workspace / "historical-inputs/P"
    json_dir = history / "ec/L2/json"
    json_dir.mkdir(parents=True)
    configured_names = {"src_Na22_fit_results.l2.json"}
    for name in configured_names | {"80keV_fit_results.l2.json", "src_Cs137_fit_results.l2.json"}:
        (json_dir / name).write_text(
            json.dumps([{"channel": ch, "center": 1000.0} for ch in range(4)]), encoding="utf-8"
        )
    before = {path.name: path.read_bytes() for path in json_dir.iterdir()}
    output = service.root(run_id) / "jobs/ec-scope-check/products"
    isolated = output / "P/ec/L2/json"
    isolated.mkdir(parents=True)
    for source in json_dir.iterdir():
        shutil.copy2(source, isolated / source.name)
    acquisition = service.adapter_for(state).acquisition
    adapter = ComputeAdapter(
        service.workspace, {**state["dataset"], "product_dir": str(history)}, acquisition
    )
    observed = []

    def inspect_scope(_processor, l2_dir, _config):
        names = {path.name for path in (l2_dir / "json").glob("*_fit_results.l2.json")}
        observed.append(names)
        assert names == configured_names
        assert l2_dir == output / "P/ec/L2"
        raise RuntimeError("aggregate scope inspected")

    monkeypatch.setattr(ECL3Processor, "process", inspect_scope)
    with pytest.raises(RuntimeError, match="aggregate scope inspected"):
        adapter.execute("P", "EC_L3", output, {"representative": False})
    assert observed == [configured_names]
    assert {path.name: path.read_bytes() for path in json_dir.iterdir()} == before
    assert (json_dir / "src_Cs137_fit_results.l2.json").is_file()
