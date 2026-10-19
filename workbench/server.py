"""Loopback HTTP entry point; no framework or external service required."""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .service import Workbench, clean


def make_handler(workbench):
    static = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "GRIDWorkbench/0.1"

        def log_message(self, fmt, *args):
            # Keep the console usable while the UI polls running jobs.
            if len(args) < 2 or str(args[1]) not in ("200", "304"):
                super().log_message(fmt, *args)

        def _headers(self):
            host = self.headers.get("Host", "")
            if host.split(":")[0] not in ("127.0.0.1", "localhost"):
                raise PermissionError("Only localhost requests are supported")
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://{host}",):
                raise PermissionError("Cross-origin requests are not allowed")

        def _json(self, status, data):
            body = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, root, relative):
            root = root.resolve()
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise FileNotFoundError("文件不存在")
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header(
                "Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else "")
            )
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            # JSON, logs and data stay downloadable/readable; HTML only from the static root.
            if root != static.resolve() and target.suffix.lower() in (".html", ".svg", ".js"):
                self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            with target.open("rb") as source:
                while block := source.read(1024 * 1024):
                    self.wfile.write(block)

        def _body(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > 1024 * 1024:
                raise ValueError("请求过大")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求必须为 JSON 对象")
            return data

        def _dispatch(self):
            self._headers()
            url = urlsplit(self.path)
            path = unquote(url.path)
            parts = path.strip("/").split("/")
            query = parse_qs(url.query)
            if self.command == "GET":
                if path == "/":
                    return self._file(static, "index.html")
                if path == "/static/plotly.min.js":
                    import plotly

                    return self._file(Path(plotly.__file__).parent / "package_data", "plotly.min.js")
                if path.startswith("/static/"):
                    return self._file(static, path[len("/static/") :])
                if path == "/api/bootstrap":
                    return self._json(200, workbench.bootstrap())
                if path == "/api/health":
                    return self._json(200, {"status": "ok"})
                if path == "/api/directories":
                    return self._json(200, workbench.directories(query.get("path", [None])[0]))
                if path.startswith("/api/history/"):
                    return self._file(workbench.workspace / "products", path[len("/api/history/") :])
                if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                    run_id = parts[2]
                    if len(parts) == 3:
                        return self._json(200, workbench.view(run_id))
                    if len(parts) == 4 and parts[3] == "tb-versions":
                        return self._json(200, workbench.tb_versions(run_id))
                    if len(parts) >= 6 and parts[3] == "history":
                        from .service import LAYERS

                        if parts[4] not in LAYERS:
                            raise ValueError("未知处理层")
                        state = workbench.read(run_id)
                        return self._file(
                            workbench.adapter_for(state).historical_layer(state["payload"], parts[4]),
                            "/".join(parts[5:]),
                        )
                    if len(parts) == 4 and parts[3] == "surface":
                        return self._json(200, workbench.surface(run_id, int(query.get("channel", [0])[0])))
                    if len(parts) == 4 and parts[3] == "spectrum":
                        return self._json(
                            200,
                            workbench.spectrum(
                                run_id,
                                query.get("stem", [""])[0],
                                int(query.get("channel", [0])[0]),
                                query.get("layer", ["TB_L1"])[0],
                            ),
                        )
                    if len(parts) == 4 and parts[3] == "ec-spectrum":
                        return self._json(
                            200,
                            workbench.ec_spectrum(
                                run_id,
                                query.get("layer", ["EC_L0"])[0],
                                int(query.get("channel", [0])[0]),
                                stem=query.get("stem", [None])[0],
                                point_id=query.get("point_id", [None])[0],
                            ),
                        )
                    if len(parts) == 4 and parts[3] == "ec-overview":
                        return self._json(
                            200,
                            workbench.ec_overview(
                                run_id, int(query.get("channel", [0])[0]), query.get("layer", ["EC_L2"])[0]
                            ),
                        )
                    if len(parts) == 5 and parts[3] == "jobs":
                        return self._json(200, workbench.job(run_id, parts[4]))
                    if len(parts) >= 5 and parts[3] == "files":
                        return self._file(workbench.root(run_id), "/".join(parts[4:]))
            if self.command == "POST":
                body = self._body()
                if path == "/api/runs":
                    return self._json(
                        201, workbench.create(body.get("payload"), body.get("name"), body.get("dataset_id"))
                    )
                if path == "/api/datasets/scan":
                    return self._json(
                        200,
                        workbench.scan_datasets(
                            **{key: body.get(key) for key in ("raw_dir", "product_dir", "payload")}
                        ),
                    )
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "jobs":
                    return self._json(202, workbench.submit(parts[2], body))
            if (
                self.command == "PATCH"
                and len(parts) == 4
                and parts[:2] == ["api", "runs"]
                and parts[3] == "config"
            ):
                return self._json(200, workbench.patch(parts[2], self._body()))
            if (
                self.command == "PATCH"
                and len(parts) == 4
                and parts[:2] == ["api", "runs"]
                and parts[3] == "dependency"
            ):
                body = self._body()
                return self._json(
                    200, workbench.select_tb(parts[2], body.get("tb_version_id"), body.get("path"))
                )
            raise FileNotFoundError("接口不存在")

        def _request(self):
            try:
                self._dispatch()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        do_GET = do_POST = do_PATCH = _request

    return Handler


@contextlib.contextmanager
def workspace_lock(workspace):
    directory = Path(workspace).resolve() / "runs"
    directory.mkdir(exist_ok=True)
    with (directory / ".workbench.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("此工作区已有运行中的 Workbench 服务") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("此工作区已有运行中的 Workbench 服务") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="GRID calibration review workbench")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Bind first: a duplicate startup must never recover another live process's jobs.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    try:
        with workspace_lock(args.workspace):
            workbench = Workbench(args.workspace)
            server.RequestHandlerClass = make_handler(workbench)
            print(f"GRID Calibration Workbench: http://127.0.0.1:{args.port}", flush=True)
            print(f"Workspace: {workbench.workspace}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                workbench.close()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
