"""Source inventories inspect metadata and preserve their read-only inputs."""

import hashlib
import http.client
import json
import os
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from calibration_lib.workbench import source_catalog
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench
from calibration_lib.workbench.source_catalog import SourceCatalog


@pytest.fixture
def source_inputs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "read-only-input"
    paths = {
        "14B/Temp_Vbias/a.event.dat": b"event bytes",
        "14B/Temp_Vbias/a.hk": b"hk bytes",
        "GRID-11B_HPGe_data/Co60.TKA": b"300\n300\n10\n20\n",
        "GRID-11B_HPGe_data/Co60.CNF": b"binary spectrum",
        "GRID-15B/protocol.xml": b"<protocol/>",
        "GRID-15B/result.json": b"[]",
        "run_me.py": b"raise RuntimeError('source script must never run')",
        "14B/Temp_Vbias/incomplete.dat~": b"temporary",
    }
    for name, data in paths.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return workspace, source


def source_bytes(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_source_index_records_file_metadata_and_group_statistics_without_reading_content(
    source_inputs, monkeypatch
):
    workspace, source = source_inputs
    before = source_bytes(source)
    original_open = Path.open

    def reject_input_reads(path, *args, **kwargs):
        if path.is_relative_to(source):
            raise AssertionError("Index attempted to open source file content")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_input_reads)
    catalog = SourceCatalog(workspace)
    result = catalog.index(str(source), "Detector acquisitions")
    assert result["label"] == "Detector acquisitions"
    assert result["file_count"] == 7
    assert result["status"] == "complete"
    assert result["errors"] == []
    assert result["metadata_only"] and not result["content_hashed"]
    assert result["skipped_counts"] == {"temporary_suffix": 1}
    assert result["stats"]["by_payload"]["14B"]["files"] == 2
    assert result["stats"]["by_payload"]["11B"]["files"] == 2
    assert result["stats"]["by_payload"]["15B"]["files"] == 2
    assert result["stats"]["by_type"]["event_packets"]["files"] == 1
    assert result["stats"]["by_type"]["cnf_spectrum"]["files"] == 1
    assert result["stats"]["by_directory"]["14B/Temp_Vbias"]["files"] == 2
    assert result["changes"]["baseline"]
    assert all(not Path(item["path"]).is_absolute() and item["mtime_ns"] > 0 for item in result["files"])
    assert all(item["stable"] for item in result["files"])
    saved = workspace / "runs/_sources" / result["id"] / "index.json"
    assert saved.is_file()
    assert catalog.get(result["id"]) == result
    monkeypatch.setattr(Path, "open", original_open)
    assert source_bytes(source) == before


def test_rescan_identifies_added_removed_and_size_or_mtime_changes(source_inputs):
    workspace, source = source_inputs
    catalog = SourceCatalog(workspace)
    first = catalog.index(str(source), "Acquisitions")
    deleted = source / "GRID-15B/result.json"
    deleted.unlink()
    changed = source / "14B/Temp_Vbias/a.hk"
    previous_ns = changed.stat().st_mtime_ns
    changed.write_bytes(b"new hk data and more samples")
    os.utime(changed, ns=(previous_ns + 20_000_000, previous_ns + 20_000_000))
    (source / "14B/new.dat").write_bytes(b"new acquisition")
    second = catalog.index(str(source))
    assert second["id"] == first["id"]
    assert second["label"] == "Acquisitions"
    assert second["previous_indexed_at"] == first["indexed_at"]
    assert second["changes"]["added"] == ["14B/new.dat"]
    assert second["changes"]["removed"] == ["GRID-15B/result.json"]
    assert [item["path"] for item in second["changes"]["modified"]] == ["14B/Temp_Vbias/a.hk"]
    assert second["changes"]["modified"][0]["previous_size"] < second["changes"]["modified"][0]["size"]
    assert catalog.index(str(source))["changes"]["modified_count"] == 0


def test_saved_get_and_list_do_not_rescan_or_require_source_to_exist(source_inputs):
    workspace, source = source_inputs
    catalog = SourceCatalog(workspace)
    result = catalog.index(str(source))
    source.rename(source.with_name("source-offline"))
    assert catalog.get(result["id"])["files"] == result["files"]
    summary = SourceCatalog(workspace).list()
    assert summary["errors"] == []
    assert summary["sources"][0]["file_count"] == 7
    assert summary["sources"][0]["root"] == str(source)
    assert summary["sources"][0]["errors_count"] == 0


def test_directory_errors_are_retained_with_partial_inventory(source_inputs, monkeypatch):
    workspace, source = source_inputs
    original_scandir = os.scandir

    def unavailable(path):
        if Path(path) == source / "GRID-15B":
            raise PermissionError("simulated offline cloud directory")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", unavailable)
    result = SourceCatalog(workspace).index(str(source))
    assert result["status"] == "partial"
    assert result["file_count"] == 5
    assert result["errors"] == [
        {
            "path": "GRID-15B",
            "operation": "list_directory",
            "message": "simulated offline cloud directory",
            "exception": "PermissionError",
        }
    ]


def test_files_changing_during_metadata_pass_are_marked_unstable(source_inputs, monkeypatch):
    workspace, source = source_inputs
    original_signature = source_catalog._signature
    changed = False

    def signature(info):
        nonlocal changed
        result = original_signature(info)
        if not changed:
            changed = True
            # The root script is encountered before its subdirectories.
            (source / "run_me.py").write_bytes(b"still only indexed; content updated during scan")
        return result

    monkeypatch.setattr(source_catalog, "_signature", signature)
    result = SourceCatalog(workspace).index(str(source))
    assert result["status"] == "unstable"
    assert [item["path"] for item in result["unstable"]] == ["run_me.py"]
    assert result["unstable"][0]["reason"] == "changed_during_scan"
    assert not next(item for item in result["files"] if item["path"] == "run_me.py")["stable"]


def test_windows_junction_is_skipped_without_traversing_its_target(source_inputs, monkeypatch):
    workspace, source = source_inputs
    junction = source / "loop"
    junction.mkdir()
    original_scandir = os.scandir

    class JunctionEntry:
        name = "loop"

        @staticmethod
        def stat(follow_symlinks):
            assert follow_symlinks is False
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)

    class Entries:
        def __enter__(self):
            return iter([JunctionEntry()])

        def __exit__(self, *_args):
            return False

    def scandir(path):
        if Path(path) == source:
            return Entries()
        if Path(path) == junction:
            raise AssertionError("Junction target must not be traversed")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    result = SourceCatalog(workspace).index(str(source))
    assert result["files"] == []
    assert result["skipped"] == [{"path": "loop", "reason": "link_or_junction"}]


def test_symlink_loop_and_external_file_are_not_followed_when_platform_allows(source_inputs):
    workspace, source = source_inputs
    outside = source.parent / "private.txt"
    outside.write_text("must not be indexed", encoding="utf-8")
    try:
        (source / "loop").symlink_to(source, target_is_directory=True)
        (source / "outside.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Host does not allow creating symlinks")
    result = SourceCatalog(workspace).index(str(source))
    assert result["file_count"] == 7
    assert result["skipped_counts"]["link_or_junction"] == 2
    assert not any(
        item["path"].startswith("loop/") or item["path"] == "outside.txt" for item in result["files"]
    )


@pytest.mark.parametrize("value", [None, "", "absent", "../read-only-input/run_me.py"])
def test_invalid_root_does_not_create_catalog(source_inputs, value):
    workspace, _ = source_inputs
    catalog = SourceCatalog(workspace)
    with pytest.raises(ValueError):
        catalog.index(value)
    assert not catalog.directory.exists()


def test_source_cannot_index_its_own_output_and_invalid_id_cannot_escape_store(source_inputs):
    workspace, _ = source_inputs
    catalog = SourceCatalog(workspace)
    with pytest.raises(ValueError, match="runs/_sources"):
        catalog.index(str(workspace))
    with pytest.raises(ValueError):
        catalog.get("../../source")


def test_source_index_http_routes_persist_and_get_reads_saved_data(source_inputs):
    workspace, source = source_inputs
    service = Workbench(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {"Content-Type": "application/json"} if body is not None else {},
            )
            response = connection.getresponse()
            content = json.loads(response.read())
            connection.close()
            return response.status, content

        assert request("GET", "/api/sources")[1] == {"sources": [], "errors": []}
        status, indexed = request(
            "POST", "/api/sources/index", {"root": str(source), "label": "Read-only source"}
        )
        assert status == 201 and indexed["file_count"] == 7
        source.rename(source.with_name("source-unmounted"))
        assert request("GET", f"/api/sources/{indexed['id']}")[1] == indexed
        assert request("GET", "/api/sources")[1]["sources"][0]["label"] == "Read-only source"
        assert request("POST", "/api/sources/index", {"root": str(source)})[0] == 400
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        service.close()


def test_read_only_source_needs_no_write_access_and_preserves_bytes_and_mtime(source_inputs):
    workspace, source = source_inputs
    paths = [path for path in source.rglob("*") if path.is_file()]
    original_modes = {path: path.stat().st_mode for path in paths}
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    try:
        for path in paths:
            path.chmod(stat.S_IREAD)
        result = SourceCatalog(workspace).index(str(source), "Read-only mounted data")
        assert result["file_count"] == 7 and result["status"] == "complete"
        for path, (content, mtime) in before.items():
            assert path.read_bytes() == content
            assert path.stat().st_mtime_ns == mtime
            assert not path.stat().st_mode & stat.S_IWRITE
    finally:
        for path, mode in original_modes.items():
            path.chmod(mode)
