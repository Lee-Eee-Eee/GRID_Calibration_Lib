import hashlib

import pytest
from calibration_lib.common.reference_spectra import read_reference_spectrum, scan_reference_directory


def test_hpge_header_does_not_become_counts_or_shift_channel_axis(tmp_path):
    path = tmp_path / "20keV-25kV-0.4mA-30s.TKA"
    raw = b"30\r\n31\r\n0\r\n2\r\n150\r\n3\r\n"
    path.write_bytes(raw)
    path.with_suffix(".CNF").write_bytes(b"binary preserved separately")
    spectrum = read_reference_spectrum(path)
    assert spectrum["x"] == [0, 1, 2, 3, 4, 5]
    assert spectrum["y"] == [0, 0, 0, 2, 150, 3]
    assert spectrum["sha256"] == hashlib.sha256(raw).hexdigest()
    meta = spectrum["metadata"]
    assert meta["live_time_seconds"] == 30
    assert meta["second_header_value"] == 31 and meta["second_header_meaning"] == "unknown"
    assert meta["nominal_energy_keV"] == 20 and meta["tube_current_mA"] == 0.4
    assert meta["x_quantity"] == "channel" and meta["energy_axis_calibrated"] is False
    assert meta["total_counts"] == 155 and meta["max_count_channel"] == 4
    assert meta["binary_companion"].endswith(".CNF")


@pytest.mark.parametrize("raw", [b"30\n31", b"30\n31\n-1", b"30\n31\n1.2", b"0\n31\n5", b"30\n31\nNaN", b"not a TKA"])
def test_bad_reference_spectrum_rejected(tmp_path, raw):
    path = tmp_path / "test.TKA"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_reference_spectrum(path)


def test_reference_catalog_numerical_energy_order_and_errors(tmp_path):
    for name in ("100keV", "20keV", "unlabelled"):
        (tmp_path / f"{name}.TKA").write_text("10\n11\n5\n")
    (tmp_path / "bad.TKA").write_text("invalid")
    (tmp_path / "ignored.CNF").write_bytes(b"binary")
    rows = scan_reference_directory(tmp_path)
    assert [row["filename"] for row in rows] == ["20keV.TKA", "100keV.TKA", "bad.TKA", "unlabelled.TKA"]
    assert rows[2]["status"] == "invalid" and rows[2]["error"]
    assert rows[-1]["status"] == "ready"
    assert "x" not in rows[0] and "y" not in rows[0]
    assert "nominal_energy_keV" not in rows[-1]["metadata"]
