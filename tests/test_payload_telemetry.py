"""Named payload electronics conversions and actual packet decoder coverage."""

import binascii
from pathlib import Path

import numpy as np
import pytest
from calibration_lib.common.legacy_parser import (
    convert_hk_bias,
    convert_hk_temperature,
    packet_repeat_options,
    parse_event_hk_to_dataframe,
)
from calibration_lib.workbench.acquisition import (
    default_profile,
    discover,
    tb_histogram_config,
    validate_profile,
)
from calibration_lib.workbench.acquisition_store import prepare_profile

RESOURCES = Path(__file__).resolve().parents[1] / "resources/legacy"


def waveform_packet(channel, pulse=2400):
    packet = bytearray(560)
    packet[:4] = bytes.fromhex("2e2e33ff")
    packet[4:8] = (100 + channel).to_bytes(4, "big")
    packet[20:22] = channel.to_bytes(2, "big")
    packet[26:28] = (256).to_bytes(2, "big")
    packet[540:548] = (12345).to_bytes(8, "big")
    packet[548:550] = (pulse + 100).to_bytes(2, "big")
    packet[550:552] = (400).to_bytes(2, "big")
    packet[554:556] = binascii.crc_hqx(packet[:554], 0).to_bytes(2, "big")
    packet[556:] = bytes.fromhex("22eeff33")
    return packet


def telemetry_packet(voltage=2275, current=10):
    packet = bytearray(106)
    packet[:4] = bytes.fromhex("1a2b3c4d")
    for channel in range(4):
        offset = 80 + 6 * channel
        packet[offset:offset + 2] = voltage.to_bytes(2, "big")
        packet[offset + 2:offset + 4] = current.to_bytes(2, "big")
        packet[offset + 4:offset + 6] = int((255 - 10 if channel == 0 else 24) * 256).to_bytes(2, "big")
    packet[104:] = binascii.crc_hqx(packet[:104], 0).to_bytes(2, "big")
    return packet


def test_named_temperature_and_series_resistance_conversions_match_original_electronics():
    values = np.array([24 * 256, 245 * 256])
    np.testing.assert_allclose(convert_hk_temperature(values, {"temp_mode": "grid10_signed"}), [24, -10])
    voltage, current = np.array([2275, 2280]), np.array([10, 20])
    np.testing.assert_allclose(convert_hk_bias(voltage, current, {"bias_mode": "grid10_current_drop"}),
                               20.57 * 2.5 * voltage / 4096 - 499 * 2.5 * current / (4096 * 548.88))
    np.testing.assert_allclose(convert_hk_bias(voltage, current, {"bias_mode": "grid11_current_drop"}),
                               voltage / 1000 - 499 * current * 1e-6)
    with pytest.raises(ValueError, match="同长度"):
        convert_hk_bias(voltage, np.array([]), {"bias_mode": "grid10_current_drop"})


def test_original_14b_15b_affine_conversions_are_unchanged():
    transform = default_profile("14B")["parser"]["transform"]
    np.testing.assert_allclose(convert_hk_temperature(np.array([30000]), transform), [26.85])
    np.testing.assert_allclose(convert_hk_bias(np.array([28500]), np.array([]), transform), [28.5])
    for payload in ("14B", "15B"):
        parser = default_profile(payload)["parser"]
        assert (parser["event_multi_evt"], parser["event_multi_step"]) == (38, 14)


@pytest.mark.parametrize("key,value", [("temp_mode", "eval"), ("bias_mode", "unknown"),
                                       ("hk_selection", "eval"), ("stable_bias_tolerance", 0)])
def test_unknown_or_invalid_named_conversions_are_rejected(key, value):
    profile = default_profile("10B")
    profile["parser"]["transform"][key] = value
    with pytest.raises(ValueError):
        validate_profile(profile)


def test_current_field_is_required_for_voltage_drop_correction(tmp_path):
    profile = default_profile("10B")
    profile["parser"]["fields"]["current"] = None
    with pytest.raises(ValueError, match="current"):
        validate_profile(profile)
    profile = default_profile("10B")
    profile["parser"]["fields"]["current"] = "missing_current{channel}"
    with pytest.raises(ValueError, match="missing_current"):
        prepare_profile(tmp_path, profile)


def test_xml_geometry_selects_584_and_528_byte_repeat_layouts():
    assert packet_repeat_options(RESOURCES / "hnu_packet.xml", "grid1x_ft_packet") == {"multi_evt": 38, "multi_step": 14}
    assert packet_repeat_options(RESOURCES / "grid_packet.xml", "grid1x_ft_packet") == {"multi_evt": 41, "multi_step": 12}
    assert packet_repeat_options(RESOURCES / "grid10_packet.xml", "grid1x_wf_packet") == {}


def test_real_10b_xml_decoder_reads_negative_temperature_current_and_amplitude(tmp_path):
    event, hk = tmp_path / "observe.dat", tmp_path / "hk.dat"
    event.write_bytes(b"".join(waveform_packet(channel) for channel in range(4)))
    hk.write_bytes(telemetry_packet())
    profile = prepare_profile(tmp_path, default_profile("10B"))
    frame, metadata = parse_event_hk_to_dataframe(event, hk, tmp_path,
                                                 "grid1x_wf_packet", "hk_grid1x_packet", True, profile)
    assert frame.channel.tolist() == [0, 1, 2, 3]
    np.testing.assert_allclose(frame.amp, [2400] * 4)
    np.testing.assert_allclose(frame.temp, [-10, 24, 24, 24])
    np.testing.assert_allclose(frame.bias, 20.57 * 2.5 * 2275 / 4096 - 499 * 2.5 * 10 / (4096 * 548.88))
    assert metadata["crc"]["failed_events"] == 0
    assert metadata["hk_crc"]["failed_packets"] == 0
    assert metadata["acquisition"]["fitting"]["tb_histogram"]["max"] == 16384


def test_invalid_hk_crc_cannot_supply_calibration_temperature_bias(tmp_path):
    event, hk = tmp_path / "observe.dat", tmp_path / "hk.dat"
    event.write_bytes(waveform_packet(0))
    damaged = telemetry_packet()
    damaged[-1] ^= 0xFF
    hk.write_bytes(damaged)
    with pytest.raises(ValueError, match="全部 HK CRC"):
        parse_event_hk_to_dataframe(event, hk, tmp_path, "grid1x_wf_packet", "hk_grid1x_packet", True,
                                    prepare_profile(tmp_path, default_profile("10B")))


def test_10b_working_plateau_survives_a_long_shutdown_hk_tail(tmp_path):
    event, hk = tmp_path / "observe.dat", tmp_path / "hk.dat"
    event.write_bytes(b"".join(waveform_packet(channel) for channel in range(4)))
    # More than tail_samples shutdown records used to make the parser select
    # the low-voltage tail and reject an otherwise valid operating interval.
    hk.write_bytes(telemetry_packet(100) * 10 + telemetry_packet(2275) * 120 + telemetry_packet(150) * 80)
    profile = prepare_profile(tmp_path, default_profile("10B"))
    frame, metadata = parse_event_hk_to_dataframe(event, hk, tmp_path,
                                                 "grid1x_wf_packet", "hk_grid1x_packet", True, profile)
    target = 20.57 * 2.5 * 2275 / 4096 - 499 * 2.5 * 10 / (4096 * 548.88)
    np.testing.assert_allclose(frame.bias, target)
    assert metadata["channels"]["0"]["bias_n_raw"] == 210
    assert metadata["channels"]["0"]["bias_n"] == 60
    assert metadata["hk_crc"]["failed_packets"] == 0


def test_10b_events_without_any_operating_hk_bias_still_fail(tmp_path):
    event, hk = tmp_path / "observe.dat", tmp_path / "hk.dat"
    event.write_bytes(waveform_packet(0))
    hk.write_bytes(telemetry_packet(150) * 80)
    with pytest.raises(ValueError, match="没有有效稳态偏压"):
        parse_event_hk_to_dataframe(event, hk, tmp_path, "grid1x_wf_packet", "hk_grid1x_packet", True,
                                    prepare_profile(tmp_path, default_profile("10B")))


def test_10b_pairing_uses_metadata_exposure_and_keeps_distinct_source_background(tmp_path):
    names = {"tb_data": ["094_observe_50C_290.dat", "094_hk_50C_290.dat"],
             "x_data": ["253_observe_51_ch2.dat", "253_hk_51_ch2.dat"],
             "src_data": ["073_observe_Cs137.dat", "073_hk_Cs137.dat", "075_observe_Cs137_bkg.dat", "075_hk_Cs137_bkg.dat"]}
    for directory, files in names.items():
        (tmp_path / directory).mkdir()
        for filename in files:
            (tmp_path / directory / filename).write_bytes(b"packet")
    preview = discover(tmp_path, default_profile("10B"))
    assert not preview["errors"]
    assert preview["counts"] == {"tb": 1, "xray": 1, "source": 1, "background": 1}
    source = next(row for row in preview["rows"] if row["kind"] == "source")
    assert source["source"] == "Cs137" and source["exposure_minutes"] == 60


@pytest.mark.parametrize("values", [{"max": 100000}, {"bin_width": 0}, {"max": 10, "min": 20},
                                   {"max": 65536, "bin_width": .1}, {"max": 10, "bin_width": 3}])
def test_histogram_bounds_and_bin_count_are_validated(values):
    with pytest.raises(ValueError):
        tb_histogram_config({"fitting": {"tb_histogram": values}})
