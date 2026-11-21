import pytest

from agenttrace.schema import (
    SCHEMA_VERSION,
    SchemaError,
    UnknownSchemaVersionError,
    new_event,
    validate_event,
)


def _event(**overrides):
    event = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": "trace-test",
        "ts": "2026-11-21T00:00:00Z",
        "source": "agent-canary",
        "agent": "agent-canary",
        "type": "compare_verdict",
        "kind": "event",
        "payload": {},
    }
    event.update(overrides)
    return event


def test_valid_event_passes():
    assert validate_event(_event())["trace_id"] == "trace-test"


def test_new_event_is_valid():
    event = new_event(trace_id="t", ts="2026-11-21T00:00:00Z", source="s",
                      agent="a", type="x", payload={})
    assert event["schema_version"] == SCHEMA_VERSION
    assert event["kind"] == "event"


def test_missing_field_fails():
    event = _event()
    del event["trace_id"]
    with pytest.raises(SchemaError):
        validate_event(event)


def test_wrong_type_fails():
    with pytest.raises(SchemaError):
        validate_event(_event(payload="not-a-dict"))


def test_unknown_kind_fails():
    with pytest.raises(SchemaError):
        validate_event(_event(kind="narrative"))


def test_unknown_schema_version_fails_loudly():
    with pytest.raises(UnknownSchemaVersionError):
        validate_event(_event(schema_version="99"))


def test_missing_schema_version_fails_loudly():
    event = _event()
    del event["schema_version"]
    with pytest.raises(UnknownSchemaVersionError):
        validate_event(event)


def test_stored_event_requires_chain_fields():
    with pytest.raises(SchemaError):
        validate_event(_event(), stored=True)
    stored = _event(seq=1, prev_hash="0" * 64, hash="a" * 64)
    assert validate_event(stored, stored=True)["seq"] == 1


def test_bool_is_not_a_valid_seq():
    stored = _event(seq=True, prev_hash="0" * 64, hash="a" * 64)
    with pytest.raises(SchemaError):
        validate_event(stored, stored=True)
