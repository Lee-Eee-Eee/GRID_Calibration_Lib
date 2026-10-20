"""Reusable reading standards keep immutable versions and directory provenance."""

import copy
import hashlib
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from calibration_lib.workbench.acquisition_store import load_profile, prepare_profile
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.presets import PresetRegistry
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench, peak_job_settings


@pytest.fixture
def inputs(tmp_path):
    raw = tmp_path / "data/DETECTOR-A"
    (raw / "Temp_Vbias").mkdir(parents=True)
    (raw / "X_new").mkdir()
    (raw / "source").mkdir()
    (raw / "Temp_Vbias/20C_285_001.event.dat").write_bytes(b"events")
    (raw / "Temp_Vbias/20C_285_ecu_001.hk").write_bytes(b"hk")
    product = tmp_path / "products/DETECTOR-A"
    (product / "tb/L0/parquet").mkdir(parents=True)
    profile = prepare_profile(tmp_path, payload_profile="14B")
    profile["parser"]["conditions"].update(mode="fixed", temp=20.0, bias=28.5)
    profile["parser"]["transform"]["temp_offset"] = -273.05
    return tmp_path, raw, product, profile


def saved(registry, raw, product, profile, **kwargs):
    return registry.save(
        profile=profile,
        name="DETECTOR A flight protocol",
        payload="DETECTOR-A",
        bindings=[{"role": "raw", "path": str(raw)}, {"role": "product", "path": str(product)}],
        **kwargs,
    )


def definition(raw, product):
    return {"payload": "DETECTOR-A", "raw_dir": str(raw), "product_dir": str(product)}


def test_versions_contain_xml_fields_rules_and_energies_without_referencing_mutable_input(inputs):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    first = saved(registry, raw, product, profile)
    original = copy.deepcopy(first)
    profile["parser"]["transform"]["bias_scale"] = 0.002
    profile["discovery"]["sources"][0]["energies_keV"] = [510.99895, 1274.537]
    second = saved(registry, raw, product, profile, preset_id=first["reference"]["id"])
    assert second["reference"]["version"] == 2
    assert first["reference"]["sha256"] != second["reference"]["sha256"]
    reopened = PresetRegistry(workspace)
    restored = reopened.get(first["reference"]["id"], 1)
    assert restored["profile"] == original["profile"]
    assert restored["profile"]["parser"]["transform"]["bias_scale"] == 0.001
    assert reopened.get(first["reference"]["id"])["profile"]["discovery"]["sources"][0]["energies_keV"] == [
        510.99895,
        1274.537,
    ]
    assert all(restored["profile"]["xml"][role]["text"] for role in ("event", "hk"))
    assert restored["profile"]["discovery"]["rules"]
    assert Path(restored["profile"]["xml"]["event"]["snapshot_path"]).is_relative_to(
        workspace / "runs/_presets"
    )
    events = json.loads(registry.path.read_text(encoding="utf-8"))["events"]
    assert len([e for e in events if e["action"] == "publish"]) == 2
    assert any(e["action"] == "bind" and e["previous"] for e in events)


@pytest.mark.parametrize("target", ["profile", "event", "hk"])
def test_corrupt_version_is_rejected_instead_of_reconstructed_from_defaults(inputs, target):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    first = saved(registry, raw, product, profile)
    snapshot = first["preset"]["versions"][0]["profile_reference"]
    path = Path(
        snapshot["path"]
        if target == "profile"
        else next(item["path"] for item in snapshot["xml"] if item["role"] == target)
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        registry.get(first["reference"]["id"], 1)


def test_directory_matching_overrides_payload_label_and_conflicting_dirs_require_choice(inputs):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    first = saved(registry, raw, product, profile)
    renamed = {**definition(raw, product), "payload": "renamed-output", "source_payload": "also-renamed"}
    assert registry.match(renamed)["reference"]["id"] == first["reference"]["id"]
    other = registry.save(profile=profile, name="Other protocol", payload="OTHER")
    registry.bind(other["reference"]["id"], 1, [{"role": "product", "path": str(product)}])
    conflict = registry.match(renamed)
    assert conflict["status"] == "conflict"
    assert conflict["reference"] is None
    assert {c["id"] for c in conflict["candidates"]} == {first["reference"]["id"], other["reference"]["id"]}
    assert conflict["warnings"]


def test_unambiguous_payload_fallback_and_ambiguity_are_explicit(inputs):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    first = registry.save(profile=profile, name="Single standard", payload="14B")
    match = registry.match({"payload": "GRID-02", "raw_dir": str(raw)})
    assert match["reference"]["id"] == first["reference"]["id"]
    assert match["reference"]["origin"] == "payload_match"
    registry.save(profile=profile, name="Second standard", payload="14B")
    assert registry.match({"payload": "14B"})["status"] == "conflict"
    assert registry.match({"payload": "unknown"})["status"] == "none"


def test_new_runs_snapshot_current_binding_and_prior_run_never_tracks_latest(inputs):
    workspace, raw, product, profile = inputs
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (raw, product)
        for p in folder.rglob("*")
        if p.is_file()
    }
    service = Workbench(workspace)
    try:
        initial = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        run = service.create(dataset_id=initial["id"])
        published = service.save_acquisition_preset(run["id"], {"name": "Detector A", "profile": profile})
        first = published["reference"]
        selected = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        assert selected["acquisition_preset_match"]["reference"]["version"] == 1
        prior = service.create(dataset_id=selected["id"])
        prior_bytes = Path(prior["acquisition"]["path"]).read_bytes()
        changed = service.acquisition(run["id"])["profile"]
        changed["parser"]["conditions"]["temp"] = 22.0
        published_two = service.save_acquisition_preset(
            run["id"], {"preset_id": first["id"], "profile": changed, "reason": "correct fixed condition"}
        )
        assert published_two["reference"]["version"] == 2
        assert service.view(prior["id"])["acquisition_preset"]["version"] == 1
        assert Path(prior["acquisition"]["path"]).read_bytes() == prior_bytes
        assert load_profile(prior["acquisition"])["parser"]["conditions"]["temp"] == 20.0
        fresh = service.create(dataset_id=selected["id"])
        assert fresh["acquisition_preset"]["version"] == 2
        assert load_profile(fresh["acquisition"])["parser"]["conditions"]["temp"] == 22.0
        # Selecting a prior run's products recovers its pinned version.
        old_products = service.root(prior["id"]) / "products" / prior["payload"]
        (old_products / "tb").mkdir(exist_ok=True)
        old_dataset = service.scan_datasets(product_dir=str(old_products))["datasets"][0]
        assert old_dataset["acquisition_preset_match"]["reference"]["version"] == 1
        # Default raw and old product bindings disagree; the new run must ask for a choice.
        mixed = service.scan_datasets(raw_dir=str(raw), product_dir=str(old_products))["datasets"][0]
        states = list((workspace / "runs").glob("*/state.json"))
        with pytest.raises(ValueError, match="明确选择"):
            service.create(dataset_id=mixed["id"])
        assert list((workspace / "runs").glob("*/state.json")) == states
        explicit = service.create(dataset_id=mixed["id"], preset_id=first["id"], preset_version=1)
        assert explicit["acquisition_preset"]["origin"] == "manual"
        assert explicit["acquisition_preset"]["version"] == 1
    finally:
        service.close()
    restored = Workbench(workspace)
    try:
        assert restored.acquisition(prior["id"])["acquisition_preset"]["version"] == 1
        assert restored.acquisition(fresh["id"])["acquisition_preset"]["version"] == 2
    finally:
        restored.close()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in before.items())


def test_manual_edits_mark_parent_preset_and_switch_records_revision(inputs):
    workspace, raw, product, profile = inputs
    service = Workbench(workspace)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        selected = service.save_acquisition_preset(run["id"], {"name": "Detector A", "profile": profile})
        edited = copy.deepcopy(profile)
        edited["parser"]["conditions"]["temp"] = 21.0
        changed = service.save_acquisition(run["id"], edited)
        assert changed["acquisition_preset"]["modified"]
        switched = service.apply_acquisition_preset(run["id"], selected["reference"]["id"], 1)
        assert switched["acquisition_preset"]["modified"] is False
        assert switched["acquisition"]["sha256"] == selected["reference"]["sha256"]
        assert switched["revisions"][-1]["acquisition_preset"]["version"] == 1
        # Equal-content versions still change the explicit provenance without new fitting invalidation.
        same = service.save_acquisition_preset(run["id"], {"preset_id": selected["reference"]["id"]})
        assert same["reference"]["version"] == 2
        assert same["run"]["acquisition"]["revision"] == switched["acquisition"]["revision"]
        assert same["run"]["config_revision"] == switched["config_revision"] + 1
    finally:
        service.close()


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "payload": "D"},
        {"name": "A", "payload": None},
        {"name": "A", "payload": "../bad"},
        {"name": "A", "payload": "D", "bindings": [{"role": "raw"}]},
        {"name": "A", "payload": "D", "bindings": [{"role": "other", "path": "."}]},
        {"name": "A", "payload": "D", "bindings": [{"role": "raw", "path": "absent"}]},
    ],
)
def test_invalid_preset_is_not_published(inputs, body):
    workspace, _, _, profile = inputs
    registry = PresetRegistry(workspace)
    with pytest.raises(ValueError):
        registry.save(profile=profile, **body)
    assert registry.list() == {"presets": []}
    assert not registry.root.exists()


@pytest.mark.parametrize(
    "spec",
    [
        {"automatic_window": 1},
        {"peak_detection": []},
        {"peak_detection": {"unknown": 1}},
        {"peak_detection": {"expected_center": 0}},
        {"peak_detection": {"noise_ceiling": -1}},
        {"peak_detection": {"min_significance": float("nan")}},
        {"peak_detection": {"min_sigma_bins": True}},
        {"peak_detection": {"expected_tolerance": 1.1}},
        {"peak_detection": {"max_resolution": 0}},
    ],
)
def test_peak_job_settings_reject_unsafe_or_undefined_search_parameters(spec):
    with pytest.raises(ValueError):
        peak_job_settings("TB_L1", spec)


def test_peak_job_settings_keep_physical_constraints_in_job_config():
    options = {
        "automatic_window": True,
        "peak_detection": {
            "min_significance": 4.5,
            "min_sigma_bins": 1,
            "max_resolution": 1.2,
            "noise_ceiling": 0,
            "expected_center": 60,
            "expected_tolerance": 0.25,
        },
    }
    assert peak_job_settings("EC_L2", options) == options
    assert (
        peak_job_settings("TB_L1", {"peak_detection": {"expected_center": None, "noise_ceiling": None}})[
            "peak_detection"
        ]["expected_center"]
        is None
    )
    with pytest.raises(ValueError, match="只适用于"):
        peak_job_settings("TB_L0", options)


def test_source_line_energy_anchor_requires_ec_adc_reference():
    settings = {"peak_detection": {"expected_center": 140, "expected_energy_keV": 511}}
    assert peak_job_settings("EC_L2", settings) == settings
    with pytest.raises(ValueError, match="EC L2"):
        peak_job_settings("TB_L1", settings)
    with pytest.raises(ValueError, match="期望 ADC"):
        peak_job_settings("EC_L2", {"peak_detection": {"expected_energy_keV": 511}})


def test_preset_http_endpoints_and_new_run_version_choice(inputs):
    workspace, raw, product, profile = inputs
    service = Workbench(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            return response.status, data

        status, created = request(
            "POST",
            "/api/acquisition/presets",
            {
                "profile": profile,
                "name": "Detector A",
                "payload": "DETECTOR-A",
                "bindings": [{"role": "raw", "path": str(raw)}, {"role": "product", "path": str(product)}],
            },
        )
        assert status == 201
        preset_id = created["reference"]["id"]
        assert (
            request("GET", "/api/acquisition/presets?payload=DETECTOR-A")[1]["presets"][0]["id"] == preset_id
        )
        assert (
            request("GET", f"/api/acquisition/presets/{preset_id}?version=1")[1]["profile"]["parser"][
                "conditions"
            ]["temp"]
            == 20.0
        )
        scan = request("POST", "/api/datasets/scan", {"raw_dir": str(raw), "product_dir": str(product)})[1]
        status, run = request(
            "POST",
            "/api/runs",
            {"dataset_id": scan["datasets"][0]["id"], "preset_id": preset_id, "preset_version": 1},
        )
        assert status == 201 and run["acquisition_preset"]["version"] == 1
        status, republished = request(
            "POST",
            f"/api/runs/{run['id']}/acquisition/presets",
            {"preset_id": preset_id, "reason": "unchanged XML, new named revision"},
        )
        assert status == 201 and republished["reference"]["version"] == 2
        status, switched = request(
            "POST", f"/api/runs/{run['id']}/acquisition/preset", {"preset_id": preset_id, "version": 1}
        )
        assert status == 200 and switched["acquisition_preset"]["version"] == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        service.close()


@pytest.mark.parametrize("profile", [None, [], "", "default"])
def test_generic_publish_requires_explicit_profile_object(inputs, profile):
    workspace, _, _, _ = inputs
    service = Workbench(workspace)
    try:
        with pytest.raises(ValueError, match="profile"):
            service.register_acquisition_preset({"name": "Incomplete", "payload": "A", "profile": profile})
        assert service.acquisition_presets() == {"presets": []}
        assert not service.preset_registry.root.exists()
    finally:
        service.close()


@pytest.mark.parametrize("invalid_kind", ["xml", "manifest"])
def test_invalid_draft_cannot_publish_a_version_or_change_directory_bindings(inputs, invalid_kind):
    workspace, raw, product, profile = inputs
    service = Workbench(workspace)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        good = service.save_acquisition_preset(run["id"], {"name": "Flight", "profile": profile})
        state_bytes = (service.root(run["id"]) / "state.json").read_bytes()
        registry_bytes = service.preset_registry.path.read_bytes()
        changed = copy.deepcopy(profile)
        if invalid_kind == "xml":
            changed["xml"]["event"]["text"] = "<invalid"
        else:
            changed["discovery"].update(
                mode="manifest",
                manifest=[{"kind": "tb", "event": "absent.bin", "hk": "absent.hk", "stem": "sample"}],
            )
        with pytest.raises(ValueError):
            service.save_acquisition_preset(
                run["id"], {"preset_id": good["reference"]["id"], "profile": changed}
            )
        assert (service.root(run["id"]) / "state.json").read_bytes() == state_bytes
        assert service.preset_registry.path.read_bytes() == registry_bytes
    finally:
        service.close()


def test_failed_publication_rolls_back_run_and_registry_and_allows_retry(inputs, monkeypatch):
    workspace, raw, product, profile = inputs
    service = Workbench(workspace)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        published = service.save_acquisition_preset(run["id"], {"name": "Flight", "profile": profile})
        state_path = service.root(run["id"]) / "state.json"
        state_bytes = state_path.read_bytes()
        registry_bytes = service.preset_registry.path.read_bytes()
        snapshots = {
            path: path.read_bytes()
            for root in (service.root(run["id"]) / "acquisition", service.preset_registry.root)
            for path in root.rglob("*")
            if path.is_file()
        }
        changed = copy.deepcopy(profile)
        changed["parser"]["conditions"]["temp"] = 23.0
        original_bind = service._bind_run_products

        def reject_binding(*_args):
            raise OSError("injected directory-binding failure after run save")

        monkeypatch.setattr(service, "_bind_run_products", reject_binding)
        with pytest.raises(OSError, match="injected"):
            service.save_acquisition_preset(
                run["id"], {"preset_id": published["reference"]["id"], "profile": changed}
            )
        assert state_path.read_bytes() == state_bytes
        assert service.preset_registry.path.read_bytes() == registry_bytes
        assert all(path.read_bytes() == contents for path, contents in snapshots.items())
        assert len(list((service.root(run["id"]) / "acquisition").glob("*/profile.json"))) == 1
        assert len(list(service.preset_registry.root.glob("*/acquisition/*/profile.json"))) == 1
        monkeypatch.setattr(service, "_bind_run_products", original_bind)
        retried = service.save_acquisition_preset(
            run["id"], {"preset_id": published["reference"]["id"], "profile": changed}
        )
        assert retried["reference"]["version"] == 2
        assert load_profile(retried["run"]["acquisition"])["parser"]["conditions"]["temp"] == 23.0
    finally:
        service.close()


def test_inventory_failure_prevents_publishing_any_preset(inputs, monkeypatch):
    workspace, raw, product, profile = inputs
    service = Workbench(workspace)
    try:
        dataset = service.scan_datasets(raw_dir=str(raw), product_dir=str(product))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        before = (service.root(run["id"]) / "state.json").read_bytes()

        def reject_inventory(*_args):
            raise OSError("unreadable selected input")

        monkeypatch.setattr(ComputeAdapter, "inventory", reject_inventory)
        with pytest.raises(OSError, match="unreadable"):
            service.save_acquisition_preset(run["id"], {"name": "Flight", "profile": profile})
        assert service.acquisition_presets() == {"presets": []}
        assert not service.preset_registry.path.exists()
        assert (service.root(run["id"]) / "state.json").read_bytes() == before
    finally:
        service.close()


@pytest.mark.parametrize("tamper", ["version_sha", "revision", "binding_sha"])
def test_mismatched_outer_version_or_binding_reference_is_rejected(inputs, tamper):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    published = saved(registry, raw, product, profile)
    index = json.loads(registry.path.read_text(encoding="utf-8"))
    if tamper == "version_sha":
        index["presets"][0]["versions"][0]["sha256"] = "0" * 64
    elif tamper == "revision":
        index["presets"][0]["versions"][0]["profile_reference"]["revision"] = 2
    else:
        index["bindings"][0]["sha256"] = "0" * 64
    registry.path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        if tamper == "binding_sha":
            registry.match(definition(raw, product))
        else:
            registry.get(published["reference"]["id"], 1)


def test_registry_write_failure_preserves_earlier_version_and_allows_same_version_retry(inputs, monkeypatch):
    workspace, raw, product, profile = inputs
    registry = PresetRegistry(workspace)
    first = saved(registry, raw, product, profile)
    before = registry.path.read_bytes()
    original_write = registry._write

    def reject_index_write(data):
        original_write(data)
        raise OSError("index publication acknowledgement failed")

    monkeypatch.setattr(registry, "_write", reject_index_write)
    changed = copy.deepcopy(profile)
    changed["parser"]["conditions"]["temp"] = 23.0
    with pytest.raises(OSError, match="acknowledgement"):
        saved(registry, raw, product, changed, preset_id=first["reference"]["id"])
    assert registry.path.read_bytes() == before
    assert len(list(registry.root.glob("*/acquisition/*/profile.json"))) == 1
    assert registry.get(first["reference"]["id"], 1)["profile"] == first["profile"]
    monkeypatch.setattr(registry, "_write", original_write)
    retried = saved(registry, raw, product, changed, preset_id=first["reference"]["id"])
    assert retried["reference"]["version"] == 2
