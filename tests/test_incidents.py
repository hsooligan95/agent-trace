"""Incident prototype tests: derivation from the store alone, the
incident detail view-model (step-through window, payload diffs,
per-event hash consistency, state before/after), and the read-only
HTTP surface for /api/incidents + /api/incident.

The incident view is an entry path, not a platform: it derives
everything from chained events through the query layer, exactly
like the rest of the dashboard, and adds no write surface.
"""

import http.client
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, store  # noqa: E402
from dashboard.server import (  # noqa: E402
    build_incident_detail, build_incidents, build_state, make_server,
    payload_diff,
)

SAMPLES = REPO / "samples"
DEMO_STORE = REPO / "dashboard" / "live-store.jsonl"
DRILL_TRACE = "trace-drill-11028023"


def _samples_store(tmp_path) -> Path:
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    store.append_events(path, ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp-joined.json"))
    return path


def _serve(store_path):
    server = make_server(store_path, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# ----------------------------------------------------------------
# Derivation
# ----------------------------------------------------------------

def test_incidents_on_drill_store():
    """The phase-2 demo store yields exactly the two incidents the
    drill is known for: the canary BLOCK and the blast-radius
    rollback, in store order."""
    result = build_incidents(DEMO_STORE)
    assert result["trace_id"] == DRILL_TRACE
    incidents = result["incidents"]
    assert [i["kind"] for i in incidents] == ["block", "rollback"]

    block = incidents[0]
    assert block["source"] == "agent-canary"
    assert block["seq"] == 2
    assert block["window"] == {"from_seq": 1, "to_seq": 2}
    assert "v2-bad" in block["title"]
    assert "forbidden" in block["summary"]

    rollback = incidents[1]
    assert rollback["source"] == "blast-radius"
    assert rollback["seq"] == 55
    assert rollback["window"] == {"from_seq": 10, "to_seq": 55}
    # The one-line summary must carry the causal chain, not a label.
    assert "kill -9" in rollback["summary"]
    assert "verification failed" in rollback["summary"]
    # Cost: the ledger spans land after both windows, so neither
    # incident may claim a cost.
    assert block["cost_usd"] is None and rollback["cost_usd"] is None


def test_rollback_why_chain_names_each_cause():
    rollback = build_incidents(DEMO_STORE)["incidents"][1]
    text = "\n".join(w["text"] for w in rollback["why"])
    assert "Plan created" in text
    assert "Approved by" in text
    assert "kill -9" in text and "#18" in text and "#20" in text
    assert "health_not_cleared" in text and "bad-disk" in text
    assert "Verification check failed" in text
    assert "Post-run verification failed" in text and "8/9" in text
    assert "Rolled back 6 step(s)" in text
    assert "Run finished: rolled_back" in text
    # Why lines are in causal (seq) order.
    seqs = [w["seq"] for w in rollback["why"]]
    assert seqs == sorted(seqs)


def test_incidents_on_samples_store_block_only(tmp_path):
    """The spike samples: canary BLOCKed, but the blast-radius run
    finished cleanly -- one incident, no invented rollback."""
    result = build_incidents(_samples_store(tmp_path))
    assert [i["kind"] for i in result["incidents"]] == ["block"]
    assert result["incidents"][0]["source"] == "agent-canary"


def test_state_carries_incidents(tmp_path):
    state = build_state(_samples_store(tmp_path))
    assert [i["kind"] for i in state["incidents"]] == ["block"]


# ----------------------------------------------------------------
# Incident detail view-model
# ----------------------------------------------------------------

def test_incident_detail_rollback_window():
    detail = build_incident_detail(DEMO_STORE, 55)
    assert detail is not None
    assert detail["trace_id"] == DRILL_TRACE
    assert detail["anchor"]["type"] == "run_finished"
    assert detail["anchor"]["payload"]["result"] == "rolled_back"
    steps = detail["steps"]
    assert [s["seq"] for s in steps] == list(range(10, 56))
    assert steps[-1]["is_anchor"] is True
    assert sum(s["is_anchor"] for s in steps) == 1
    # Untouched store: every step's hash consistency recomputes.
    assert detail["first_unverified_seq"] is None
    assert all(s["verified"] for s in steps)
    # First step has no diff; later steps diff against their
    # predecessor and the diff machinery sees real changes.
    assert steps[0]["diff"] is None
    assert all(isinstance(s["diff"], list) for s in steps[1:])
    # State reconstruction brackets the anchor.
    assert detail["state_before"]["events"] == 54
    assert detail["state_after"]["events"] == 55
    assert detail["state_after"]["last_seq"] == 55


def test_incident_detail_block_window():
    detail = build_incident_detail(DEMO_STORE, 2)
    assert [s["seq"] for s in detail["steps"]] == [1, 2]
    assert detail["anchor"]["kind"] == "decision_record"
    assert detail["anchor"]["payload"]["choice"] == "BLOCK"
    assert detail["anchor"]["payload"]["computed"] is True


def test_incident_detail_unknown_anchor():
    assert build_incident_detail(DEMO_STORE, 999) is None
    assert build_incident_detail(DEMO_STORE, 30) is None  # not an anchor


def test_incident_detail_tamper_flags_exact_step(tmp_path):
    """Flip one character inside the seq-20 line of a store copy:
    steps before it stay verified, seq 20 and everything after it
    (the chain no longer recomputes) are flagged, and the detail
    names seq 20 as the first unverified step."""
    lines = DEMO_STORE.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[19])["seq"] == 20
    lines[19] = lines[19].replace("health_check", "health_cheXk", 1)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    detail = build_incident_detail(tampered, 55)
    assert detail["first_unverified_seq"] == 20
    by_seq = {s["seq"]: s["verified"] for s in detail["steps"]}
    assert by_seq[19] is True
    assert by_seq[20] is False
    assert by_seq[21] is False and by_seq[55] is False


def test_payload_diff_shapes():
    assert payload_diff(None, {"a": 1}) is None
    assert payload_diff({"a": 1}, {"a": 1}) == []
    diff = payload_diff({"a": 1, "b": 2, "c": 3},
                        {"a": 1, "b": 20, "d": 4})
    by_key = {d["key"]: d for d in diff}
    assert by_key["b"]["change"] == "changed"
    assert by_key["b"]["before"] == "2" and by_key["b"]["after"] == "20"
    assert by_key["c"]["change"] == "removed"
    assert by_key["d"]["change"] == "added"
    assert "a" not in by_key


# ----------------------------------------------------------------
# HTTP surface
# ----------------------------------------------------------------

def test_incident_endpoints_read_only(tmp_path):
    import shutil
    store_copy = tmp_path / "store.jsonl"
    shutil.copy(DEMO_STORE, store_copy)
    server, port = _serve(store_copy)
    try:
        status, body = _request(port, "GET", "/api/incidents")
        payload = json.loads(body)
        assert status == 200
        assert [i["kind"] for i in payload["incidents"]] == ["block", "rollback"]

        status, body = _request(port, "GET", "/api/incident?seq=55")
        detail = json.loads(body)
        assert status == 200
        assert detail["incident"]["kind"] == "rollback"
        assert len(detail["steps"]) == 46

        status, _ = _request(port, "GET", "/api/incident?seq=999")
        assert status == 404
        status, _ = _request(port, "GET", "/api/incident")
        assert status == 404
        # The incident surface adds no write path.
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, body = _request(port, method, "/api/incident?seq=55", body="{}")
            assert status == 405, f"{method} must be rejected, got {status}"
            assert b"read-only" in body
    finally:
        server.shutdown()


def test_page_carries_incident_views(tmp_path):
    server, port = _serve(_samples_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/")
        assert status == 200
        for marker in (b"INCIDENTS", b'id="incidents-panel"',
                       b'id="incident-view"', b'id="inc-steps"',
                       b"JUMP TO DECISION", b"STEP-THROUGH"):
            assert marker in body, f"page missing {marker!r}"
    finally:
        server.shutdown()
