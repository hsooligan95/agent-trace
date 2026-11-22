"""Slice-1 tier tests: admin overview (Tier 1) and per-agent-type
(Tier 2) derivations + endpoints.

Pins the honesty rules the tiers were built under: tallies carry
reasons, cost is None (never a borrowed zero) where the ledger
emitted no spans, runs are grouped by trace and never upgraded to
"instances", status stays categorical -- and the new endpoints are
as read-only as the rest of the dashboard.
"""

import http.client
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, query as q, store  # noqa: E402
from dashboard.server import (build_agent, build_agent_events,  # noqa: E402
                              build_overview, build_walk, make_server)

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


def _ev(seq, source, type_, payload=None, kind="event", trace="t-1",
        ts="2026-11-21T19:25:00Z"):
    return {"seq": seq, "ts": ts, "trace_id": trace,
            "agent": source, "source": source, "type": type_,
            "kind": kind, "payload": payload or {}}


# ---------------------------------------------------------------------
# Query-layer derivations
# ---------------------------------------------------------------------

def test_decision_tallies_count_rolled_back_runs():
    events = [
        _ev(1, "agent-canary", "compare", kind="decision_record",
            payload={"decision_point": "agent-canary.compare",
                     "choice": "BLOCK", "evidence": {"reasons": ["drop"]}}),
        _ev(2, "agent-gate", "policy", kind="decision_record",
            payload={"decision_point": "agent-gate.policy",
                     "choice": "allow", "evidence": {"reason": "Read-only."}}),
        _ev(3, "agent-gate", "screening", kind="decision_record",
            payload={"decision_point": "agent-gate.screening",
                     "choice": "flagged", "evidence": {"patterns": ["ssn"]}}),
        _ev(4, "blast-radius", "run_finished", {"result": "rolled_back"}),
    ]
    t = q.decision_tallies(events)
    assert t["total"] == 3
    assert t["buckets"] == {"allow": 1, "block": 1, "rollback": 1, "other": 1}
    assert t["rollback_runs"] == 1  # the event-shaped rollback is counted
    reasons = {(r["label"], r["kind"]) for r in q.decision_reasons(events)}
    assert ("drop", "reason") in reasons
    assert ("Read-only.", "reason") in reasons
    assert ("pattern: ssn", "reason") in reasons


def test_trigger_labels_for_blast_radius_and_chaos():
    events = [
        _ev(1, "blast-radius", "verify_check",
            {"result": "FAIL", "detail": "disk healthy: expected ok"}),
        _ev(2, "blast-radius", "verify_failed", {"detail": "8/9 checks passed"}),
        _ev(3, "agent-chaos", "scenario_result",
            {"scenario": "poisoned-metric", "fault": "value -47.3",
             "fault_injected": True, "detected": False,
             "outcome": "failed_silently"}),
        _ev(4, "agent-chaos", "scenario_result",
            {"scenario": "baseline", "fault_injected": False,
             "detected": False, "outcome": "success"}),
    ]
    labels = {r["label"] for r in q.decision_reasons(events)}
    assert any(l.startswith("verify check failed: disk healthy") for l in labels)
    assert any(l.startswith("post-run verification failed: 8/9") for l in labels)
    assert any(l.startswith("undetected fault: poisoned-metric") for l in labels)
    assert len(labels) == 3  # the clean baseline contributes nothing
    outcomes = {o["label"]: o["count"] for o in q.outcome_tallies(events)}
    assert outcomes == {"failed_silently": 1, "success": 1}


def test_fleet_table_rows_and_cost_honesty(tmp_path):
    from dashboard.server import load_store_events
    events = load_store_events(_full_store(tmp_path))
    rows = {r["source"]: r for r in q.fleet_table(events, now=0.0)}
    assert rows["agent-canary"]["decisions"]["block"] == 1
    assert rows["agent-canary"]["events"] == 2
    assert rows["agent-canary"]["runs"] == 1
    # Cost exists only where the ledger emitted spans.
    assert rows["agent-ledger"]["cost_usd"] is not None
    assert rows["agent-ledger"]["cost_usd"] > 0
    assert rows["agent-canary"]["cost_usd"] is None
    assert rows["blast-radius"]["cost_usd"] is None
    # Sparklines are 24 buckets and account for every event of the row.
    for r in rows.values():
        assert len(r["sparkline"]) == 24
        assert sum(r["sparkline"]) == r["events"]
    # Status is categorical; there is no numeric score anywhere.
    assert rows["agent-canary"]["status"] in ("blocked", "running", "idle")
    assert not any("score" in k for r in rows.values() for k in r)


def test_overview_payload(tmp_path):
    ov = build_overview(_full_store(tmp_path))
    assert ov["events"] == 68
    assert ov["traces"] == ["trace-spike-efadd3b5"]
    assert ov["producers_active"] == 4
    assert ov["decisions"]["total"] == 3
    assert ov["decisions"]["buckets"]["block"] == 1
    assert ov["head"]["seq"] == 68 and ov["head"]["hash"]
    assert ov["chain"]["status"] == "ok"
    # Decision feed is newest first and carries the canary BLOCK.
    seqs = [d["seq"] for d in ov["decision_feed"]]
    assert seqs == sorted(seqs, reverse=True)
    assert ov["decision_feed"][-1]["choice"] == "BLOCK"
    # The samples store's run completed, so only the canary BLOCK is
    # an incident on the attention strip.
    assert [(i["kind"], i["source"]) for i in ov["incidents"]] == [
        ("block", "agent-canary")]


def test_source_summary_gate(tmp_path):
    from dashboard.server import load_store_events
    events = load_store_events(_full_store(tmp_path))
    ag = q.source_summary(events, "agent-gate", now=0.0)
    assert ag["decisions"]["by_choice"] == {"allow": 1, "flagged": 1}
    labels = {r["label"] for r in ag["reasons"]}
    assert "Read-only retrieval." in labels
    assert any(l.startswith("pattern: ") for l in labels)
    assert ag["cost"]["total_usd"] is None  # gate emits no ledger spans
    assert len(ag["runs"]) == 1
    run = ag["runs"][0]
    assert run["trace_id"] == "trace-spike-efadd3b5"
    assert run["events"] == ag["events"] == 7
    assert run["cost_usd"] is None
    assert ag["role"]  # a plain-language role line exists


def test_source_summary_unknown_and_absent(tmp_path):
    from dashboard.server import load_store_events
    events = load_store_events(_full_store(tmp_path))
    assert q.source_summary(events, "no-such-agent", now=0.0) is None
    # Known but silent in this store: a valid, empty page -- not None.
    ff = q.source_summary(events, "factory-floor-agent", now=0.0)
    assert ff is not None and ff["events"] == 0 and ff["runs"] == []


def test_runs_are_traces_not_instances():
    events = [
        _ev(1, "agent-chaos", "scenario_result", {"outcome": "success"}, trace="t-1"),
        _ev(2, "agent-chaos", "scenario_result", {"outcome": "success"}, trace="t-2"),
        _ev(3, "agent-chaos", "chaos_report", {}, trace="t-2"),
    ]
    ag = q.source_summary(events, "agent-chaos", now=0.0)
    assert [r["trace_id"] for r in ag["runs"]] == ["t-1", "t-2"]
    assert ag["runs"][1]["events"] == 2
    assert not any("instance" in k for r in ag["runs"] for k in r)


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

def test_tier_endpoints_and_read_only(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/api/overview")
        assert status == 200
        ov = json.loads(body)
        assert ov["events"] == 68 and "fleet" in ov and "incidents" in ov

        status, body = _request(port, "GET", "/api/agent?source=agent-gate")
        assert status == 200
        assert json.loads(body)["source"] == "agent-gate"

        status, _ = _request(port, "GET", "/api/agent?source=nope")
        assert status == 404
        status, _ = _request(port, "GET", "/api/agent")
        assert status == 404

        # Faceted stream: decisions-only returns just the records.
        status, body = _request(
            port, "GET", "/api/agent-events?source=agent-gate&decisions=1")
        payload = json.loads(body)
        assert status == 200 and payload["total"] == 2
        assert all(e["kind"] == "decision_record" for e in payload["events"])
        # Kind facet agrees.
        status, body = _request(
            port, "GET", "/api/agent-events?source=agent-gate&kind=event")
        payload = json.loads(body)
        assert payload["total"] == 5
        assert all(e["kind"] == "event" for e in payload["events"])
        status, _ = _request(port, "GET", "/api/agent-events")
        assert status == 400

        # Walk: the samples blast-radius run, anchored on run_finished.
        status, body = _request(
            port, "GET", "/api/walk?source=blast-radius&trace=trace-spike-efadd3b5")
        walk = json.loads(body)
        assert status == 200
        assert walk["anchor"]["type"] == "run_finished"
        assert walk["incident"]["kind"] == "run"
        assert all(s["source"] == "blast-radius" for s in walk["steps"])
        assert walk["first_unverified_seq"] is None
        status, _ = _request(port, "GET", "/api/walk?source=agent-gate&trace=nope")
        assert status == 404

        # The new surface is exactly as read-only as the old.
        for method in ("POST", "PUT", "DELETE"):
            status, body = _request(port, method, "/api/overview", body="{}")
            assert status == 405 and b"read-only" in body
    finally:
        server.shutdown()


def test_build_agent_events_cap_is_not_silent(tmp_path):
    path = _full_store(tmp_path)
    payload = build_agent_events(path, "agent-ledger", limit=5)
    assert payload["shown"] == 5 and payload["total"] == 42
    assert payload["events"][0]["seq"] > payload["events"][-1]["seq"]


def test_page_carries_tier_scaffolding(tmp_path):
    server, port = _serve(_full_store(tmp_path))
    try:
        status, body = _request(port, "GET", "/")
        assert status == 200
        for marker in (b"NEEDS ATTENTION", b"CHAIN CONSISTENCY",
                       b"ONE ROW PER AGENT TYPE", b'id="view-overview"',
                       b'id="view-agent"', b'id="view-topology"',
                       b'id="fleet-body"', b'id="ag-runs-body"',
                       b"REPLAY THIS RUN", b"no instance field yet",
                       b"Not provenance"):
            assert marker in body, f"page missing {marker!r}"
    finally:
        server.shutdown()
