"""Selected protocols are validated and immutable across acquisition revisions."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from calibration_lib.workbench.acquisition import default_profile
from calibration_lib.workbench.acquisition_store import (
    MAX_XML_BYTES,
    freeze_profile,
    load_profile,
    prepare_profile,
    presets,
    profile_digest,
    read_xml,
    xml_document,
)


def prepared(tmp_path):
    return prepare_profile(tmp_path, payload_profile="14B")


def test_default_profiles_load_real_bundled_xml_and_preserve_payload_directory(tmp_path):
    first = prepare_profile(tmp_path, payload_profile="14B")
    second = prepare_profile(tmp_path, payload_profile="15B")
    assert first["parser"] == second["parser"]
    assert first["discovery"]["rules"][1]["directory"] == "X_new"
    assert second["discovery"]["rules"][1]["directory"] == "X"
    assert profile_digest(first) != profile_digest(second)
    assert "<grid1x_ft_packet" in first["xml"]["event"]["text"]
    assert Path(first["xml"]["event"]["source_path"]).is_file()
    assert len(presets()) == 2
    assert read_xml(tmp_path, preset="grid_packet")["tags"]


@pytest.mark.parametrize(
    "text, message",
    [
        ("<grid>", "语法错误"),
        ("<grid/>", "没有数据包"),
        ('<!DOCTYPE grid [<!ENTITY foo "bar">]><grid><packet/></grid>', "DTD"),
        ("<grid>" + "中" * (MAX_XML_BYTES // 3) + "<packet/></grid>", "1 MiB"),
    ],
    ids=["malformed", "empty", "entity", "oversize-utf8"],
)
def test_invalid_xml_is_rejected_before_snapshot(text, message):
    with pytest.raises(ValueError, match=message):
        xml_document(text)


def test_external_xml_reading_is_utf8_and_does_not_write_original(tmp_path):
    document = read_xml(tmp_path, preset="hnu_packet")
    original = tmp_path / "external.XML"
    original.write_bytes(b"\xef\xbb\xbf" + document["text"].encode("utf-8"))
    before = original.read_bytes()
    imported = read_xml(tmp_path, path="external.XML")
    assert imported["sha256"] == hashlib.sha256(imported["text"].encode("utf-8")).hexdigest()
    profile = default_profile()
    profile["xml"]["event"].update(source_path=str(original))
    draft = prepare_profile(tmp_path, profile)
    draft["xml"]["event"]["text"] = draft["xml"]["event"]["text"].replace(
        'packet_len="584"',
        'packet_len="586"',
        1,
    )
    reference = freeze_profile(tmp_path / "runs/one", draft, 1)
    assert original.read_bytes() == before
    assert 'packet_len="586"' in load_profile(reference)["xml"]["event"]["text"]
    # A later change to the external file cannot alter the saved revision.
    original.write_text("<changed/>", encoding="utf-8")
    assert "<grid1x_ft_packet" in load_profile(reference)["xml"]["event"]["text"]


def test_xml_path_and_encoding_errors_are_actionable(tmp_path):
    with pytest.raises(ValueError, match="请选择存在"):
        read_xml(tmp_path, path="absent.xml")
    with pytest.raises(ValueError, match="未知 XML"):
        read_xml(tmp_path, preset="not-in-preset-list")
    invalid = tmp_path / "invalid.xml"
    invalid.write_bytes(b"\xff\xfeinvalid")
    with pytest.raises(ValueError, match="UTF-8"):
        read_xml(tmp_path, path=invalid)
    invalid.write_bytes(b" " * (MAX_XML_BYTES + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        read_xml(tmp_path, path=invalid)


def test_selected_packet_and_mapped_fields_must_exist_directly(tmp_path):
    profile = prepared(tmp_path)
    profile["parser"]["event_tag"] = ".//data_max"
    with pytest.raises(ValueError, match="没有所选包标签"):
        prepare_profile(tmp_path, profile)
    profile = prepared(tmp_path)
    profile["parser"]["fields"]["max"] = "unknown_amplitude"
    with pytest.raises(ValueError, match="unknown_amplitude"):
        prepare_profile(tmp_path, profile)
    profile = prepared(tmp_path)
    profile["parser"]["fields"]["temp"] = "temperature_{channel}"
    with pytest.raises(ValueError, match="temperature_0"):
        prepare_profile(tmp_path, profile)
    profile["parser"]["conditions"].update(mode="fixed", temp=24, bias=28.5)
    assert prepare_profile(tmp_path, profile)["parser"]["fields"]["temp"] == "temperature_{channel}"


@pytest.mark.parametrize(
    "replacement",
    [
        'packet_len="0"',
        'packet_len="3"',
        'packet_len="99999999"',
    ],
)
def test_invalid_selected_packet_length_is_rejected(tmp_path, replacement):
    profile = prepared(tmp_path)
    profile["xml"]["event"]["text"] = profile["xml"]["event"]["text"].replace('packet_len="584"', replacement)
    with pytest.raises(ValueError, match="packet_len/head/tail"):
        prepare_profile(tmp_path, profile)


def test_invalid_framing_and_fields_are_rejected(tmp_path):
    profile = prepared(tmp_path)
    profile["xml"]["event"]["text"] = profile["xml"]["event"]["text"].replace(
        'head="0x1c;0x1c;0x22;0x88"',
        'head="0x1ff"',
    )
    with pytest.raises(ValueError, match="字段布局无效"):
        prepare_profile(tmp_path, profile)
    profile = prepared(tmp_path)
    profile["xml"]["event"]["text"] = profile["xml"]["event"]["text"].replace(
        "<start>48</start>",
        "<start>584</start>",
    )
    with pytest.raises(ValueError, match="字段布局无效"):
        prepare_profile(tmp_path, profile)


def test_custom_repeated_packet_requires_explicit_repeat_stride(tmp_path):
    profile = prepared(tmp_path)
    profile["xml"]["event"]["text"] = profile["xml"]["event"]["text"].replace(
        "grid1x_ft_packet",
        "different_event_packet",
    )
    profile["parser"]["event_tag"] = "different_event_packet"
    with pytest.raises(ValueError, match="字段布局无效"):
        prepare_profile(tmp_path, profile)
    profile["parser"].update(event_multi_evt=40, event_multi_step=12)
    assert prepare_profile(tmp_path, profile)["parser"]["event_multi_evt"] == 40


def test_snapshot_roundtrip_and_revision_immutability(tmp_path):
    profile = prepared(tmp_path)
    before = copy.deepcopy(profile)
    reference = freeze_profile(tmp_path / "runs/one", profile, 1)
    loaded = load_profile(reference)
    assert loaded["_sha256"] == reference["sha256"] == profile_digest(before)
    assert loaded["_revision"] == 1
    assert profile == before
    for item in reference["xml"]:
        assert Path(item["path"]).parent == Path(reference["path"]).parent
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
    with pytest.raises(FileExistsError):
        freeze_profile(tmp_path / "runs/one", profile, 1)
    profile["parser"]["transform"]["temp_offset"] = -273.0
    second = freeze_profile(tmp_path / "runs/one", profile, 2)
    assert second["sha256"] != reference["sha256"]
    assert load_profile(reference)["parser"]["transform"]["temp_offset"] == -273.15


@pytest.mark.parametrize("target", ["profile", "event", "hk"])
def test_hash_corruption_never_silently_reloads_changed_inputs(tmp_path, target):
    reference = freeze_profile(tmp_path / "run", prepared(tmp_path), 1)
    path = (
        Path(reference["path"])
        if target == "profile"
        else Path(next(item["path"] for item in reference["xml"] if item["role"] == target))
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256.*变化"):
        load_profile(reference)


def test_reference_paths_roles_and_digest_must_match_frozen_profile(tmp_path):
    reference = freeze_profile(tmp_path / "run", prepared(tmp_path), 1)
    changed = copy.deepcopy(reference)
    changed["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="与引用不一致"):
        load_profile(changed)
    changed = copy.deepcopy(reference)
    changed["xml"][1] = changed["xml"][0]
    with pytest.raises(ValueError, match="唯一的 event 和 hk"):
        load_profile(changed)
    changed = copy.deepcopy(reference)
    changed["xml"][0]["path"] = changed["xml"][1]["path"]
    with pytest.raises(ValueError, match="路径与配置记录不一致"):
        load_profile(changed)


def test_missing_snapshot_and_invalid_references_have_user_errors(tmp_path):
    reference = freeze_profile(tmp_path / "run", prepared(tmp_path), 1)
    Path(reference["xml"][0]["path"]).unlink()
    with pytest.raises(ValueError, match="无法恢复"):
        load_profile(reference)
    with pytest.raises(ValueError, match="引用无效"):
        load_profile({})


def test_json_and_xml_contents_must_agree_even_with_updated_file_hash(tmp_path):
    reference = freeze_profile(tmp_path / "run", prepared(tmp_path), 1)
    item = reference["xml"][0]
    path = Path(item["path"])
    path.write_bytes(path.read_bytes() + b" ")
    item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="内容与配置记录不一致"):
        load_profile(reference)
    # Changing a JSON hash alone also cannot replace the selected profile.
    reference = freeze_profile(tmp_path / "run", prepared(tmp_path), 2)
    profile_path = Path(reference["path"])
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["parser"]["transform"]["temp_offset"] = -100
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    reference["file_sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="与引用不一致"):
        load_profile(reference)
