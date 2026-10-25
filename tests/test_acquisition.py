"""Input discovery is explicit and rejects ambiguous acquisition metadata."""

from pathlib import Path

import pytest
from calibration_lib.workbench.acquisition import default_profile, discover, validate_profile


def packet(root, name):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"raw-packet-input")
    return path


def test_defaults_are_independent_and_payload_presets_keep_existing_directory():
    first, second = default_profile("14B"), default_profile("15B")
    assert first["discovery"]["rules"][1]["directory"] == "X_new"
    assert second["discovery"]["rules"][1]["directory"] == "X"
    first["parser"]["fields"]["max"] = "different"
    assert default_profile()["parser"]["fields"]["max"] == "data_max"


def test_new_filename_convention_pairs_and_converts_nominal_units(tmp_path):
    packet(tmp_path, "thermal/run_A_events.bin")
    packet(tmp_path, "thermal/run_A_housekeeping.bin")
    profile = default_profile()
    profile["parser"]["conditions"]["mode"] = "filename"
    profile["discovery"]["rules"] = [
        {
            "kind": "tb",
            "directory": "thermal",
            "event_regex": r"^run_(?P<key>[A-Z]+)_events\.bin$",
            "hk_regex": r"^run_(?P<key>[A-Z]+)_housekeeping\.bin$",
            "metadata_regex": r"^(?P<temp>run)",
        }
    ]
    # Temperature/bias can be captured by a completely different naming scheme.
    profile["discovery"]["rules"][0]["metadata_regex"] = None
    preview = discover(tmp_path, profile)
    assert not preview["rows"] and "缺少 temp" in preview["errors"][0]
    profile["discovery"]["rules"][0].update(
        event_regex=r"^T(?P<temp>m?\d+)_V(?P<bias>\d+)_(?P<key>[A-Z]+)_events\.bin$",
        hk_regex=r"^run_(?P<key>[A-Z]+)_housekeeping\.bin$",
    )
    (tmp_path / "thermal/run_A_events.bin").unlink()
    packet(tmp_path, "thermal/Tm20_V287_A_events.bin")
    preview = discover(tmp_path, profile)
    assert preview["errors"] == []
    assert preview["rows"][0]["temp"] == -20
    assert preview["rows"][0]["bias"] == pytest.approx(28.7)
    assert preview["counts"] == {"tb": 1}


def test_explicit_manifest_bypasses_names_and_supports_custom_source(tmp_path):
    event, hk = packet(tmp_path, "0001.bin"), packet(tmp_path, "ancillary.bin")
    profile = default_profile()
    profile["parser"]["conditions"]["mode"] = "manifest"
    profile["discovery"].update(
        mode="manifest",
        manifest=[
            {
                "event": event.name,
                "hk": hk.name,
                "kind": "source",
                "source": "Am241",
                "peak_energies_keV": [59.54],
                "exposure_minutes": 2.5,
                "temp": 24.2,
                "bias": 28.6,
            }
        ],
    )
    preview = discover(tmp_path, profile)
    assert not preview["errors"]
    row = preview["rows"][0]
    assert row["stem"] == "src_Am241_2.5m"
    assert row["temp"] == 24.2 and row["bias"] == 28.6
    assert row["peak_energies_keV"] == [59.54]
    assert Path(row["event"]) == event


def test_custom_xray_filename_keeps_canonical_downstream_stem(tmp_path):
    packet(tmp_path, "events/E59.54_C2_shot.bin")
    packet(tmp_path, "events/HK_E59.54_C2.bin")
    profile = default_profile()
    profile["discovery"]["rules"] = [
        {
            "kind": "xray",
            "directory": "events",
            "event_regex": r"^E(?P<energy>[\d.]+)_C(?P<ch>\d)_shot\.bin$",
            "hk_regex": r"^HK_E(?P<energy>[\d.]+)_C(?P<ch>\d)\.bin$",
        }
    ]
    result = discover(tmp_path, profile)
    assert not result["errors"]
    assert result["rows"][0]["stem"] == "59.54keV_ch2"
    assert result["rows"][0]["energy_keV"] == 59.54


def test_empty_cut_never_hides_valid_input_and_nonempty_cut_is_preferred(tmp_path):
    for name in ("40-ch0-001.event.dat", "40-ch0-ecu_001.hk"):
        packet(tmp_path, "X/" + name)
        (tmp_path / "X" / (name + ".cut")).write_bytes(b"")
    profile = default_profile("15B")
    profile["discovery"]["rules"] = [profile["discovery"]["rules"][1]]
    result = discover(tmp_path, profile)
    assert len(result["rows"]) == 1 and not result["errors"]
    assert not result["rows"][0]["event"].endswith(".cut")
    packet(tmp_path, "X/40-ch0-001.event.dat.cut")
    packet(tmp_path, "X/40-ch0-ecu_001.hk.cut")
    assert discover(tmp_path, profile)["rows"][0]["event"].endswith(".cut")


def test_rule_ambiguity_is_visible_and_not_resolved_by_sort_order(tmp_path):
    packet(tmp_path, "Temp_Vbias/0C_280_001.event.dat")
    packet(tmp_path, "Temp_Vbias/0C_280_002.event.dat")
    packet(tmp_path, "Temp_Vbias/0C_280_ecu_003.hk")
    profile = default_profile()
    profile["discovery"]["rules"] = [profile["discovery"]["rules"][0]]
    result = discover(tmp_path, profile)
    assert result["rows"] == []
    assert len(result["ambiguous"]) == 1
    assert "无法唯一配对" in result["errors"][0]


def test_unmatched_files_are_reported_for_new_extensions_not_only_legacy_names(tmp_path):
    packet(tmp_path, "Temp_Vbias/new-convention.bin")
    packet(tmp_path, "Temp_Vbias/inspection.png")
    profile = default_profile()
    profile["discovery"]["rules"] = [profile["discovery"]["rules"][0]]
    result = discover(tmp_path, profile)
    assert len(result["unmatched"]) == 1
    assert result["unmatched"][0]["path"].endswith("new-convention.bin")


def test_duplicate_manifest_destinations_and_reused_files_fail(tmp_path):
    for name in ("a.evt", "a.hk", "b.evt", "b.hk"):
        packet(tmp_path, name)
    profile = default_profile()
    row = {"kind": "tb", "event": "a.evt", "hk": "a.hk", "stem": "same"}
    profile["discovery"].update(mode="manifest", manifest=[row, {**row, "event": "b.evt", "hk": "b.hk"}])
    result = discover(tmp_path, profile)
    assert result["rows"] == [] and "stem 重复" in result["errors"][0]
    profile["discovery"]["manifest"] = [row, {**row, "stem": "another"}]
    result = discover(tmp_path, profile)
    assert result["rows"] == [] and any("重复引用" in error for error in result["errors"])


def test_source_pairing_normalizes_spacing_alias_and_hour_exposure(tmp_path):
    packet(tmp_path, "source/co60_1h_177.event.dat")
    packet(tmp_path, "source/Co60_ 1h_ecu_019.hk")
    packet(tmp_path, "source/bkg_30m_180.event.dat")
    packet(tmp_path, "source/bkg_30m_ecu_022.hk")
    profile = default_profile()
    profile["discovery"]["rules"] = [profile["discovery"]["rules"][2]]
    result = discover(tmp_path, profile)
    assert not result["errors"]
    source = next(row for row in result["rows"] if row["kind"] == "source")
    assert source["stem"] == "src_Co60_60m"
    assert source["peak_energies_keV"] == [1173.2, 1332.5]
    assert result["counts"] == {"source": 1, "background": 1}


def test_multiple_source_backgrounds_require_explicit_selection(tmp_path):
    for prefix in ("bkg_10m", "bkg_30m"):
        packet(tmp_path, f"source/{prefix}_001.event.dat")
        packet(tmp_path, f"source/{prefix}_ecu_002.hk")
    profile = default_profile()
    profile["discovery"]["rules"] = [profile["discovery"]["rules"][2]]
    result = discover(tmp_path, profile)
    assert not result["rows"]
    assert any("唯一全局本底" in message for message in result["errors"])
    profile["discovery"].update(
        mode="manifest",
        manifest=[
            {
                "kind": "background",
                "event": "source/bkg_30m_001.event.dat",
                "hk": "source/bkg_30m_ecu_002.hk",
                "exposure_minutes": 30,
            }
        ],
    )
    selected = discover(tmp_path, profile)
    assert not selected["errors"] and selected["counts"] == {"background": 1}


def test_unknown_source_requires_explicit_energies(tmp_path):
    packet(tmp_path, "signal.bin")
    packet(tmp_path, "house.bin")
    profile = default_profile()
    profile["discovery"].update(
        mode="manifest",
        manifest=[
            {
                "kind": "source",
                "event": "signal.bin",
                "hk": "house.bin",
                "source": "unidentified",
                "exposure_minutes": 10,
            }
        ],
    )
    preview = discover(tmp_path, profile)
    assert not preview["rows"] and "未知发射源" in preview["errors"][0]


@pytest.mark.parametrize("energies", ["59.5", [59.5, 59.5], [float("nan")], [0]])
def test_source_energy_metadata_cannot_silently_change_numeric_meaning(tmp_path, energies):
    packet(tmp_path, "signal.bin")
    packet(tmp_path, "house.bin")
    profile = default_profile()
    profile["discovery"].update(
        mode="manifest",
        manifest=[
            {
                "kind": "source",
                "event": "signal.bin",
                "hk": "house.bin",
                "source": "Am241",
                "exposure_minutes": 10,
                "peak_energies_keV": energies,
            }
        ],
    )
    preview = discover(tmp_path, profile)
    assert not preview["rows"] and preview["errors"]


def test_partial_profiles_and_xml_snapshot_metadata_are_retained():
    profile = validate_profile(
        {"xml": {"event": {"snapshot_path": "C:/runs/config/event.xml", "sha256": "abc"}}}
    )
    assert profile["xml"]["event"]["sha256"] == "abc"
    assert profile["parser"]["fields"]["channel"] == "channel_n"


def test_xml_limit_is_utf8_bytes_instead_of_character_count():
    profile = default_profile()
    profile["xml"]["event"]["text"] = "中" * (1024 * 1024 // 3 + 1)
    with pytest.raises(ValueError, match="1 MiB"):
        validate_profile(profile)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["parser"].update(endian="guess"), "MSB"),
        (lambda p: p["parser"]["transform"].update(base_divisor=0), "base_divisor"),
        (lambda p: p["parser"]["transform"].update(temp_scale=float("nan")), "有限数值"),
        (lambda p: p["parser"].update(event_multi_evt=0), "正整数"),
        (lambda p: p["parser"]["conditions"].update(mode="fixed"), "固定工况"),
        (lambda p: p["discovery"]["rules"][0].update(event_regex="["), "正则表达式无效"),
        (lambda p: p["discovery"]["rules"][0].update(event_regex="^anything$"), "命名组"),
    ],
)
def test_invalid_profiles_are_actionable(mutate, message):
    profile = default_profile()
    mutate(profile)
    with pytest.raises(ValueError, match=message):
        validate_profile(profile)
