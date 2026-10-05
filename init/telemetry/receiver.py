#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Local OTLP/HTTP JSON receiver for Claude Code's native telemetry.

Claude Code exports metrics and events over OTLP but has no file exporter. This receiver accepts that export on
localhost and appends one JSON line per data point or event to `<logs>/<session-id>/metrics.jsonl` and `events.jsonl`,
where DuckDB reads them directly.

`ensure` is for the SessionStart hook: it starts `serve` in the background unless one already listens.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True

HOST = "127.0.0.1"
UNATTRIBUTED = "_unattributed"
IDLE_SECONDS = 3600
SESSION_KEY = "session.id"
NUMBER_FIELDS = ("asDouble", "asInt", "sum")


def attribute_value(value: dict):
    """An OTLP AnyValue as plain JSON."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [attribute_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attributes(value["kvlistValue"].get("values", []))
    return None


def attributes(pairs: list[dict]) -> dict:
    return {pair["key"]: attribute_value(pair.get("value", {})) for pair in pairs}


def iso(nanos: str | int | None) -> str | None:
    if not nanos or int(nanos) == 0:
        return None
    return datetime.fromtimestamp(int(nanos) / 1e9, UTC).isoformat(timespec="milliseconds")


def metric_rows(payload: dict) -> list[tuple[str, dict]]:
    """(session id, row) per data point. Counters arrive as deltas, so rows are summed, not last-valued."""
    rows = []
    for resource in payload.get("resourceMetrics", []):
        shared = attributes(resource.get("resource", {}).get("attributes", []))
        for scope in resource.get("scopeMetrics", []):
            for metric in scope.get("metrics", []):
                kind = next((k for k in ("sum", "gauge", "histogram") if k in metric), None)
                for point in metric.get(kind, {}).get("dataPoints", []) if kind else []:
                    attrs = {**shared, **attributes(point.get("attributes", []))}
                    value = next((point[f] for f in NUMBER_FIELDS if f in point), None)
                    rows.append(
                        (
                            str(attrs.get(SESSION_KEY) or UNATTRIBUTED),
                            {
                                "ts": iso(point.get("timeUnixNano")),
                                "name": metric.get("name"),
                                "unit": metric.get("unit"),
                                "kind": kind,
                                "value": float(value) if value is not None else None,
                                "attributes": attrs,
                            },
                        )
                    )
    return rows


def event_rows(payload: dict) -> list[tuple[str, dict]]:
    rows = []
    for resource in payload.get("resourceLogs", []):
        shared = attributes(resource.get("resource", {}).get("attributes", []))
        for scope in resource.get("scopeLogs", []):
            for record in scope.get("logRecords", []):
                attrs = {**shared, **attributes(record.get("attributes", []))}
                body = attribute_value(record.get("body", {}))
                rows.append(
                    (
                        str(attrs.get(SESSION_KEY) or UNATTRIBUTED),
                        {
                            "ts": iso(record.get("timeUnixNano") or record.get("observedTimeUnixNano")),
                            "event": attrs.get("event.name") or body,
                            "attributes": attrs,
                        },
                    )
                )
    return rows


def safe_session(session: str) -> str:
    """The session id names a folder: keep it to one path segment."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in session).lstrip(".")
    return cleaned or UNATTRIBUTED


def write_rows(logs: Path, filename: str, rows: list[tuple[str, dict]]) -> None:
    by_session: dict[str, list[str]] = {}
    for session, row in rows:
        by_session.setdefault(safe_session(session), []).append(json.dumps(row, ensure_ascii=False))
    for session, lines in by_session.items():
        folder = logs / session
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / filename).open("a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")


ROUTES = {"/v1/metrics": ("metrics.jsonl", metric_rows), "/v1/logs": ("events.jsonl", event_rows)}


def make_server(logs: Path, port: int) -> ThreadingHTTPServer:
    lock = threading.Lock()
    state = {"last": time.monotonic()}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            route = ROUTES.get(self.path.split("?")[0])
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            state["last"] = time.monotonic()
            if route is None:
                return self.reply(404, b"{}")
            try:
                rows = route[1](json.loads(body or b"{}"))
            except (ValueError, KeyError, TypeError, AttributeError):
                return self.reply(400, b"{}")
            with lock:
                write_rows(logs, route[0], rows)
            self.reply(200, b"{}")

        def reply(self, status: int, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((HOST, port), Handler)
    server.idle_seconds = lambda: time.monotonic() - state["last"]  # type: ignore[attr-defined]
    return server


def serve(logs: Path, port: int) -> int:
    try:
        server = make_server(logs, port)
    except OSError:
        return 0  # another receiver already holds the port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while server.idle_seconds() < IDLE_SECONDS:  # type: ignore[attr-defined]
        time.sleep(30)
    server.shutdown()
    return 0


def listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def ensure(logs: Path, port: int) -> int:
    if not listening(port):
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve", "--logs", str(logs), "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("serve", "ensure"))
    parser.add_argument("--logs", type=Path, required=True, help="folder that receives <session-id>/*.jsonl")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    return {"serve": serve, "ensure": ensure}[args.command](args.logs, args.port)


if __name__ == "__main__":
    sys.exit(main())
