
import pytest
from calibration_lib.tools.migrate_calibration_data import file_sha256, migrate_sources


def test_verified_copy_preserves_tree_and_idempotent_rerun(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "sub").mkdir()
    original = source / "sub" / "20keV.TKA"
    original.write_bytes(b"30\n30\n5\n")
    (source / "private.key").write_text("not calibration input")
    first = migrate_sources({"HPGe-11B": source}, tmp_path / "imported")
    output = tmp_path / "imported" / "HPGe-11B" / "sub" / "20keV.TKA"
    assert output.read_bytes() == original.read_bytes()
    assert first["actions"] == {"copied": 1}
    assert first["files"][0]["sha256"] == file_sha256(original)
    assert first["ignored"][0]["source"].endswith("private.key")
    assert not (output.parent.parent / "private.key").exists()
    assert migrate_sources({"HPGe-11B": source}, tmp_path / "imported")["actions"] == {"verified_existing": 1}


def test_preflight_conflict_stops_before_any_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.json").write_text("first")
    (source / "z.json").write_text("new")
    target = tmp_path / "output" / "payload"
    target.mkdir(parents=True)
    (target / "z.json").write_text("existing")
    with pytest.raises(FileExistsError, match="no files were overwritten"):
        migrate_sources({"payload": source}, target.parent)
    assert not (target / "a.json").exists()
    assert (target / "z.json").read_text() == "existing"


def test_no_overlapping_trees_and_no_unsafe_labels(tmp_path):
    with pytest.raises(ValueError, match="overlap"):
        migrate_sources({"nested": tmp_path}, tmp_path / "output")
    with pytest.raises(ValueError, match="label"):
        migrate_sources({"../escape": tmp_path}, tmp_path.parent / "output")
    with pytest.raises(ValueError, match="label"):
        migrate_sources({"CON": tmp_path}, tmp_path.parent / "output")


def test_linked_destination_cannot_escape_import_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.json").write_bytes(b"{}")
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (output / "payload").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require privileges on this host")
    with pytest.raises(ValueError, match="escapes"):
        migrate_sources({"payload": source}, output)
    assert list(outside.iterdir()) == []


def test_duplicate_content_is_reported_without_collapsing_filenames(tmp_path):
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        (root / "same.json").write_bytes(b"{}")
    report = migrate_sources({name: tmp_path / name for name in ("first", "second")}, tmp_path / "output")
    assert len(report["duplicate_content"]) == 1
    assert len(report["duplicate_content"][0]["sources"]) == 2
    assert report["file_count"] == 2
