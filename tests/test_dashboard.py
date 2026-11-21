"""Dashboard tests: read-only enforcement, store-only data path,
chain-status correctness, pending-approval derivation, live tailing.

The dashboard is a view, not a platform (Critic ruling, phase 2):
these tests pin the five conditions -- GET only, data exclusively
from the store through agenttrace.query, no state of its own.
"""

import http.client
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, store  # noqa: E402
from dashboard.server import build_state, make_server  # noqa: E402

SAMPLES = REPO / "samples"


def _full_store(tmp_path) -> Path:
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


def test_read_only_enforcement(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/")
        assert status == 200 and b"AGENT" in body and b"TRACE" in body
        status, body = _request(port, "GET", "/api/state")
        assert status == 200 and json.loads(body)["chain"]["status"] == "ok"
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, body = _request(port, method, "/api/state", body="{}")
            assert status == 405, f"{method} must be rejected, got {status}"
            assert b"read-only" in body
        # No action surface exists anywhere: an approve-style URL is 404.
        status, _ = _request(port, "POST", "/api/approve", body="{}")
        assert status == 405
        status, _ = _request(port, "GET", "/api/approve")
        assert status == 404
    finally:
        server.shutdown()


def test_state_shape_from_store(tmp_path):
    state = build_state(_full_store(tmp_path))
    assert state["chain"] == {"status": "ok", "count": state["events"],
                              "broken_seq": None, "detail": "every hash verified"}
    sources = {f["source"] for f in state["fleet"]}
    assert {"agent-canary", "agent-gate", "blast-radius", "agent-ledger"} <= sources
    assert state["active_trace"] == "trace-spike-efadd3b5"
    assert {b["source"] for b in state["tree"]} >= {"agent-canary", "agent-gate"}
    seqs = [e["seq"] for e in state["stream"]]
    assert seqs == sorted(seqs, reverse=True)
    # Cost column comes from the ledger span events in the store.
    assert state["cost"]["spans"] == 42
    assert state["cost"]["total_usd"] > 0


def test_chain_status_broken_at_exact_seq(tmp_path):
    path = _full_store(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    target = json.loads(lines[2])
    assert target["seq"] == 3
    assert "trace-spike" in lines[2]
    lines[2] = lines[2].replace("trace-spike", "trace-spikX", 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = build_state(path)
    assert state["chain"]["status"] == "broken"
    assert state["chain"]["broken_seq"] == 3


def test_empty_and_missing_store(tmp_path):
    state = build_state(tmp_path / "does-not-exist.jsonl")
    assert state["events"] == 0
    assert state["chain"]["status"] == "empty"
    assert all(f["status"] == "idle" and f["events"] == 0 for f in state["fleet"])
    assert state["pending_approvals"] == []


def test_pending_approval_blocks_fleet_status(tmp_path):
    # Only the plan_created line of the blast-radius sample: a plan
    # exists, no approval_granted follows -> pending + blocked.
    first_line = (SAMPLES / "blastradius-audit.jsonl").read_text(
        encoding="utf-8").splitlines()[0]
    partial = tmp_path / "br-partial.jsonl"
    partial.write_text(first_line + "\n", encoding="utf-8")
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_blastradius_audit(partial))
    state = build_state(path)
    assert len(state["pending_approvals"]) == 1
    assert state["pending_approvals"][0]["source"] == "blast-radius"
    br = next(f for f in state["fleet"] if f["source"] == "blast-radius")
    assert br["status"] == "blocked"


def test_live_tailing_via_events_endpoint(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    server, port = _serve(path)
    try:
        status, body = _request(port, "GET", "/api/state")
        count_before = json.loads(body)["events"]
        # A producer lands more events while the server is running.
        store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
        status, body = _request(port, "GET", f"/api/events?after_seq={count_before}")
        payload = json.loads(body)
        assert payload["count"] > count_before
        assert payload["events"] and all(
            e["seq"] > count_before for e in payload["events"])
        assert all(e["source"] == "agent-gate" for e in payload["events"])
    finally:
        server.shutdown()


def _ev(seq, source, type_, payload=None, kind="event"):
    return {"seq": seq, "ts": "2026-11-21T19:25:00Z", "trace_id": "t-1",
            "agent": source, "source": source, "type": type_,
            "kind": kind, "payload": payload or {}}


def test_page_is_hybrid_graph_plus_ops(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/")
        assert status == 200
        # Fleet-graph layer: SVG graph, store hub, beat ribbon.
        for marker in (b"FLEET GRAPH", b"TRACE RIBBON", b"AGENTTRACE STORE",
                       b'id="graph"', b'id="beats"'):
            assert marker in body, f"hybrid page missing {marker!r}"
        # Ops-console layer: span tree, decision cards, stream, cost bars.
        for marker in (b"SPAN TREE", b"DECISION RECORDS", b"EVENT STREAM",
                       b"COST BREAKDOWN"):
            assert marker in body, f"hybrid page missing {marker!r}"
    finally:
        server.shutdown()


def test_state_carries_graph_view_models(tmp_path):
    state = build_state(_full_store(tmp_path))
    nodes = {n["source"]: n for n in state["nodes"]}
    assert set(nodes) >= {"agent-canary", "agent-gate", "blast-radius",
                          "factory-floor-agent", "agent-ledger", "agent-chaos"}
    # The samples store is the spike trace: canary BLOCKed, gate flagged,
    # ledger costed -- the graph must say so, from the store alone.
    assert nodes["agent-canary"]["headline"] == "BLOCK"
    assert nodes["agent-canary"]["tone"] == "bad"
    assert nodes["agent-canary"]["events"] == 2
    assert nodes["agent-gate"]["headline"] == "FLAGGED"
    assert "$" in nodes["agent-ledger"]["detail"]
    assert nodes["factory-floor-agent"]["events"] == 0
    # Decision cards: canary's BLOCK + gate's two computed records.
    choices = {(d["decision_point"], d["choice"]) for d in state["decisions"]}
    assert ("agent-canary.compare", "BLOCK") in choices
    assert len(state["decisions"]) == 3
    # Cost bars reconcile with the cost summary (row rounding aside).
    bars_total = sum(r["cost_usd"] for r in state["cost_steps"])
    assert abs(bars_total - state["cost"]["total_usd"]) < 0.001
    assert all(" / " in r["label"] or r["label"].startswith("+")
               for r in state["cost_steps"])


def test_beats_on_samples_store(tmp_path):
    from agenttrace import query as q
    from dashboard.server import load_store_events
    events = load_store_events(_full_store(tmp_path))
    prog = q.drill_progress(events, "trace-spike-efadd3b5")
    lit = {b["key"]: b["lit"] for b in prog["beats"]}
    # Samples: verdict, screening, plan and ledger spans exist; the
    # sample blast-radius run has no kill seam and completes cleanly,
    # and there is no factory-floor or chaos producer in the store.
    assert lit == {"block": True, "flagged": True, "plan": True,
                   "kill": False, "rollback": False, "answered": False,
                   "costed": True, "matrix": False}
    assert prog["complete"] is False
    frontier = [b["key"] for b in prog["beats"] if b["frontier"]]
    assert frontier == ["costed"]


def test_beats_kill_seam_and_full_completion():
    from agenttrace import query as q
    br_detail = "step s3/6: Health gate for checkout-api-2"
    events = [
        _ev(1, "agent-canary", "compare_verdict",
            {"base_version": "v1", "candidate_version": "v2-bad",
             "decision": "BLOCK", "reasons": ["r"]}),
        _ev(2, "agent-gate", "screening_findings",
            {"findings": [{"pattern": "ssn"}]}),
        _ev(3, "blast-radius", "plan_created", {"detail": "6 step(s)"}),
        _ev(4, "blast-radius", "step_started",
            {"action": "health_check", "resource": "i-1", "detail": br_detail}),
        _ev(5, "blast-radius", "step_started",
            {"action": "health_check", "resource": "i-1", "detail": br_detail}),
        _ev(6, "blast-radius", "rollback_step", {"detail": "inverse"}),
        _ev(7, "blast-radius", "run_finished", {"result": "rolled_back"}),
        _ev(8, "factory-floor-agent", "outcome", {"outcome": "answered"}),
        _ev(9, "agent-ledger", "span", {"name": "run", "attributes": {}}),
        _ev(10, "agent-chaos", "chaos_report",
            {"headline": {"silent_failure_rate_defenses_off": 1.0,
                          "silent_failure_rate_defenses_on": 0.0}, "rows": 12}),
    ]
    prog = q.drill_progress(events, "t-1")
    assert all(b["lit"] for b in prog["beats"]) and prog["complete"] is True
    kill = next(b for b in prog["beats"] if b["key"] == "kill")
    assert kill["seq"] == 5  # the replayed step_started is the seam
    nodes = {n["source"]: n for n in q.graph_nodes(events, "t-1", now=0.0)}
    assert nodes["blast-radius"]["headline"] == "ROLLED BACK"
    assert "kill -9" in nodes["blast-radius"]["detail"]


def test_server_reads_store_only():
    """The server's data path is the store + the query layer, only.

    It must never import the ingestors or reach for producer
    artifacts: if a datum is not a chained event, it is not served.
    """
    source = (REPO / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "agenttrace.query" in source and "agenttrace.store" in source
    for forbidden in ("import ingest", "ingest_canary", "ingest_gate",
                      "subprocess", "samples/", "producer artifacts are"):
        assert forbidden not in source, f"server.py must not reference {forbidden!r}"
