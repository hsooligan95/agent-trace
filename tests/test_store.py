import json

import pytest

from agenttrace import store
from agenttrace.schema import UnknownSchemaVersionError, new_event


def _events(n=3, trace_id="trace-store"):
    return [new_event(trace_id=trace_id, ts=f"2026-11-21T00:00:0{i}Z",
                      source="agent-canary", agent="agent-canary",
                      type=f"e{i}", payload={"i": i}) for i in range(n)]


def test_append_and_verify_roundtrip(tmp_path):
    path = tmp_path / "store.jsonl"
    assert store.append_events(path, _events()) == 3
    result = store.verify(path)
    assert result.ok and result.count == 3
    loaded = store.load_events(path)
    assert [e["seq"] for e in loaded] == [1, 2, 3]
    assert loaded[0]["prev_hash"] == store.GENESIS


def test_append_extends_existing_chain(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events(2))
    store.append_events(path, _events(2, trace_id="trace-other"))
    result = store.verify(path)
    assert result.ok and result.count == 4
    assert store.load_events(path)[2]["seq"] == 3


def test_tamper_fails_at_exact_event(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events(4))
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["payload"]["i"] = 999  # alter history, keep the stored hash
    lines[1] = json.dumps(entry)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = store.verify(tampered)
    assert not result.ok
    assert result.broken_seq == 2


def test_deleted_event_breaks_chain(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events(3))
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    shrunk = tmp_path / "shrunk.jsonl"
    shrunk.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not store.verify(shrunk).ok


def test_unknown_version_in_store_fails_loudly(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events(1))
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["schema_version"] = "99"
    lines[0] = json.dumps(entry)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(UnknownSchemaVersionError):
        store.load_events(bad)
    with pytest.raises(UnknownSchemaVersionError):
        store.verify(bad)


def test_append_refuses_broken_store(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events(2))
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["payload"]["i"] = 42
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.append_events(path, _events(1))


def test_append_rejects_unknown_version_event(tmp_path):
    path = tmp_path / "store.jsonl"
    event = _events(1)[0]
    event["schema_version"] = "2"
    with pytest.raises(UnknownSchemaVersionError):
        store.append_events(path, [event])
