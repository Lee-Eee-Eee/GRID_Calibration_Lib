"""Run a representative seven-layer acceptance against a running local workbench."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--payload", choices=("14B", "15B"), default="15B")
    args = parser.parse_args()

    def api(path, body=None):
        request = Request(
            args.url.rstrip("/") + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=120) as response:
            return json.load(response)

    def protected():
        # Detect accidental writes without hashing gigabytes of raw events.
        roots = ("14、15B标定数据", "products", "cali_format", "cali_format.wiki")
        return {
            path.relative_to(args.workspace).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
            for name in roots
            for path in (args.workspace / name).rglob("*")
            if path.is_file()
        }

    before = protected()
    run = api("/api/runs", {"payload": args.payload, "name": f"{args.payload} · 七层实际验收"})
    prefix = f"/api/runs/{run['id']}"
    report = {"run_id": run["id"], "payload": args.payload, "scope": "representative", "jobs": []}
    for layer in ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3"):
        job = api(prefix + "/jobs", {"layer": layer, "representative": True})
        deadline = time.monotonic() + 900
        while job["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{layer} exceeded 15 minutes; inspect the saved job log")
            time.sleep(0.5)
            job = api(prefix + "/jobs/" + job["id"])
        report["jobs"].append(job)
        print(
            f"{layer}: {job['status']}; validation={job.get('validation', {}).get('status', 'unavailable')}",
            flush=True,
        )
        if job["status"] != "succeeded":
            break
    report["protected_inputs_unchanged"] = before == protected()
    report["complete"] = len(report["jobs"]) == 7 and all(j["status"] == "succeeded" for j in report["jobs"])
    report["state"] = api(prefix)
    path = args.workspace / "runs" / run["id"] / "acceptance.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Acceptance: {path}", flush=True)
    if not report["complete"] or not report["protected_inputs_unchanged"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
