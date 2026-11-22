"""Versioned event schema for the AgentTrace store.

Every event in the store carries ``schema_version``. The schema is a
cross-repo API: six producers emit into one store, so a renamed field or
a new event shape must never be absorbed silently. Ingestion and
verification FAIL LOUDLY on an unknown version (Critic fence, objection
#5): an audit system that silently drops or misreads events manufactures
false confidence, which is worse than no audit system.
"""

from __future__ import annotations

SCHEMA_VERSION = "1"
SUPPORTED_VERSIONS = frozenset({SCHEMA_VERSION})

# Event kinds. `decision_record` is reserved for records emitted by the
# code branch that made a *computed* decision, at decision time, from the
# values that decided it (canary verdicts, gate policy/screening calls).
# Anything an LLM narrates about its own reasoning is at most a
# `rationale_annotation` -- a different, weaker artifact class, and the
# schema keeps the two visibly distinct.
KIND_EVENT = "event"
KIND_DECISION_RECORD = "decision_record"
KIND_RATIONALE_ANNOTATION = "rationale_annotation"
KINDS = frozenset({KIND_EVENT, KIND_DECISION_RECORD, KIND_RATIONALE_ANNOTATION})

# Fields every event carries before it enters the store.
CORE_FIELDS = {
    "schema_version": str,
    "trace_id": str,
    "ts": str,
    "source": str,
    "agent": str,
    "type": str,
    "kind": str,
    "payload": dict,
}

# Optional fields an event may carry. ``instance`` names the producing
# process instance of a source (one agent type can run as many
# processes); it is deliberately NOT required and NOT a version bump:
# v1 validation tolerates it, stores written before it existed stay
# valid, and the query layer treats a missing instance as the event's
# ``agent`` value (see query.event_instance).
OPTIONAL_FIELDS = {
    "instance": str,
}

# Additional fields the store itself assigns on append.
STORE_FIELDS = {
    "seq": int,
    "prev_hash": str,
    "hash": str,
}


class SchemaError(ValueError):
    """An event does not conform to the AgentTrace event schema."""


class UnknownSchemaVersionError(SchemaError):
    """An event carries a schema_version this build does not support."""


def check_version(event: dict) -> None:
    version = event.get("schema_version")
    if version not in SUPPORTED_VERSIONS:
        raise UnknownSchemaVersionError(
            f"unsupported schema_version {version!r} "
            f"(supported: {sorted(SUPPORTED_VERSIONS)}); refusing to ingest "
            "or verify an event this build does not understand"
        )


def _check_fields(event: dict, fields: dict, what: str) -> None:
    for name, typ in fields.items():
        if name not in event:
            raise SchemaError(f"{what} is missing required field {name!r}")
        value = event[name]
        # bool is a subclass of int; a bool is never a valid seq.
        if typ is int and isinstance(value, bool):
            raise SchemaError(f"{what} field {name!r} must be int, got bool")
        if not isinstance(value, typ):
            raise SchemaError(
                f"{what} field {name!r} must be {typ.__name__}, "
                f"got {type(value).__name__}"
            )


def validate_event(event: dict, *, stored: bool = False) -> dict:
    """Validate one event dict against the schema. Returns the event.

    Raises UnknownSchemaVersionError for an unsupported schema_version and
    SchemaError for any other nonconformance. With ``stored=True`` the
    store-assigned fields (seq / prev_hash / hash) are also required.
    """
    if not isinstance(event, dict):
        raise SchemaError(f"event must be a dict, got {type(event).__name__}")
    check_version(event)
    _check_fields(event, CORE_FIELDS, "event")
    for name, typ in OPTIONAL_FIELDS.items():
        if name in event and not isinstance(event[name], typ):
            raise SchemaError(
                f"event optional field {name!r} must be {typ.__name__}, "
                f"got {type(event[name]).__name__}"
            )
    if event["kind"] not in KINDS:
        raise SchemaError(
            f"event kind {event['kind']!r} is not one of {sorted(KINDS)}"
        )
    if stored:
        _check_fields(event, STORE_FIELDS, "stored event")
    return event


def new_event(*, trace_id: str, ts: str, source: str, agent: str,
              type: str, payload: dict, kind: str = KIND_EVENT,
              instance: str | None = None) -> dict:
    """Build a schema-conformant event (pre-store; no seq/hash yet).

    ``instance`` is the producing process instance of the source, when
    the producer declares one; the key is omitted entirely when it
    does not, so events built from older producer artifacts are
    byte-identical to what they were before the field existed.
    """
    event = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "ts": ts,
        "source": source,
        "agent": agent,
        "type": type,
        "kind": kind,
        "payload": payload,
    }
    if instance is not None:
        event["instance"] = instance
    return validate_event(event)
