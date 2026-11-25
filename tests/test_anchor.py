"""F1 (Red Team Audit Run #1): signed chain heads + external anchors.

The pre-fix store proved internal consistency only -- a forged insert
or a full-history rewrite with recomputed hashes verified "Chain OK".
These tests pin the post-fix behavior: anchored verification FAILS on
both attacks, still passes on an untampered anchored history, and the
anchor log itself is tamper-evident.
"""
import json

import pytest

from agenttrace import anchor, store
from agenttrace.schema import new_event

KEY = "test-signing-key-not-a-real-secret"
OTHER_KEY = "another-key-that-is-long-enough"


def _events(n=4, trace_id="trace-anchor"):
    return [new_event(trace_id=trace_id, ts=f"2026-11-21T00:00:0{i}Z",
                      source="agent-canary", agent="agent-canary",
                      type=f"e{i}", payload={"i": i}) for i in range(n)]


def _rechain(entries):
    """Recompute seq/prev_hash/hash -- the attacker's free move."""
    prev = store.GENESIS
    out = []
    for i, e in enumerate(entries, start=1):
        e = {k: v for k, v in e.items() if k not in ("seq", "prev_hash", "hash")}
        e["seq"] = i
        e["prev_hash"] = prev
        e["hash"] = store.event_hash(e)
        prev = e["hash"]
        out.append(e)
    return out


def _write(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries),
                    encoding="utf-8")


def _anchored_store(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events())
    anchors = tmp_path / "anchors.jsonl"
    entry = anchor.anchor_store(path, anchors, KEY, environment="test")
    return path, anchors, entry


def test_anchor_roundtrip_verifies(tmp_path):
    path, anchors, entry = _anchored_store(tmp_path)
    assert entry["event_count"] == 4
    assert entry["key_id"] == anchor.key_id(KEY)
    result = anchor.verify_against_anchors(path, anchors, KEY)
    assert result.ok
    log = anchor.verify_anchor_log(anchors, KEY)
    assert log.ok and log.anchors_checked == 1


def test_forged_insert_fails_anchored_verification(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    entries = store.load_events(path)
    forged = dict(entries[1])
    forged["payload"] = {"i": 999, "forged": True}
    attacked = _rechain(entries[:2] + [forged] + entries[2:])
    evil = tmp_path / "evil.jsonl"
    _write(evil, attacked)
    assert store.verify(evil).ok  # plain chain check is still fooled
    result = anchor.verify_against_anchors(evil, anchors, KEY)
    assert not result.ok


def test_full_rewrite_fails_anchored_verification(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    entries = store.load_events(path)
    entries[0]["payload"]["i"] = 424242  # rewrite history, rechain all
    evil = tmp_path / "evil.jsonl"
    _write(evil, _rechain(entries))
    assert store.verify(evil).ok
    result = anchor.verify_against_anchors(evil, anchors, KEY)
    assert not result.ok
    assert "no anchor matches" in result.detail


def test_unanchored_store_fails_anchored_verification(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events())
    anchors = tmp_path / "anchors.jsonl"  # never anchored
    result = anchor.verify_against_anchors(path, anchors, KEY)
    assert not result.ok


def test_wrong_key_fails(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    assert not anchor.verify_anchor_log(anchors, OTHER_KEY).ok
    assert not anchor.verify_against_anchors(path, anchors, OTHER_KEY).ok


def test_anchor_log_tampering_fails(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    anchor.anchor_store(path, anchors, KEY, environment="test")  # 2nd entry
    lines = anchors.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["head"] = "f" * 64  # rewrite the recorded head after the fact
    lines[0] = json.dumps(entry)
    anchors.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = anchor.verify_anchor_log(anchors, KEY)
    assert not result.ok
    assert result.last_good_anchor is None  # first anchor is the bad one
    assert not anchor.verify_against_anchors(path, anchors, KEY).ok


def test_anchor_refuses_to_extend_tampered_log(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    lines = anchors.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["event_count"] = 999
    lines[0] = json.dumps(entry)
    anchors.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to anchor"):
        anchor.anchor_store(path, anchors, KEY)


def test_extension_beyond_anchor_is_not_verified(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    store.append_events(path, _events(2))  # legitimate growth, unanchored
    result = anchor.verify_against_anchors(path, anchors, KEY)
    assert not result.ok
    assert "extends the anchored head" in result.detail


def test_broken_chain_fails_anchored_verification(tmp_path):
    path, anchors, _ = _anchored_store(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["payload"]["i"] = 999  # no rechain: plain tamper
    lines[1] = json.dumps(entry)
    broken = tmp_path / "broken.jsonl"
    broken.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = anchor.verify_against_anchors(broken, anchors, KEY)
    assert not result.ok
    assert "chain broken" in result.detail


def test_short_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(anchor.ENV_VAR, "short")
    with pytest.raises(ValueError, match="too short"):
        anchor.key_from_env()
    monkeypatch.delenv(anchor.ENV_VAR)
    with pytest.raises(ValueError, match="not set"):
        anchor.key_from_env()


def test_failure_streak_counts_and_resets(tmp_path):
    anchors = tmp_path / "anchors.jsonl"
    assert anchor.append_failure_record(anchors, "boom", None) == 1
    assert anchor.append_failure_record(anchors, "boom again", None) == 2
    path = tmp_path / "store.jsonl"
    store.append_events(path, _events())
    anchor.anchor_store(path, anchors, KEY)
    assert anchor.count_failure_streak(anchors) == 0
