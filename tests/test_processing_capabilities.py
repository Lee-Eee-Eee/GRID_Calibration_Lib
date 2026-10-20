"""Explicit profile incompatibilities block both UI jobs and direct adapters."""

import pytest
from calibration_lib.workbench.acquisition import default_profile
from calibration_lib.workbench.compute import LAYERS, ComputeAdapter
from calibration_lib.workbench.service import Workbench

EC_REASON = "N5 X射线须另一照射文件中同物理通道背景及曝光归一化，当前配对接口待接入"


def blocked_profile():
    profile = default_profile("14B")
    profile["processing_capabilities"] = {
        "EC_L2": {"supported": False, "reason": EC_REASON},
        "EC_L3": {"supported": False, "reason": "依赖有效 N5 EC L2"},
    }
    return profile


def test_unsupported_layers_are_visible_and_rejected_before_a_job_or_output_is_created(tmp_path):
    service = Workbench(tmp_path)
    try:
        run = service.create("14B")
        updated = service.save_acquisition(run["id"], blocked_profile())
        assert updated["stages"]["EC_L2"]["unavailable_reason"] == EC_REASON
        assert updated["stages"]["EC_L3"]["unavailable_reason"] == "依赖有效 N5 EC L2"
        assert all(
            updated["stages"][layer]["unavailable_reason"] is None
            for layer in LAYERS
            if layer not in ("EC_L2", "EC_L3")
        )
        state_path = service.root(run["id"]) / "state.json"
        before = state_path.read_bytes()
        files_before = sorted(service.root(run["id"]).rglob("*"))
        for layer, reason in (("EC_L2", EC_REASON), ("EC_L3", "依赖有效 N5 EC L2")):
            with pytest.raises(ValueError, match=reason):
                service.submit(run["id"], {"layer": layer})
            assert state_path.read_bytes() == before
            assert sorted(service.root(run["id"]).rglob("*")) == files_before
            current = service.view(run["id"])
            assert current["jobs"] == []
            assert current["stages"][layer]["status"] == "not_run"
            assert current["stages"][layer]["current_count"] == 0
    finally:
        service.close()


def test_direct_adapter_enforces_explicit_block_before_inputs_are_resolved(tmp_path, monkeypatch):
    adapter = ComputeAdapter(tmp_path, acquisition=blocked_profile())

    def should_not_reach_processor(_payload):
        raise AssertionError("Unsupported processing must stop before input resolution")

    monkeypatch.setattr(adapter, "_config", should_not_reach_processor)
    products = tmp_path / "runs/direct/products"
    for layer in ("EC_L2", "EC_L3"):
        with pytest.raises(ValueError, match=adapter.unavailable_reason(layer)):
            adapter.execute("14B", layer, products, {})
    assert not products.exists()


def test_missing_enabled_and_future_layer_capabilities_do_not_disable_existing_processors(tmp_path):
    assert all(ComputeAdapter(tmp_path).unavailable_reason(layer) is None for layer in LAYERS)
    profile = default_profile("14B")
    profile["processing_capabilities"] = {
        "EC_L2": {"supported": True, "reason": "显式启用"},
        "FUTURE_LAYER": {"supported": False, "reason": "仅适用于未来处理器"},
    }
    adapter = ComputeAdapter(tmp_path, acquisition=profile)
    assert all(adapter.unavailable_reason(layer) is None for layer in LAYERS)
