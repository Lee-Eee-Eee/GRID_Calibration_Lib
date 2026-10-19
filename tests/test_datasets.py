"""Directory discovery and compute adapters must follow the selected inputs."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import write_parquet
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.datasets import DatasetCatalog, validate_payload
from calibration_lib.workbench.service import Workbench


def raw(root, name="ABC"):
    directory = root / "data" / name
    (directory / "Temp_Vbias").mkdir(parents=True)
    (directory / "X").mkdir()
    return directory


def products(root, name="ABC"):
    directory = root / "products" / name
    (directory / "tb/L1/json").mkdir(parents=True)
    (directory / "ec/L1/parquet").mkdir(parents=True)
    return directory


def test_default_scan_discovers_payloads_instead_of_requiring_two_known_names(tmp_path):
    selected_raw = raw(tmp_path)
    selected_products = products(tmp_path)
    scan = DatasetCatalog(tmp_path).scan()
    assert [item["payload"] for item in scan["datasets"]] == ["ABC"]
    assert Path(scan["datasets"][0]["raw_dir"]) == selected_raw
    assert Path(scan["datasets"][0]["product_dir"]) == selected_products


def test_multiple_payloads_pair_by_source_and_can_rename_one(tmp_path):
    raw(tmp_path, "02")
    raw(tmp_path, "03")
    products(tmp_path, "14B")
    products(tmp_path, "15B")
    catalog = DatasetCatalog(tmp_path)
    result = catalog.scan("data", "products")
    assert {item["payload"] for item in result["datasets"]} == {"14B", "15B"}
    with pytest.raises(ValueError, match="多个载荷"):
        catalog.scan("data", "products", "new-label")
    renamed = catalog.scan("data/02", "products/14B", "试算16")["datasets"][0]
    assert renamed["payload"] == "试算16"
    assert renamed["source_payload"] == "14B"
    assert renamed["profile"] == "14B"
    assert len(json.loads(catalog.path.read_text(encoding="utf-8"))["datasets"]) == 3
    adapter = ComputeAdapter(tmp_path, renamed)
    assert adapter.historical_layer("试算16", "TB_L1") == tmp_path / "products/14B/tb/L1"
    assert adapter._config("试算16").raw_root == tmp_path / "data/02"
    assert adapter._config("试算16").payload_name == "试算16"


@pytest.mark.parametrize("selection", ["archive", "archive/products", "archive/products/ABC"])
def test_product_directory_accepts_saved_run_parent_or_payload_root(tmp_path, selection):
    raw(tmp_path)
    (tmp_path / "archive/products/ABC/ec/L2/json").mkdir(parents=True)
    selected = DatasetCatalog(tmp_path).scan(product_dir=selection)["datasets"][0]
    assert Path(selected["product_dir"]) == tmp_path / "archive/products/ABC"
    assert selected["payload"] == "ABC"


def test_raw_only_dataset_does_not_silently_read_default_history(tmp_path):
    selected_raw = raw(tmp_path)
    products(tmp_path, "14B")
    definition = DatasetCatalog(tmp_path).scan(raw_dir=str(selected_raw))["datasets"][0]
    assert definition["product_dir"] is None
    adapter = ComputeAdapter(tmp_path, definition)
    assert adapter.historical_layer("ABC", "TB_L1") != tmp_path / "products/14B/tb/L1"
    assert adapter._config("ABC").ec_xray_dir == selected_raw / "X"
    assert adapter.inventory("ABC")["historical"]["TB_L1"]["files"] == 0


@pytest.mark.parametrize("payload", ["../ABC", "A/B", "A:B", "..", "CON", "NUL.txt", "bad\\name", ""])
def test_output_payload_cannot_escape_run_or_use_reserved_name(payload):
    with pytest.raises(ValueError):
        validate_payload(payload)


def test_missing_selected_directory_has_an_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="目录不存在"):
        DatasetCatalog(tmp_path).scan(raw_dir="absent")


def test_tb_l0_spectrum_exists_before_any_l1_fit(tmp_path):
    selected_raw = raw(tmp_path)
    definition = DatasetCatalog(tmp_path).resolve({"payload": "ABC", "raw_dir": str(selected_raw)})
    adapter = ComputeAdapter(tmp_path, definition)
    root = tmp_path / "runs/test/products"
    write_parquet(
        root / "ABC/tb/L0/parquet/case.l0.parquet",
        pd.DataFrame({"channel": [0] * 30, "amp": np.linspace(10, 60, 30)}),
        metadata={"temp": [20] * 4, "bias": [28.5] * 4},
        meta_key="hk_data",
    )
    spectrum = adapter.spectrum("ABC", root, "ABC:case:0", layer="TB_L0")
    assert spectrum["n_events"] == 30
    assert spectrum["fit"] is None and spectrum["curve"] is None
    assert spectrum["hk_data"]["temp"][0] == 20
    assert spectrum["files"][0]["stem"] == "case"


def test_ec_files_are_selectable_and_hydration_understands_l1_suffix(tmp_path):
    selected_products = products(tmp_path)
    definition = DatasetCatalog(tmp_path).resolve({"payload": "ABC", "product_dir": str(selected_products)})
    adapter = ComputeAdapter(tmp_path, definition)
    frame = pd.DataFrame({"channel": [0] * 30, "amp_corr": np.linspace(10, 90, 30)})
    for name in ("40keV_ch0", "45keV_ch0", "src_Na22_5m"):
        write_parquet(selected_products / "ec/L1/parquet" / f"{name}_corr.l1.parquet", frame)
    root = tmp_path / "runs/test/products"
    spectrum = adapter.ec_spectrum("ABC", root, "EC_L1", 0, stem="src_Na22_5m")
    assert spectrum["stem"] == "src_Na22_5m"
    assert {item["stem"] for item in spectrum["files"]} == {"40keV_ch0", "45keV_ch0", "src_Na22_5m"}
    copied = adapter._hydrate("ABC", root, "EC_L1", ["45keV_ch0"])
    assert len(copied) == 1
    assert (root / "ABC/ec/L1/parquet/45keV_ch0_corr.l1.parquet").is_file()
    assert not (root / "ABC/ec/L1/parquet/40keV_ch0_corr.l1.parquet").exists()


def test_ec_l0_source_selection_reuses_processor_and_includes_background(tmp_path, monkeypatch):
    """Test actual processor routing, replacing only binary decoding with data."""
    selected_raw = raw(tmp_path)
    source = selected_raw / "source"
    source.mkdir()
    for isotope, duration, number in (("Na22", "5m", 1), ("Cs137", "20m", 2), ("Bkg", "30m", 3)):
        (source / f"{isotope}_{duration}_{number}.event.dat").write_bytes(b"raw-input")
        (source / f"{isotope}_{duration}_ecu_{number}.hk").write_bytes(b"hk-input")
    before = {path: path.read_bytes() for path in source.iterdir()}
    seen = []

    def decode(event_path, **kwargs):
        seen.append(event_path.name)
        frame = pd.DataFrame(
            {
                "channel": np.repeat(np.arange(4), 10),
                "amp": np.arange(40),
                "temp": [25.0] * 40,
                "bias": [28.5] * 40,
                "utc": np.arange(40),
                "crc_check": [True] * 40,
            }
        )
        return frame, {}

    monkeypatch.setattr("calibration_lib.ec.processors.L0_processor.parse_event_hk_to_dataframe", decode)
    definition = DatasetCatalog(tmp_path).resolve({"payload": "ABC", "raw_dir": str(selected_raw)})
    adapter = ComputeAdapter(tmp_path, definition)
    output = tmp_path / "runs/source-test/products"
    result = adapter.execute("ABC", "EC_L0", output, {"representative": False, "only": ["src_Cs137_20m"]})
    assert result["success"], result["errors"]
    assert set(seen) == {"Bkg_30m_3.event.dat", "Cs137_20m_2.event.dat"}
    assert {path.name for path in (output / "ABC/ec/L0/parquet").glob("*")} == {
        "src_Cs137_20m.l0.parquet",
        "src_bkg_30m.l0.parquet",
    }
    assert before == {path: path.read_bytes() for path in source.iterdir()}
    with pytest.raises(ValueError, match="缺少匹配"):
        adapter.execute(
            "ABC", "EC_L0", output, {"representative": False, "only": ["src_Am241_10m"]}
        )
    assert set(seen) == {"Bkg_30m_3.event.dat", "Cs137_20m_2.event.dat"}


def test_source_l2_checks_background_tb_dependency_before_any_fit(tmp_path, monkeypatch):
    history = products(tmp_path)
    params = {"G0": 0.012, "V0": 24.11, "k": 0.0184, "b": 54.31, "c": 23459.83}
    tb = tmp_path / "chosen.json"
    tb.write_text(json.dumps([params] * 4), encoding="utf-8")
    exact = {str(channel): params for channel in range(4)}
    stale = {**exact, "0": {**params, "k": 0.02}}
    frame = pd.DataFrame({"channel": [0, 1, 2, 3], "amp_corr": [100.0] * 4})
    write_parquet(
        history / "ec/L1/parquet/src_Cs137_25m_corr.l1.parquet",
        frame,
        metadata={"tb_params_used": exact},
        meta_key="tb_correction",
    )
    write_parquet(
        history / "ec/L1/parquet/src_bkg_30m_corr.l1.parquet",
        frame,
        metadata={"tb_params_used": stale},
        meta_key="tb_correction",
    )
    definition = DatasetCatalog(tmp_path).resolve({"product_dir": str(history), "payload": "renamed"})
    adapter = ComputeAdapter(tmp_path, definition)
    called = []
    monkeypatch.setattr(
        "calibration_lib.workbench.compute.ECL2Processor.process", lambda *args, **kwargs: called.append(True)
    )
    with pytest.raises(ValueError, match="Stale TB dependency in src_bkg_30m"):
        adapter.execute(
            "renamed",
            "EC_L2",
            tmp_path / "runs/proof/products",
            {"representative": True, "ec_point_id": "renamed:ec:src_Cs137:0:661.7"},
            tb,
        )
    assert not called


def test_run_imports_selected_products_under_edited_output_label(tmp_path):
    selected_raw = raw(tmp_path, "02")
    history = tmp_path / "outside-archive/original"
    (history / "tb/L2/json").mkdir(parents=True)
    params = {"G0": 0.012, "V0": 24.11, "k": 0.0184, "b": 54.31, "c": 23459.83}
    source = history / "tb/L2/json/2601010000_original_TB.l2.json"
    source.write_text(json.dumps([params] * 4), encoding="utf-8")
    original = source.read_bytes()
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(raw_dir=str(selected_raw), product_dir=str(history))["datasets"][0]
        run = service.create("edited-label", dataset_id=dataset["id"])
        assert run["dataset"]["source_payload"] == "original"
        assert run["tb_binding"] == "pinned"
        assert service.adapter_for(run).historical_layer("edited-label", "TB_L2") == history / "tb/L2"
        assert (service.root(run["id"]) / "products/edited-label/tb/L2/json" / source.name).is_file()
        assert any(
            artifact["url"].startswith(f"/api/runs/{run['id']}/history/TB_L2/")
            for artifact in run["artifacts"]
        )
        assert source.read_bytes() == original
    finally:
        service.close()


@pytest.mark.parametrize("valid_older", [True, False])
@pytest.mark.parametrize("invalid_newest", [[{}, {}, {}, {}], {"unexpected": "not a TB package"}])
def test_surface_skips_invalid_packages_and_keeps_l1_points_readable(tmp_path, valid_older, invalid_newest):
    history = products(tmp_path)
    records = [{"channel": 0, "center": 100.0, "center_err": 1.0, "temp": 20.0,
                "bias": 28.5, "rsquared": .99}]
    (history / "tb/L1/json/case_fit_results.l1.json").write_text(json.dumps(records), encoding="utf-8")
    directory = history / "tb/L2/json"
    directory.mkdir(parents=True)
    if valid_older:
        params = {"G0": .012, "V0": 24.11, "k": .0184, "b": 54.31, "c": 23459.83}
        (directory / "2601010000_ABC_TB.l2.json").write_text(json.dumps([params] * 4), encoding="utf-8")
    (directory / "9912310000_ABC_TB.l2.json").write_text(json.dumps(invalid_newest), encoding="utf-8")
    adapter = ComputeAdapter(tmp_path)
    result = adapter.surface("ABC", tmp_path / "products", 0)
    assert len(result["points"]) == 1 and result["points"][0]["stem"] == "case"
    if valid_older:
        assert result["version"] == "2601010000_ABC_TB.l2.json"
        assert result["params"]["k"] == .0184
        assert result["grid"] is not None
    else:
        assert result["version"] is None and result["params"] is None and result["grid"] is None
