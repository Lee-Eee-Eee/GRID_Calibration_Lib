"""Reference actions preserve raw spectra and write inspectable analysis records."""

import hashlib
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np
from calibration_lib.workbench.references import ReferenceLibrary
from calibration_lib.workbench.server import make_handler


def spectrum(root, *, ambiguous=False):
    folder = root / "data/imported/GRID-11B_HPGe_data"
    folder.mkdir(parents=True)
    path = folder / "40keV-45kV-6mA-100s.TKA"
    x = np.arange(200)
    counts = 4 + 1000 * np.exp(-0.5 * ((x - 75) / 3) ** 2)
    if ambiguous:
        counts += 1000 * np.exp(-0.5 * ((x - 125) / 3) ** 2)
    path.write_text("100\n101\n" + "\n".join(map(str, np.rint(counts).astype(int))), encoding="utf-8")
    return path


def test_reference_analysis_uses_adc_and_keeps_original_bytes(tmp_path):
    path = spectrum(tmp_path)
    before = path.read_bytes()
    library = ReferenceLibrary(tmp_path)
    catalog = library.catalog()
    assert len(catalog["files"]) == 1
    assert not (tmp_path / "runs").exists()
    result = library.analyze(str(path))
    assert result["status"] == "resolved"
    assert abs(result["peak_detection"]["selected_center"] - 77) < 2
    assert result["source"]["metadata"]["nominal_energy_keV"] == 40
    assert result["source"]["metadata"]["energy_axis_calibrated"] is False
    assert result["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert result["config"]["infer_noise"] is False
    assert json.loads(open(result["record_path"], encoding="utf-8").read()) == result
    assert path.read_bytes() == before


def test_ambiguous_reference_retains_candidates_and_reason(tmp_path):
    path = spectrum(tmp_path, ambiguous=True)
    result = ReferenceLibrary(tmp_path).analyze(str(path))
    assert result["status"] == "needs_review"
    assert "Ambiguous" in result["reason"]
    assert len(result["peak_detection"]["candidates"]) >= 2
    assert result["peak_detection"]["selected_center"] is None


def test_reference_http_read_does_not_run_analysis(tmp_path):
    path = spectrum(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SimpleNamespace(workspace=tmp_path)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    try:
        connection.request("GET", "/api/reference-spectra")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["files"][0]["filename"] == path.name
        assert not (tmp_path / "runs").exists()
        connection.request(
            "POST",
            "/api/reference-spectra/analyze",
            json.dumps({"path": str(path)}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["status"] == "resolved"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
