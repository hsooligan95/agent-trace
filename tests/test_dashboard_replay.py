"""Slice-2 tests: the generalized replay (/api/replay) and trace
compare (/api/compare).

Pins what the generalization must not lose from the incident/walk
machinery it reuses: per-step payload diffs, per-step hash
consistency that pauses at the first unverified step, TraceReplay
state at every step -- plus the scoping rules (whole trace, seq
window, unknown trace) and the anomaly vocabulary the jump
navigation rides on. The endpoints stay exactly as read-only as
the rest of the dashboard.
"""

import http.client
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, query as q, store  # noqa: E402
from dashboard.server import (build_compare, build_replay,  # noqa: E402
                              load_store_events, make_server)

SAMPLES = REPO / "samples"
DRILL = REPO / "drill" / "output" / "trace-drill-45d404d0" / "store.jsonl"


def _full_store(tmp_path) -> Path:
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    store.append_events(path, ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp-joined.json"))
    return path


def _two_trace_store(tmp_path) -> Path:
    """Two traces in one store: the samples set under its own trace
    id, then again under a second id (one payload tweaked, so the
    pair is close but not identical)."""
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    again = ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl")
    for e in again:
        e["trace_id"] = "trace-second"
    store.append_events(path, again)
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


def _ev(seq, source, type_, payload=None, kind="event", trace="t-1",
        ts="2026-11-21T19:25:00Z"):
    return {"seq": seq, "ts": ts, "trace_id": trace,
            "agent": source, "source": source, "type": type_,
            "kind": kind, "payload": payload or {}}


# ---------------------------------------------------------------------
# Anomaly vocabulary
# ---------------------------------------------------------------------

def test_anomaly_kind_vocabulary():
    block = _ev(1, "agent-canary", "compare", kind="decision_record",
                payload={"choice": "BLOCK"})
    allow = _ev(2, "agent-gate", "policy", kind="decision_record",
                payload={"choice": "allow"})
    rolled = _ev(3, "blast-radius", "run_finished", {"result": "rolled_back"})
    finished = _ev(4, "blast-radius", "run_finished", {"result": "completed"})
    fail = _ev(5, "blast-radius", "verify_check", {"result": "FAIL"})
    passed = _ev(6, "blast-radius", "verify_check", {"result": "pass"})
    vfail = _ev(7, "blast-radius", "verify_failed", {"detail": "8/9"})
    silent = _ev(8, "agent-chaos", "scenario_result",
                 {"fault_injected": True, "detected": False})
    caught = _ev(9, "agent-chaos", "scenario_result",
                 {"fault_injected": True, "detected": True})
    step = _ev(10, "agent-ledger", "span",
               {"attributes": {"agentledger.kind": "step"}})
    span = _ev(11, "agent-ledger", "span",
               {"attributes": {"agentledger.kind": "task"}})
    plain = _ev(12, "agent-gate", "tool_call", {"tool": "s3.get"})
    assert q.anomaly_kind(block) == "block"
    assert q.anomaly_kind(allow) is None
    assert q.anomaly_kind(rolled) == "rollback"
    assert q.anomaly_kind(finished) is None
    assert q.anomaly_kind(fail) == "failed-check"
    assert q.anomaly_kind(passed) is None
    assert q.anomaly_kind(vfail) == "failed-check"
    assert q.anomaly_kind(silent) == "silent-failure"
    assert q.anomaly_kind(caught) is None
    assert q.anomaly_kind(step) == "cost"
    assert q.anomaly_kind(span) is None
    assert q.anomaly_kind(plain) is None


# ---------------------------------------------------------------------
# build_replay scoping
# ---------------------------------------------------------------------

def test_replay_whole_trace_scope(tmp_path):
    payload = build_replay(_full_store(tmp_path))
    assert payload["trace_id"] == "trace-spike-efadd3b5"
    assert payload["scope"]["trace_events"] == 68
    assert len(payload["steps"]) == 68
    assert payload["scope"]["from_seq"] == 1
    assert payload["scope"]["to_seq"] == 68
    assert sum(payload["sources"].values()) == 68
    # Diffs: the first step has nothing before it; the rest diff
    # against the previous step in scope.
    assert payload["steps"][0]["diff"] is None
    assert all(s["diff"] is not None for s in payload["steps"][1:])
    # State rides every step and grows monotonically to the full
    # trace state.
    counts = [s["state"]["events"] for s in payload["steps"]]
    assert counts == sorted(counts) and counts[-1] == 68
    assert payload["steps"][-1]["state"]["cost_usd"] > 0
    # The canary BLOCK is an anomaly; ledger steps are cost steps.
    assert payload["anomalies"].get("block") == 1
    assert payload["anomalies"].get("cost", 0) > 0
    assert payload["first_unverified_seq"] is None
    assert all(s["verified"] for s in payload["steps"])


def test_replay_window_scope(tmp_path):
    payload = build_replay(_full_store(tmp_path), from_seq=10, to_seq=20)
    assert [s["seq"] for s in payload["steps"]] == list(range(10, 21))
    assert payload["scope"]["trace_events"] == 68  # the trace, not the window
    assert sum(payload["sources"].values()) == 11
    # The window's first step diffs against nothing in scope.
    assert payload["steps"][0]["diff"] is None
    # State is still the state of the whole trace prefix at that seq.
    assert payload["steps"][0]["state"]["events"] == 10
    assert payload["steps"][-1]["state"]["events"] == 20


def test_replay_unknown_trace_and_empty_window(tmp_path):
    path = _full_store(tmp_path)
    assert build_replay(path, "no-such-trace") is None
    empty = build_replay(path, from_seq=500, to_seq=600)
    assert empty is not None and empty["steps"] == []
    assert empty["sources"] == {} and empty["anomalies"] == {}


def test_replay_tamper_flags_exact_seq(tmp_path):
    path = _full_store(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    target = json.loads(lines[19])  # seq 20
    assert target["seq"] == 20
    target["payload"]["tampered"] = "one flipped fact"
    lines[19] = json.dumps(target)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = build_replay(path)
    assert payload["first_unverified_seq"] == 20
    assert all(s["verified"] for s in payload["steps"] if s["seq"] < 20)
    assert all(not s["verified"] for s in payload["steps"] if s["seq"] >= 20)


def test_replay_on_the_real_drill_store():
    payload = build_replay(DRILL)
    assert payload["trace_id"] == "trace-drill-45d404d0"
    assert len(payload["steps"]) == 117
    # The drill's anomalies: the canary block, the rollback (decision
    # vocabulary + the rolled-back run), a failed check, cost steps,
    # and the chaos matrix's undetected faults.
    a = payload["anomalies"]
    assert a.get("block") == 1
    assert a.get("rollback", 0) >= 1
    assert a.get("failed-check", 0) >= 1
    assert a.get("cost", 0) > 0
    assert a.get("silent-failure", 0) > 0
    assert payload["first_unverified_seq"] is None


# ---------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------

def test_compare_two_traces_in_one_store(tmp_path):
    path = _two_trace_store(tmp_path)
    same = build_compare(path, "trace-spike-efadd3b5", "trace-spike-efadd3b5")
    assert same["equal"] is True
    diff = build_compare(path, "trace-spike-efadd3b5", "trace-second")
    assert diff["trace_a"] == "trace-spike-efadd3b5"
    assert diff["trace_b"] == "trace-second"
    # The second trace holds only the gate events, so classes from
    # the canary side of the first trace are named as divergences.
    assert diff["equal"] is False
    assert any(d["kind"] == "class_only_in_a" for d in diff["divergences"])
    assert build_compare(path, "trace-spike-efadd3b5", "nope") is None
    assert build_compare(path, "nope", "trace-second") is None


def test_compare_two_generated_drill_stores(tmp_path):
    """The stretch goal's honest basis: the drill stores on disk
    each hold one trace, so compare is exercised over two real
    drill runs re-chained into one store -- and two honest re-runs
    of the same drill diff clean."""
    def prestore(p):
        return [{k: v for k, v in json.loads(line).items()
                 if k not in ("seq", "prev_hash", "hash")}
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    other = REPO / "drill" / "output" / "trace-drill-11028023" / "store.jsonl"
    path = tmp_path / "combined.jsonl"
    store.append_events(path, prestore(DRILL))
    store.append_events(path, prestore(other))
    assert store.verify(path).ok
    payload = build_replay(path, "trace-drill-11028023")
    assert len(payload["steps"]) == 117
    cmp = build_compare(path, "trace-drill-45d404d0", "trace-drill-11028023")
    assert cmp is not None and cmp["equal"] is True


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

def test_replay_endpoints_and_read_only(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/api/replay")
        assert status == 200
        payload = json.loads(body)
        assert len(payload["steps"]) == 68 and "anomalies" in payload

        status, body = _request(port, "GET", "/api/replay?from=10&to=12")
        payload = json.loads(body)
        assert status == 200
        assert [s["seq"] for s in payload["steps"]] == [10, 11, 12]

        status, _ = _request(port, "GET", "/api/replay?trace=nope")
        assert status == 404
        status, _ = _request(port, "GET", "/api/compare?trace_a=x&trace_b=y")
        assert status == 404
        status, _ = _request(port, "GET", "/api/compare")
        assert status == 404

        for method in ("POST", "PUT", "DELETE"):
            status, body = _request(port, method, "/api/replay", body="{}")
            assert status == 405 and b"read-only" in body
    finally:
        server.shutdown()


def test_compare_endpoint_on_two_trace_store(tmp_path):
    server, port = _serve(_two_trace_store(tmp_path))
    try:
        status, body = _request(
            port, "GET",
            "/api/compare?trace_a=trace-spike-efadd3b5&trace_b=trace-second")
        assert status == 200
        cmp = json.loads(body)
        assert cmp["equal"] is False and cmp["divergences"]
    finally:
        server.shutdown()


def test_page_carries_replay_scaffolding(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/")
        assert status == 200
        for marker in (b'id="view-replay"', b'id="nav-replay"',
                       b"APPLY SCOPE", b"NEXT ANOMALY", b"NEXT DECISION",
                       b'id="rp-rail"', b"COMPARE TRACES",
                       b"no autoplay", b"LANES"):
            assert marker in body, f"page missing {marker!r}"
    finally:
        server.shutdown()


def test_replay_steps_carry_walk_shape(tmp_path):
    """A replay step is the walk step, generalized: same keys the
    incident view renders, plus anomaly + state."""
    events = load_store_events(_full_store(tmp_path))
    payload = build_replay(_full_store(tmp_path))
    step = payload["steps"][0]
    for key in ("seq", "ts", "source", "agent", "type", "kind",
                "summary", "payload", "diff", "verified",
                "anomaly", "state"):
        assert key in step
    assert step["state"]["sources"]  # per-source counts ride along
    assert len(events) == 68
