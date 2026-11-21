#!/usr/bin/env python3
"""AgentTrace dashboard demo: honest liveness, no simulated feed.

Starts the read-only dashboard server against a fresh live store,
runs the actual flagship drill (drill/run_drill.py) as a subprocess,
and ingests each producer artifact into the live store *as the
producer emits it* -- canary's verdict when beat 1 lands, gate's
audit when beat 2 lands, blast-radius's audit line by line through
beat 3 (so the kill -9 gap and the resume are visible as they
happen), then factory-floor, ledger, and chaos as their beats land.

What the dashboard renders is therefore events from a run actually
executing. Nothing here pre-scripts or animates the feed.

Usage:
    .venv/bin/python -m dashboard.run_demo [--port 8765]
    # then open http://127.0.0.1:8765/ -- Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, store  # noqa: E402
from dashboard.server import make_server  # noqa: E402

POLL_SECONDS = 0.4


class JsonlTail:
    """Incrementally ingest a growing producer JSONL via its ingestor."""

    def __init__(self, path: Path, ingestor, store_path: Path):
        self.path, self.ingestor, self.store_path = path, ingestor, store_path
        self.consumed = 0

    def poll(self) -> int:
        if not self.path.exists():
            return 0
        lines = [l for l in self.path.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if len(lines) <= self.consumed:
            return 0
        fresh = lines[self.consumed:]
        # Only consume lines that parse -- a line caught mid-write is
        # retried on the next poll instead of crashing the tail.
        ready = []
        for line in fresh:
            try:
                json.loads(line)
                ready.append(line)
            except json.JSONDecodeError:
                break
        if not ready:
            return 0
        with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False, encoding="utf-8") as tmp:
            tmp.write("\n".join(ready) + "\n")
            tmp_path = tmp.name
        try:
            events = self.ingestor(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        store.append_events(self.store_path, events)
        self.consumed += len(ready)
        return len(events)


class JsonOnce:
    """Ingest a whole-file producer artifact once it exists and parses."""

    def __init__(self, path: Path, ingestor, store_path: Path):
        self.path, self.ingestor, self.store_path = path, ingestor, store_path
        self.done = False

    def poll(self) -> int:
        if self.done or not self.path.exists():
            return 0
        try:
            events = self.ingestor(self.path)
        except Exception:
            return 0  # file still being written; retry next poll
        store.append_events(self.store_path, events)
        self.done = True
        return len(events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.run_demo", description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--store", default=str(REPO / "dashboard" / "live-store.jsonl"),
                        help="live store file the dashboard tails")
    args = parser.parse_args(argv)

    store_path = Path(args.store)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.unlink(missing_ok=True)  # the demo store starts empty, every run

    server = make_server(store_path, args.host, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"dashboard (read-only) -> http://{args.host}:{args.port}/  "
          f"store={store_path}", flush=True)

    venv_py = REPO / ".venv" / "bin" / "python"
    drill = subprocess.Popen(
        [str(venv_py), str(REPO / "drill" / "run_drill.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    out_dir: Path | None = None
    tails: list = []

    def reader() -> None:
        nonlocal out_dir
        for line in drill.stdout:  # type: ignore[union-attr]
            print(f"drill: {line}", end="", flush=True)
            if line.startswith("trace ID: ") and out_dir is None:
                trace_id = line.split(":", 1)[1].strip()
                out_dir = REPO / "drill" / "output" / trace_id
                tails.extend([
                    JsonOnce(out_dir / "canary-verdict.json",
                             ingest.ingest_canary_verdict, store_path),
                    JsonlTail(out_dir / "gate-state" / "audit.jsonl",
                              ingest.ingest_gate_audit, store_path),
                    JsonlTail(out_dir / "br-runs" / trace_id / "audit.jsonl",
                              ingest.ingest_blastradius_audit, store_path),
                    JsonlTail(out_dir / "factory-floor-trace.jsonl",
                              ingest.ingest_factory_floor_trace, store_path),
                    JsonOnce(out_dir / "ledger-otlp.json",
                             ingest.ingest_ledger_otlp, store_path),
                    JsonOnce(out_dir / "chaos" / "report.json",
                             ingest.ingest_chaos_report, store_path),
                ])

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    drain_deadline = None
    try:
        while True:
            landed = sum(t.poll() for t in tails)
            if landed:
                print(f"live store: +{landed} events", flush=True)
            if drill.poll() is not None:
                if drain_deadline is None:
                    drain_deadline = time.time() + 10  # final sweep window
                elif time.time() > drain_deadline:
                    break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        drill.terminate()
        server.shutdown()
        return 130

    result = store.verify(store_path)
    print(f"drill exit={drill.returncode}; live store: {result.count} events, "
          f"chain ok={result.ok}", flush=True)
    print("dashboard still serving the finished store — Ctrl-C to stop.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0 if drill.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
