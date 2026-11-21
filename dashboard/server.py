#!/usr/bin/env python3
"""Read-only dashboard server for the AgentTrace store.

Stdlib only. Serves one static page and a read-only JSON API whose
entire payload is built from the store file through the same query
layer the CLI uses (agenttrace.query) plus the store's own verifier
(agenttrace.store). The server holds no state of its own, opens no
producer artifact, and rejects every non-GET request with 405: there
is no approve / pause / kill / config surface here, by design.

Usage:
    python -m dashboard.server --store drill/output/<trace>/store.jsonl
    python -m dashboard.server --store /path/to/any-store.jsonl --port 8765
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttrace import query as query_mod  # noqa: E402
from agenttrace import store as store_mod  # noqa: E402
from agenttrace.schema import validate_event  # noqa: E402

HERE = Path(__file__).resolve().parent
STREAM_CAP = 150
TREE_CAP_PER_BRANCH = 400


def load_store_events(path: Path) -> list[dict]:
    """Load stored events, tolerating one incomplete trailing line.

    The dashboard tails a store that a live run is appending to; a
    final line caught mid-write is dropped for this read and picked up
    complete on the next poll. Any other malformed line raises --
    loudly, like everywhere else in AgentTrace.
    """
    if not path.exists():
        return []
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = []
    for i, line in enumerate(lines):
        try:
            events.append(validate_event(json.loads(line), stored=True))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return events


def chain_status(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {"status": "empty", "count": 0, "broken_seq": None, "detail": "store is empty"}
    try:
        result = store_mod.verify(path)
    except Exception as exc:  # mid-append read or schema error: report, don't crash
        return {"status": "error", "count": 0, "broken_seq": None, "detail": str(exc)}
    if result.ok:
        return {"status": "ok", "count": result.count, "broken_seq": None,
                "detail": "every hash verified"}
    return {"status": "broken", "count": result.count,
            "broken_seq": result.broken_seq, "detail": result.detail}


def build_state(path: Path) -> dict:
    """The full dashboard payload, from the store alone."""
    events = load_store_events(path)
    chain = chain_status(path)
    traces = sorted({e["trace_id"] for e in events})
    active_trace = events[-1]["trace_id"] if events else None
    tree = []
    if active_trace:
        tree = query_mod.trace_tree(events, active_trace)
        for branch in tree:
            branch["truncated"] = len(branch["events"]) > TREE_CAP_PER_BRANCH
            branch["events"] = branch["events"][-TREE_CAP_PER_BRANCH:]
    stream = [{
        "seq": e["seq"], "ts": e["ts"], "source": e["source"],
        "agent": e["agent"], "type": e["type"], "kind": e["kind"],
        "summary": query_mod.summarize_event(e),
    } for e in events[-STREAM_CAP:]][::-1]
    return {
        "store": path.name,
        "server_time": time.time(),
        "events": len(events),
        "chain": chain,
        "traces": traces,
        "active_trace": active_trace,
        "fleet": query_mod.fleet_status(events, now=time.time()),
        "pending_approvals": query_mod.pending_approvals(events),
        "cost": query_mod.cost_summary(events),
        "tree": tree,
        "stream": stream,
    }


def build_events_after(path: Path, after_seq: int) -> dict:
    """Incremental poll payload: events newer than after_seq + chain."""
    events = load_store_events(path)
    fresh = query_mod.query_events(events)
    fresh = [e for e in fresh if e["seq"] > after_seq]
    return {
        "events": [{
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "agent": e["agent"], "type": e["type"], "kind": e["kind"],
            "summary": query_mod.summarize_event(e),
        } for e in fresh],
        "count": len(events),
        "chain": chain_status(path),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    store_path: Path = Path("store.jsonl")
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(200, (HERE / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json(200, build_state(self.store_path))
            elif parsed.path == "/api/events":
                qs = parse_qs(parsed.query)
                after = int(qs.get("after_seq", ["0"])[0])
                self._json(200, build_events_after(self.store_path, after))
            elif parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _reject(self) -> None:
        self._json(405, {"error": "read-only dashboard: only GET is supported; "
                                  "approvals are resolved at the producer, never here"})

    do_POST = _reject
    do_PUT = _reject
    do_DELETE = _reject
    do_PATCH = _reject
    do_OPTIONS = _reject
    do_HEAD = _reject


def make_server(store_path: str | Path, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundDashboardHandler", (DashboardHandler,),
                   {"store_path": Path(store_path)})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.server", description=__doc__)
    parser.add_argument("--store", required=True, help="AgentTrace store JSONL to view")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.store, args.host, args.port)
    print(f"AgentTrace dashboard (read-only) -> http://{args.host}:{args.port}/ "
          f"store={args.store}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
