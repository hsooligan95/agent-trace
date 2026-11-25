"""agenttrace-run: run ANY command as a traced AgentTrace run.

The zero-code integration. Usage:

    python -m agenttrace.run --store <store.jsonl> --source music-maker \
        [--trace-id <id>] [--capture-output] [--ingest-jsonl <file> ...] \
        -- <command> [args...]

What it does:
  1. mints a trace ID (or takes --trace-id),
  2. appends a ``run_started`` event to the store,
  3. runs the command with AGENTTRACE_TRACE_ID / AGENTTRACE_SOURCE /
     AGENTTRACE_DROP set in its environment -- so a project using the
     drop-in emitter (agenttrace/emit.py, or the same JSONL lines
     written by hand from any language) records into this run with no
     further wiring; the drop file is ingested, in order, when the
     command exits,
  4. optionally ingests existing JSONL files (--ingest-jsonl: rollout /
     log files; full AgentTrace events are kept as-is, any other JSON
     object is wrapped as an ``external_event``, plain text lines as
     ``log_line``) and the command's own stdout (--capture-output, one
     ``stdout_line`` event per line, capped),
  5. appends ``run_finished`` with the exit code and duration, verifies
     the chain, prints the trace ID, and exits with the command's own
     exit code.

The wrapper never modifies the wrapped project. It is the entire
integration for projects that cannot or should not carry AgentTrace
code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import ingest as ingest_mod
from . import store as store_mod
from .schema import new_event

MAX_STDOUT_EVENTS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wrap_jsonl(path: Path, trace_id: str, source: str) -> list[dict]:
    """Wrap an arbitrary JSONL/text file as events under this trace."""
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and ingest_mod._CORE_KEYS <= set(obj):
            events.append(obj)
        elif isinstance(obj, dict):
            events.append(new_event(
                trace_id=trace_id, ts=_now(), source=source, agent=source,
                type="external_event", payload={"data": obj}))
        else:
            events.append(new_event(
                trace_id=trace_id, ts=_now(), source=source, agent=source,
                type="log_line", payload={"line": line}))
    return ingest_mod._finish(events)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: agenttrace-run --store S --source NAME [--trace-id ID] "
              "[--capture-output] [--ingest-jsonl F ...] -- <command> [args...]",
              file=sys.stderr)
        return 2
    sep = argv.index("--")
    command = argv[sep + 1:]
    if not command:
        print("agenttrace-run: no command after --", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(prog="agenttrace-run")
    parser.add_argument("--store", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--trace-id")
    parser.add_argument("--capture-output", action="store_true",
                        help="record the command's stdout lines as events")
    parser.add_argument("--ingest-jsonl", action="append", default=[],
                        metavar="FILE",
                        help="existing JSONL/log file to ingest after the run "
                             "(repeatable)")
    args = parser.parse_args(argv[:sep])

    store_path = Path(args.store)
    trace_id = args.trace_id or f"trace-{args.source}-{uuid.uuid4().hex[:8]}"
    drop_path = store_path.parent / f".{trace_id}.drop.jsonl"
    if drop_path.exists():
        drop_path.unlink()

    def record(type_: str, payload: dict) -> None:
        store_mod.append_events(store_path, [new_event(
            trace_id=trace_id, ts=_now(), source=args.source,
            agent="agenttrace-run", type=type_, payload=payload)])

    record("run_started", {"command": command, "cwd": os.getcwd()})

    env = dict(os.environ)
    env["AGENTTRACE_TRACE_ID"] = trace_id
    env["AGENTTRACE_SOURCE"] = args.source
    env["AGENTTRACE_DROP"] = str(drop_path)
    env.pop("AGENTTRACE_STORE", None)  # drop mode keeps event order

    started = time.monotonic()
    proc = subprocess.run(command, env=env,
                          stdout=subprocess.PIPE if args.capture_output else None,
                          text=True)
    duration = round(time.monotonic() - started, 3)

    appended = 0
    if drop_path.exists() and drop_path.read_text(encoding="utf-8").strip():
        events = ingest_mod.ingest_drop(drop_path, trace_id=trace_id,
                                         source=args.source)
        # Drop events belong to this run even if the emitter minted
        # its own trace ID before the wrapper's env reached it.
        for event in events:
            event["trace_id"] = trace_id
        appended += store_mod.append_events(store_path, events)
        drop_path.unlink()
    for extra in args.ingest_jsonl:
        events = _wrap_jsonl(Path(extra), trace_id, args.source)
        appended += store_mod.append_events(store_path, events)
    if args.capture_output and proc.stdout:
        lines = proc.stdout.splitlines()[:MAX_STDOUT_EVENTS]
        events = ingest_mod._finish([new_event(
            trace_id=trace_id, ts=_now(), source=args.source, agent=args.source,
            type="stdout_line", payload={"line": line}) for line in lines])
        appended += store_mod.append_events(store_path, events)
        sys.stdout.write(proc.stdout)

    record("run_finished", {"command": command,
                            "exit_code": proc.returncode,
                            "duration_s": duration,
                            "ingested_events": appended})
    result = store_mod.verify(store_path)
    print(f"agenttrace-run: trace_id={trace_id} source={args.source} "
          f"exit_code={proc.returncode} duration_s={duration} "
          f"store_events={result.count} chain_ok={result.ok}")
    if not result.ok:
        print(f"agenttrace-run: CHAIN BROKEN at seq {result.broken_seq}",
              file=sys.stderr)
        return 1
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
