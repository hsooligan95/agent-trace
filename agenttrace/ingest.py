"""Ingestors: producer artifacts -> AgentTrace events.

One ingestor per producer, each reading only that producer's existing
machine-readable output. Producers emit their own trace identity
(agent-canary's verdict carries run_id + generated_at; agent-gate's
audit entries carry traceId; blast-radius's audit records carry
run_id; factory-floor's run record carries the trace ID it was
supplied; agent-chaos's report carries run_id + generated_at;
agent-ledger's root span may declare an external trace ID) --
ingestors preserve the producer-emitted identity and never
re-key or infer correlation that the producer did not emit.

Every produced event is validated against the versioned schema before
it is returned; a producer artifact that does not fit fails loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import decisions
from .redact import redact_events
from .schema import new_event, validate_event


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in
            Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def _finish(events: list[dict]) -> list[dict]:
    # Redaction at capture: payloads are scrubbed here, on the events
    # the ingestors produce -- raw producer artifacts are never touched,
    # and store.append_events re-applies the (idempotent) pass so no
    # path into the store bypasses it.
    events = redact_events(events)
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
        instance=verdict.get("instance"),
    )]
    events += decisions.canary_decision_events(
        verdict, trace_id=trace_id, instance=verdict.get("instance"))
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
            instance=entry.get("instance"),
        ))
        events += decisions.gate_decision_events(
            entry, trace_id=trace_id, instance=entry.get("instance"))
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
            instance=record.get("instance"),
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

    Ledger spans carry their own OTLP trace IDs. If the run was stamped
    with an external trace ID (the sample agent's --trace-id /
    AGENTTRACE_TRACE_ID plumbing puts ``agenttrace.trace_id`` on the
    run's root span), every span of that OTLP trace joins the external
    trace: the mapping is declared by the producer on its own root
    span, never inferred by this ingestor, and the native OTLP trace ID
    is preserved in each event's payload as ``otlp_trace_id``.
    Unstamped exports keep their OTLP trace IDs, exactly as before.
    """
    doc = _read_json(path)
    spans = []
    for resource_span in doc.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                spans.append((span, _otlp_attrs(span)))
    join: dict[str, str] = {}
    instance_join: dict[str, str] = {}
    for span, attrs in spans:
        external = attrs.get("agenttrace.trace_id")
        if external:
            join[span["traceId"]] = external
        # Same declared-by-the-producer pattern for instance identity:
        # the run's root span may declare ``agenttrace.instance``, and
        # every span of that OTLP trace then belongs to that instance.
        declared_instance = attrs.get("agenttrace.instance")
        if declared_instance:
            instance_join[span["traceId"]] = declared_instance
    events = []
    for span, attrs in spans:
        otlp_trace_id = span["traceId"]
        payload = {
            "name": span.get("name"),
            "span_id": span.get("spanId"),
            "parent_span_id": span.get("parentSpanId"),
            "otlp_trace_id": otlp_trace_id,
            "attributes": attrs,
        }
        if otlp_trace_id in join:
            payload["agenttrace_trace_id"] = join[otlp_trace_id]
        events.append(new_event(
            trace_id=join.get(otlp_trace_id, otlp_trace_id),
            ts=_nano_to_iso(span["startTimeUnixNano"]),
            source="agent-ledger",
            agent=attrs.get("agentledger.agent", "agent-ledger"),
            type="span",
            payload=payload,
            instance=(attrs.get("agenttrace.instance")
                      or instance_join.get(otlp_trace_id)),
        ))
    return _finish(events)


def ingest_factory_floor_trace(path: str | Path) -> list[dict]:
    """factory-floor-agent JSONL run record -> one event per record line.

    The agent emits only when a trace ID was supplied to it
    (FACTORY_FLOOR_TRACE_ID / AGENTTRACE_TRACE_ID); the record carries
    that propagated ID, which this ingestor preserves verbatim. The
    groundedness line is the agent's computed decision point, so it
    additionally yields a decision record reshaped from the checker's
    own emitted scores (see decisions.factory_floor_decision_events).
    """
    events: list[dict] = []
    for record in _read_jsonl(path):
        trace_id = record["trace_id"]
        events.append(new_event(
            trace_id=trace_id,
            ts=record["ts"],
            source="factory-floor-agent",
            agent=record.get("agent", "factory-floor-agent"),
            type=record["type"],
            payload=record["payload"],
            instance=record.get("instance"),
        ))
        if record["type"] == "groundedness":
            events += decisions.factory_floor_decision_events(
                record["payload"], trace_id=trace_id, ts=record["ts"],
                instance=record.get("instance"))
    return _finish(events)


def ingest_chaos_report(path: str | Path) -> list[dict]:
    """agent-chaos report JSON -> headline event + one event per row.

    Chaos reports carry their own run_id + generated_at (emit-side
    additions in agent-chaos, same pattern as canary's verdict); both
    are required — a report without them cannot be keyed to a trace
    honestly, so ingestion fails loudly on the missing key. Rows carry
    no per-row timestamps, so every event is stamped with the report's
    generated_at rather than an invented time.
    """
    report = _read_json(path)
    trace_id = report["run_id"]
    ts = report["generated_at"]
    events = [new_event(
        trace_id=trace_id,
        ts=ts,
        source="agent-chaos",
        agent="agent-chaos",
        type="chaos_report",
        payload={"headline": report["headline"], "rows": len(report["rows"])},
        instance=report.get("instance"),
    )]
    for row in report["rows"]:
        events.append(new_event(
            trace_id=trace_id,
            ts=ts,
            source="agent-chaos",
            agent="agent-chaos",
            type="scenario_result",
            payload=row,
            instance=report.get("instance"),
        ))
    return _finish(events)


# ---------------------------------------------------------------------------
# Generic drop-file ingestor -- the plug-and-play path for ANY project.
#
# A drop file is plain JSONL, one object per line, written by
# agenttrace.emit, by a few lines of any language (Node/TS included --
# no AgentTrace code needs to run in the host project), or by hand.
# Each line is either:
#   * a full pre-store event (schema_version/trace_id/ts/source/agent/
#     type/kind/payload), used exactly as written, or
#   * a loose record {"type": ..., "payload": {...}, ...} -- missing
#     core fields are filled from the line itself, then from the
#     --trace-id / --source arguments, then from documented defaults
#     (ts = ingest time, agent = source, kind = "event"). Remaining
#     unrecognized keys are preserved inside the payload, never dropped.
# ---------------------------------------------------------------------------

_CORE_KEYS = {"schema_version", "trace_id", "ts", "source", "agent",
              "type", "kind", "payload", "instance"}


def ingest_drop(path: str | Path, trace_id: str | None = None,
                source: str | None = None) -> list[dict]:
    """Ingest a generic AgentTrace drop JSONL file (see above)."""
    events = []
    for obj in _read_jsonl(path):
        if not isinstance(obj, dict):
            raise ValueError(f"drop line is not a JSON object: {obj!r}")
        if _CORE_KEYS <= set(obj):
            events.append(obj)
            continue
        payload = dict(obj.get("payload") or {})
        for key, value in obj.items():
            if key not in _CORE_KEYS and key not in payload:
                payload[key] = value
        events.append(new_event(
            trace_id=obj.get("trace_id") or trace_id or "trace-drop",
            ts=obj.get("ts") or datetime.now(timezone.utc).isoformat(),
            source=obj.get("source") or source or "unknown",
            agent=obj.get("agent") or obj.get("source") or source or "unknown",
            type=obj.get("type") or "event",
            payload=payload,
            kind=obj.get("kind") or "event",
            instance=obj.get("instance"),
        ))
    return _finish(events)
