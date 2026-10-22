"""Packet streaming preserves feature values/CRC across arbitrary read boundaries."""

import contextlib
import io
from pathlib import Path

import numpy as np
from calibration_lib.common import legacy_parser
from test_payload_telemetry import waveform_packet


def parser():
    resources = legacy_parser._ensure_parser_path(Path(__file__).resolve().parents[2])
    from parse_grid_data import parse_grid_data_new

    return parse_grid_data_new, resources / "grid10_packet.xml"


def test_streamed_waveforms_match_full_packet_decode_without_unused_sample_matrix(tmp_path, monkeypatch):
    decode, xml = parser()
    event = tmp_path / "event.dat"
    packets = [waveform_packet(channel % 4, pulse=2000 + channel) for channel in range(35)]
    packets[13][123] ^= 1  # Keep the failed CRC in its original event position.
    event.write_bytes(b"prefix!" + b"".join(packets[:12]) + b"gap" + b"".join(packets[12:]) + b"tail")
    with contextlib.redirect_stdout(io.StringIO()):
        expected, _ = decode(str(event), xml_file=str(xml), data_tag="grid1x_wf_packet")
    monkeypatch.setattr(legacy_parser, "PARSER_CHUNK_BYTES", 1703)
    sizes = []

    def observed(*args, **kwargs):
        sizes.append(kwargs["data"].nbytes)
        return decode(*args, **kwargs)

    actual = legacy_parser._parse_silent(observed, event, "grid1x_wf_packet", xml)
    assert len(sizes) > 1 and max(sizes) <= 1703 + 559
    assert "waveform_data" not in actual
    assert actual["crc_check"].tolist() == [index != 13 for index in range(35)]
    for key in actual:
        np.testing.assert_array_equal(np.atleast_1d(expected[key]), actual[key], err_msg=key)


def test_one_packet_and_empty_chunks_do_not_drop_or_duplicate_events(tmp_path, monkeypatch):
    decode, xml = parser()
    event = tmp_path / "one.dat"
    event.write_bytes(b"x" * 5000 + waveform_packet(2, pulse=3333) + b"y" * 5000)
    monkeypatch.setattr(legacy_parser, "PARSER_CHUNK_BYTES", 561)
    actual = legacy_parser._parse_silent(decode, event, "grid1x_wf_packet", xml)
    assert actual["channel_n"].tolist() == [2]
    assert actual["data_max"].tolist() == [3433]
    assert actual["crc_check"].tolist() == [True]


def test_truncated_packet_is_not_decoded_or_completed_with_unrelated_bytes(tmp_path, monkeypatch):
    decode, xml = parser()
    event = tmp_path / "truncated.dat"
    event.write_bytes(bytes(waveform_packet(0)) + bytes(waveform_packet(1))[:400])
    monkeypatch.setattr(legacy_parser, "PARSER_CHUNK_BYTES", 563)
    actual = legacy_parser._parse_silent(decode, event, "grid1x_wf_packet", xml)
    assert actual["channel_n"].tolist() == [0]
    assert actual["crc_check"].tolist() == [True]


def test_packet_gather_above_one_batch_keeps_order_and_crc(tmp_path):
    decode, xml = parser()
    event = tmp_path / "batch.dat"
    event.write_bytes(b"".join(waveform_packet(index % 4) for index in range(8201)))
    with contextlib.redirect_stdout(io.StringIO()):
        actual, indices = decode(str(event), xml_file=str(xml), data_tag="grid1x_wf_packet",
                                 skip_et=["waveform_data"])
    assert len(indices) == 8201
    np.testing.assert_array_equal(actual["channel_n"], np.arange(8201) % 4)
    assert actual["crc_check"].all()
