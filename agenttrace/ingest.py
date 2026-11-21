"""Ingestors: producer artifacts -> AgentTrace events.

One ingestor per producer, each reading only that producer's existing
machine-readable output. Producers emit their own trace identity
(agent-canary's verdict carries run_id + generated_at; agent-gate's
audit entries carry traceId; blast-radius's audit records carry
run_id) -- ingestors preserve the producer-emitted identity and never
re-key or infer correlation that the producer did not emit.

Every produced event is validated against the versioned schema before
it is returned; a producer artifact that does not fit fails loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import decisions
from .schema import new_event, validate_event


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in
            Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def _finish(events: list[dict]) -> list[dict]:
    for event in events:
        validate_event(event)
    return events


def ingest_canary_verdict(path: str | Path) -> list[dict]:
    """agent-canary compare verdict JSON -> verdict event + decision record."""
    verdict = _read_json(path)
    trace_id = verdict["run_id"]
    events = [new_event(
        trace_id=trace_id,
        ts=verdict["generated_at"],
        source="agent-canary",
        agent="agent-canary",
        type="compare_verdict",
        payload=verdict,
    )]
    events += decisions.canary_decision_events(verdict, trace_id=trace_id)
    return _finish(events)


def ingest_gate_audit(path: str | Path) -> list[dict]:
    """agent-gate audit JSONL -> one event per entry + decision records."""
    events: list[dict] = []
    for entry in _read_jsonl(path):
        trace_id = entry["traceId"]
        events.append(new_event(
            trace_id=trace_id,
            ts=entry["ts"],
            source="agent-gate",
            agent=entry.get("agent", "agent-gate"),
            type=entry["type"],
            payload=entry,
        ))
        events += decisions.gate_decision_events(entry, trace_id=trace_id)
    return _finish(events)


def ingest_blastradius_audit(path: str | Path) -> list[dict]:
    """blast-radius audit JSONL -> one event per audit record."""
    events = []
    for record in _read_jsonl(path):
        events.append(new_event(
            trace_id=record["run_id"],
            ts=record["ts"],
            source="blast-radius",
            agent=record.get("actor", "blastradius-agent"),
            type=record["event"],
            payload=record,
        ))
    return _finish(events)


def _otlp_attrs(span: dict) -> dict:
    attrs = {}
    for item in span.get("attributes", []):
        value = item.get("value", {})
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                attrs[item["key"]] = value[key]
                break
    return attrs


def _nano_to_iso(nano: str) -> str:
    seconds, nanos = divmod(int(nano), 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.replace(microsecond=nanos // 1000).isoformat().replace("+00:00", "Z")


def ingest_ledger_otlp(path: str | Path) -> list[dict]:
    """agent-ledger OTLP JSON export -> one event per span.

    Ledger spans carry their own OTLP trace IDs, which are preserved as
    the events' trace_id. (Joining a ledger run to another producer's
    trace is phase 2: the spans' identity is OTLP-native, and this
    ingestor does not invent a mapping.)
    """
    doc = _read_json(path)
    events = []
    for resource_span in doc.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                attrs = _otlp_attrs(span)
                events.append(new_event(
                    trace_id=span["traceId"],
                    ts=_nano_to_iso(span["startTimeUnixNano"]),
                    source="agent-ledger",
                    agent=attrs.get("agentledger.agent", "agent-ledger"),
                    type="span",
                    payload={
                        "name": span.get("name"),
                        "span_id": span.get("spanId"),
                        "parent_span_id": span.get("parentSpanId"),
                        "attributes": attrs,
                    },
                ))
    return _finish(events)
