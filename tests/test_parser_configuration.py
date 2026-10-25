"""Real binary decoding must honor run-specific XML and observation profiles."""

from __future__ import annotations

import binascii
import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.legacy_parser import parse_event_hk_to_dataframe
from calibration_lib.common.utils import read_parquet, read_parquet_metadata, write_parquet
from calibration_lib.config.payload_config import get_payload_config
from calibration_lib.ec.processors.L0_processor import ECL0Processor
from calibration_lib.tb.processors.L0_processor import TBL0Processor
from calibration_lib.workbench.acquisition import default_profile


def field(name, start, size=2, attrs=""):
    return f'<{name} {attrs}><start>{start}</start><size>{size}</size><len>1</len></{name}>'


def packet_inputs(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    event_xml, hk_xml = tmp_path / "event.xml", tmp_path / "hk.xml"
    event_xml.write_text(
        '<grid><detector_event packet_len="13" head="0xa1;0xb2">'
        + field("line", 2, 1) + field("peak", 3) + field("floor", 5)
        + field("ccm_word", 7) + field("clock", 9)
        + field("CRC", 11, attrs='skip0="0" skip1="0"')
        + "</detector_event></grid>", encoding="utf-8",
    )
    hk_xml.write_text(
        '<grid><telemetry packet_len="18" head="0xc3;0xd4">'
        + "".join(field(f"detector_temp{ch}", 2 + ch * 2) for ch in range(4))
        + "".join(field(f"detector_voltage{ch}", 10 + ch * 2) for ch in range(4))
        + "</telemetry></grid>", encoding="utf-8",
    )
    packets = []
    for channel in (0, 1):
        payload = bytes([0xA1, 0xB2, channel]) + b"".join(
            value.to_bytes(2, "big") for value in (1000 + channel * 100, 80, 32768, 20 + channel)
        )
        crc = binascii.crc_hqx(payload, 0) if channel == 0 else 0
        packets.append(payload + crc.to_bytes(2, "big"))
    event, hk = raw / "unstructured.binary", raw / "telemetry.binary"
    event.write_bytes(b"".join(packets))
    hk.write_bytes(bytes([0xC3, 0xD4]) + b"".join(
        value.to_bytes(2, "big") for value in (30000, 30100, 30200, 30300, 28500, 28600, 28700, 28800)
    ))
    profile = default_profile()
    profile["xml"] = {
        "event": {"snapshot_path": str(event_xml)}, "hk": {"snapshot_path": str(hk_xml)},
    }
    profile["parser"].update({"event_tag": "detector_event", "hk_tag": "telemetry"})
    profile["parser"]["fields"].update({
        "channel": "line", "max": "peak", "base": "floor", "ccm": "ccm_word",
        "utc": ["clock"], "temp": "detector_temp{channel}", "bias": "detector_voltage{channel}",
    })
    profile["_sha256"] = "fixture-profile-v1"
    profile["_revision"] = 3
    return raw, event, hk, profile


def decode(tmp_path, event, hk, profile, per_point=True, observation=None):
    return parse_event_hk_to_dataframe(
        event, hk, tmp_path, "grid1x_ft_packet", "hk_packet", per_point,
        input_profile=profile, observation=observation,
    )


def test_selected_event_and_hk_xml_decode_real_packets_and_keep_crc(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    before = {path: path.read_bytes() for path in (event, hk, Path(profile["xml"]["event"]["snapshot_path"]))}
    frame, metadata = decode(tmp_path, event, hk, profile)
    assert frame["channel"].tolist() == [0, 1]
    np.testing.assert_allclose(frame["amp"], [980, 1080])
    np.testing.assert_allclose(frame["temp"], [26.85, 27.85])
    np.testing.assert_allclose(frame["bias"], [28.5, 28.6])
    assert frame["utc"].tolist() == [20, 21]
    assert frame["crc_check"].tolist() == [True, False]
    assert metadata["crc"] == {"available": True, "checked_events": 2, "failed_events": 1}
    acquisition = metadata["acquisition"]
    assert acquisition["profile_sha256"] == "fixture-profile-v1"
    assert acquisition["revision"] == 3
    for kind in ("event", "hk"):
        path = Path(profile["xml"][kind]["snapshot_path"])
        assert acquisition["xml"][kind]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == {path: path.read_bytes() for path in before}


def test_changed_field_mapping_scales_and_xml_have_observable_results(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    baseline, original_meta = decode(tmp_path, event, hk, profile)
    changed = copy.deepcopy(profile)
    changed["parser"]["transform"].update({"base_divisor": 2, "temp_scale": 0.001, "temp_offset": 0, "bias_scale": 0.0001})
    actual, _ = decode(tmp_path, event, hk, changed)
    np.testing.assert_allclose(actual["amp"], baseline["amp"] - 20)
    np.testing.assert_allclose(actual["temp"], [30, 30.1])
    np.testing.assert_allclose(actual["bias"], [2.85, 2.86])
    selected_xml = Path(profile["xml"]["event"]["snapshot_path"])
    alternate = tmp_path / "event-alternate.xml"
    alternate.write_text(selected_xml.read_text().replace("<peak ><start>3</start>", "<peak ><start>5</start>"), encoding="utf-8")
    changed["xml"]["event"]["snapshot_path"] = str(alternate)
    actual, changed_meta = decode(tmp_path, event, hk, changed)
    np.testing.assert_allclose(actual["amp"], [40, 40])
    assert changed_meta["acquisition"]["xml"]["event"]["sha256"] != original_meta["acquisition"]["xml"]["event"]["sha256"]


def test_configured_little_endian_changes_real_packet_decoding(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    payload = bytes([0xB2, 0xA1, 0]) + b"".join(
        value.to_bytes(2, "little") for value in (1000, 80, 32768, 20)
    )
    event.write_bytes(payload + binascii.crc_hqx(payload, 0).to_bytes(2, "little"))
    hk.write_bytes(bytes([0xD4, 0xC3]) + b"".join(
        value.to_bytes(2, "little") for value in (30000, 30100, 30200, 30300, 28500, 28600, 28700, 28800)
    ))
    profile["parser"]["endian"] = "LSB"
    frame, metadata = decode(tmp_path, event, hk, profile)
    assert frame["amp"].tolist() == [980]
    np.testing.assert_allclose(frame["temp"], [26.85])
    assert frame["crc_check"].tolist() == [True]
    assert metadata["acquisition"]["parser"]["endian"] == "LSB"


def test_hk_tail_sample_count_controls_actual_tb_condition_mean(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    hk.write_bytes(hk.read_bytes() + bytes([0xC3, 0xD4]) + b"".join(
        value.to_bytes(2, "big") for value in (31000, 31100, 31200, 31300, 29000, 29100, 29200, 29300)
    ))
    baseline, baseline_meta = decode(tmp_path, event, hk, profile, per_point=False)
    profile["parser"]["transform"]["tail_samples"] = 1
    latest, meta = decode(tmp_path, event, hk, profile, per_point=False)
    assert baseline_meta["channels"]["0"]["temp"] == pytest.approx(31.85)
    assert meta["channels"]["0"]["temp_n"] == 1
    assert meta["channels"]["0"]["temp"] == pytest.approx(36.85)
    # TB per-event rows deliberately carry no T/V; their metadata owns conditions.
    assert "temp" not in latest and "temp" not in baseline


@pytest.mark.parametrize("mode", ["filename", "manifest", "fixed"])
def test_observation_conditions_use_physical_units_once(tmp_path, mode):
    _, event, hk, profile = packet_inputs(tmp_path)
    profile["parser"]["conditions"].update({"mode": mode, "temp": -7.5, "bias": 27.2, "bias_scale": 0.1})
    frame, meta = decode(tmp_path, event, hk, profile, observation={"temp": -7.5, "bias": 27.2})
    np.testing.assert_allclose(frame["temp"], [-7.5, -7.5])
    np.testing.assert_allclose(frame["bias"], [27.2, 27.2])
    assert meta["conditions_source"] == mode


def test_missing_field_or_observation_is_an_actionable_failure(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    profile["parser"]["fields"]["max"] = "missing_peak"
    with pytest.raises(ValueError, match="missing_peak.*字段映射"):
        decode(tmp_path, event, hk, profile)
    profile["parser"]["fields"]["max"] = "peak"
    profile["parser"]["conditions"]["mode"] = "manifest"
    with pytest.raises(ValueError, match="未提供.*temp"):
        decode(tmp_path, event, hk, profile)


def test_custom_packet_tag_respects_explicit_event_repeat_count_and_step(tmp_path):
    _, event, hk, profile = packet_inputs(tmp_path)
    xml = tmp_path / "repeated.xml"
    xml.write_text(
        '<grid><custom_repeat packet_len="14" head="0xe1;0xe2">'
        + field("clock", 2, attrs='multi="1"')
        + field("line", 4, 1, 'repeat="1"')
        + field("peak", 5, attrs='repeat="1"') + field("floor", 7, attrs='repeat="1"')
        + "</custom_repeat></grid>", encoding="utf-8",
    )
    event.write_bytes(bytes([0xE1, 0xE2, 0, 9, 0, 3, 232, 0, 80, 1, 4, 76, 0, 80]))
    profile["xml"]["event"]["snapshot_path"] = str(xml)
    profile["parser"].update({"event_tag": "custom_repeat", "event_multi_evt": 2, "event_multi_step": 5})
    frame, metadata = decode(tmp_path, event, hk, profile)
    assert frame["channel"].tolist() == [0, 1]
    assert frame["amp"].tolist() == [980, 1080]
    assert frame["utc"].tolist() == [9, 9]
    assert metadata["crc"]["available"] is False
    assert metadata["crc"]["checked_events"] == 0


def test_tb_l0_uses_manifest_pair_and_stores_profile_alongside_hk_schema(tmp_path):
    raw, event, hk, profile = packet_inputs(tmp_path)
    profile["discovery"].update({"mode": "manifest", "manifest": [
        {"kind": "tb", "event": str(event), "hk": str(hk), "stem": "condition-A", "temp": -5, "bias": 27.1},
    ]})
    profile["parser"]["conditions"]["mode"] = "manifest"
    processor = TBL0Processor("Custom16", tmp_path / "products", input_profile=profile)
    processor.raw_root = raw
    result = processor.process(raw / "unused-directory")
    assert not result["errors"]
    path = tmp_path / "products/Custom16/tb/L0/parquet/condition-A.l0.parquet"
    assert read_parquet_metadata(path, "hk_data") == {
        "temp": [-5.0] * 4, "bias": [27.1] * 4, "temp_err": [0.0] * 4, "bias_err": [0.0] * 4,
    }
    assert read_parquet_metadata(path, "acquisition")["profile_sha256"] == "fixture-profile-v1"
    assert result["outputs"][0]["acquisition"]["crc"]["failed_events"] == 1
    assert read_parquet(path)["crc_check"].tolist() == [True, False]


def test_ec_l0_custom_filename_manifest_preserves_energy_source_and_exposure(tmp_path):
    raw, event, hk, profile = packet_inputs(tmp_path)
    source_event, source_hk = raw / "run-B.bin", raw / "house-B.bin"
    bkg_event, bkg_hk = raw / "run-C.bin", raw / "house-C.bin"
    source_event.write_bytes(event.read_bytes())
    source_hk.write_bytes(hk.read_bytes())
    bkg_event.write_bytes(event.read_bytes())
    bkg_hk.write_bytes(hk.read_bytes())
    profile["discovery"].update({"mode": "manifest", "manifest": [
        {"kind": "xray", "event": str(event), "hk": str(hk), "energy_keV": 42.5, "channel": 1},
        {"kind": "source", "event": str(source_event), "hk": str(source_hk), "source": "Am241", "exposure_minutes": 1.5, "peak_energies_keV": [59.54]},
        {"kind": "background", "event": str(bkg_event), "hk": str(bkg_hk), "exposure_minutes": 2.5},
    ]})
    cfg = replace(get_payload_config("14B"), raw_root=raw)
    processor = ECL0Processor("Custom16", tmp_path / "products", input_profile=profile)
    result = processor.process(cfg, only_stems=["src_Am241_1.5m"])
    assert not result["errors"]
    assert {row["stem"] for row in result["outputs"]} == {"src_Am241_1.5m", "src_bkg_2.5m"}
    source_output = next(row for row in result["outputs"] if row["data_type"] == "source")
    assert source_output["peak_energies_keV"] == [59.54]
    assert source_output["exposure_minutes"] == 1.5
    assert source_output["bkg_parquet"] == "src_bkg_2.5m.l0.parquet"
    source_path = tmp_path / "products/Custom16/ec/L0/parquet/src_Am241_1.5m.l0.parquet"
    assert read_parquet_metadata(source_path, "acquisition")["revision"] == 3
    result = processor.process(cfg, only_stems=["42.5keV_ch1"])
    assert not result["errors"] and result["outputs"][0]["energy"] == "42.5keV"


def test_additional_parquet_metadata_cannot_silently_replace_scientific_key(tmp_path):
    path = tmp_path / "same.parquet"
    with pytest.raises(ValueError, match="重复.*hk_data"):
        write_parquet(path, pd.DataFrame({"amp": [1]}), metadata={"temp": [1] * 4}, meta_key="hk_data", schema_metadata={"hk_data": {}})
    assert not path.exists()
