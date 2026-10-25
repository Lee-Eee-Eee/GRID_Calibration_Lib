"""Interactive reads stay local to selected products and never cache stale inputs."""

import pandas as pd
import pytest
from calibration_lib.common.utils import write_json, write_parquet
from calibration_lib.workbench import compute
from calibration_lib.workbench.acquisition import default_profile
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench


def no_call(*args, **kwargs):
    pytest.fail("This interactive read must not scan or decode unrelated inputs")


def seed_l0(products, *, layer="TB_L0", stem="case", values=(280.0, 300.0, 320.0)):
    pipeline, level = layer.split("_")
    field = "amp_corr" if layer == "EC_L1" else "amp"
    suffix = "_corr.l1.parquet" if layer == "EC_L1" else ".l0.parquet"
    path = products / "14B" / pipeline.lower() / level / "parquet" / (stem + suffix)
    write_parquet(
        path,
        pd.DataFrame({"channel": [0] * len(values), field: values, "crc_check": True}),
        metadata={"temp": [25.0] * 4, "bias": [28.5] * 4},
        meta_key="hk_data",
    )
    return path


def test_selected_tb_spectrum_skips_other_l1_files_and_rereads_changed_products(tmp_path, monkeypatch):
    products = tmp_path / "runs/sample/products"
    seed_l0(products)
    directory = products / "14B/tb/L1/json"
    write_json(directory / "unrelated_fit_results.l1.json", "unrelated invalid fit")
    adapter = ComputeAdapter(tmp_path)
    monkeypatch.setattr(adapter, "points", no_call)
    assert adapter.spectrum("14B", products, "case", 0)["fit"] is None
    record = {
        "channel": 0,
        "center": 300.0,
        "sigma": 10.0,
        "amplitude": 20.0,
        "window_lo": 250.0,
        "window_hi": 350.0,
        "fit_mode": "gaus",
    }
    write_json(directory / "case_fit_results.l1.json", [record])
    assert adapter.spectrum("14B", products, "case", 0)["fit"]["center"] == 300.0
    seed_l0(products, values=(285.0, 300.0, 315.0, 330.0))
    assert adapter.spectrum("14B", products, "case", 0)["n_events"] == 4


def test_tb_point_index_reuses_run_overlay_without_per_channel_file_search(tmp_path, monkeypatch):
    current = tmp_path / "runs/sample/products"
    historical = seed_l0(tmp_path / "products")
    chosen = seed_l0(current)
    adapter = ComputeAdapter(tmp_path)
    monkeypatch.setattr(adapter, "_find_l0", no_call)
    rows = adapter.points("14B", current)
    assert len(rows) == 4 and all(row["l0_path"] == str(chosen) for row in rows)
    chosen.unlink()
    assert all(row["l0_path"] == str(historical) for row in adapter.points("14B", current))


def test_ec_spectrum_and_file_listing_never_inventory_raw_data(tmp_path, monkeypatch):
    products = tmp_path / "runs/sample/products"
    seed_l0(products, layer="EC_L0", stem="70keV_ch0")
    adapter = ComputeAdapter(tmp_path)
    monkeypatch.setattr(adapter, "inventory", no_call)
    monkeypatch.setattr(adapter, "observations", no_call)
    assert adapter.ec_files("14B", products, "EC_L0", 0)[0]["stem"] == "70keV_ch0"
    assert adapter.ec_spectrum("14B", products, "EC_L0", 0)["n_events"] == 3


def test_inventory_discovers_raw_pairs_once_per_operation(tmp_path, monkeypatch):
    adapter = ComputeAdapter(tmp_path, acquisition=default_profile("14B"))
    original = compute.discover
    calls = []

    def discovered(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(compute, "discover", discovered)
    adapter.inventory("14B")
    assert len(calls) == 1
    adapter.inventory("14B")
    assert len(calls) == 2  # No persistent raw-directory discovery cache.


def test_l0_overviews_only_list_files_and_include_unparsed_raw_choices(tmp_path, monkeypatch):
    service = Workbench(tmp_path)
    try:
        run = service.create("14B")
        state = service.read(run["id"])
        state["inventory"]["raw"].update(
            tb_samples=[{"stem": "tb_unparsed", "event": "a.bin", "hk": "a.hk"}],
            reading_preview={
                "rows": [
                    {"kind": "xray", "stem": "70keV_ch0", "channel": 0, "event": "x.bin", "hk": "x.hk"},
                    {"kind": "xray", "stem": "70keV_ch1", "channel": 1, "event": "y.bin", "hk": "y.hk"},
                    {
                        "kind": "source",
                        "stem": "src_Cs137_5m",
                        "channel": None,
                        "event": "s.bin",
                        "hk": "s.hk",
                    },
                ]
            },
        )
        service.save(state)
        seed_l0(service.root(run["id"]) / "products", layer="EC_L0", stem="70keV_ch0")
        monkeypatch.setattr(service.adapter, "surface", no_call)
        monkeypatch.setattr(service.adapter, "_spectrum_input", no_call)
        from calibration_lib.workbench.ec_interactive import ECInspector

        monkeypatch.setattr(ECInspector, "overview", no_call)
        tb = service.surface(run["id"], 0, "TB_L0")
        assert tb["points"] == [] and tb["files"][0]["fit_status"] == "not_parsed"
        ec = service.ec_overview(run["id"], 0, "EC_L0")
        files = {row["stem"]: row for row in ec["files"]}
        assert set(files) == {"70keV_ch0", "src_Cs137_5m"}
        assert files["70keV_ch0"]["fit_status"] == "parsed"
        assert files["src_Cs137_5m"]["fit_status"] == "not_parsed"
        assert files["src_Cs137_5m"]["event"] == "s.bin"
        assert service.ec_overview(run["id"], 0, "EC_L1")["files"] == []
    finally:
        service.close()


def test_saved_dataset_bootstrap_does_not_rescan_input_directories(tmp_path, monkeypatch):
    service = Workbench(tmp_path)
    try:
        service.catalog.scan()
        monkeypatch.setattr(service.catalog, "scan", no_call)
        assert service.bootstrap()["datasets"] == []
    finally:
        service.close()


def test_first_ec_l0_raw_selection_queues_without_tb_fit_or_parsed_product(tmp_path, monkeypatch):
    service = Workbench(tmp_path)
    try:
        run = service.create("14B")
        inventory = {
            "raw": {
                "reading_preview": {
                    "rows": [
                        {"kind": "source", "stem": "src_Cs137_5m"},
                    ]
                }
            }
        }
        monkeypatch.setattr(service.adapter, "inventory", lambda payload: inventory)
        monkeypatch.setattr(service.adapter, "points", no_call)
        queued = []
        monkeypatch.setattr(service.executor, "submit", lambda *args: queued.append(args))
        job = service.submit(run["id"], {"layer": "EC_L0", "stem": "src_Cs137_5m", "representative": False})
        assert job["status"] == "queued" and job["scope"] == "selected"
        assert job["config"]["only"] == ["src_Cs137_5m"] and queued
    finally:
        service.close()


def test_job_poll_reads_unicode_log_tail(tmp_path):
    service = Workbench(tmp_path)
    try:
        run = service.create("14B")
        state = service.read(run["id"])
        state["jobs"].append({"id": "log-test", "status": "failed"})
        service.save(state)
        log = service.root(run["id"]) / "jobs/log-test/processor.log"
        log.parent.mkdir(parents=True)
        content = "解析开始🚀\n" * 14000 + "计算完成\n"
        log.write_text(content, encoding="utf-8")
        assert service.job(run["id"], "log-test")["log"] == content[-12000:]
    finally:
        service.close()
