"""Drop-in AgentTrace emitter -- the plug-and-play entry point.

Copy this ONE file into any project (or import it from an installed
agenttrace package) and call ``emit(...)`` at the points worth
recording. No other integration is required.

Configuration is by environment variable only -- the wrapper
(``agenttrace.run``) sets these for you, so a wrapped project needs
zero code changes at all:

  AGENTTRACE_TRACE_ID   trace this run belongs to (one per run/job)
  AGENTTRACE_SOURCE     producer name, e.g. "music-maker"
  AGENTTRACE_AGENT      actor name (defaults to the source)
  AGENTTRACE_INSTANCE   optional process-instance name
  AGENTTRACE_STORE      path of the hash-chained store JSONL --
                        events are appended (chained + redacted)
                        immediately, when the full package is importable
  AGENTTRACE_DROP       path of a plain JSONL drop file -- events are
                        appended as raw pre-store lines and chained
                        later by ``agenttrace ingest --drop <file>``

With neither STORE nor DROP set, emit() is a no-op returning False:
instrumented code runs identically inside and outside a traced run.
Standalone copies (no agenttrace package importable) always use the
drop file -- AGENTTRACE_DROP, defaulting to ./agenttrace-drop.jsonl
when AGENTTRACE_STORE is set -- because chaining is the store's job,
not the producer's.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(type: str, payload: dict | None = None, *,
               source: str | None = None, agent: str | None = None,
               kind: str = "event", trace_id: str | None = None,
               instance: str | None = None) -> dict:
    """Build one schema-v1 pre-store event from the environment."""
    source = source or os.environ.get("AGENTTRACE_SOURCE", "unknown")
    event = {
        "schema_version": "1",
        "trace_id": trace_id or os.environ.get("AGENTTRACE_TRACE_ID")
        or f"trace-{uuid.uuid4().hex[:8]}",
        "ts": _now(),
        "source": source,
        "agent": agent or os.environ.get("AGENTTRACE_AGENT") or source,
        "type": type,
        "kind": kind,
        "payload": payload or {},
    }
    instance = instance or os.environ.get("AGENTTRACE_INSTANCE")
    if instance:
        event["instance"] = instance
    return event


def emit(type: str, payload: dict | None = None, **kwargs) -> bool:
    """Record one event. Returns True if it was written somewhere."""
    event = make_event(type, payload, **kwargs)
    store_path = os.environ.get("AGENTTRACE_STORE")
    if store_path:
        try:
            from .store import append_events
        except ImportError:
            try:
                from agenttrace.store import append_events
            except ImportError:
                append_events = None
        if append_events is not None:
            append_events(store_path, [event])
            return True
        # Standalone copy: fall through to the drop file; the store
        # chains the event at ingest time.
    drop_path = os.environ.get("AGENTTRACE_DROP")
    if not drop_path and store_path:
        drop_path = str(Path(store_path).with_suffix(".drop.jsonl"))
    if drop_path:
        path = Path(drop_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        return True
    return False
