"""Slice-3 tests: instance identity (query layer + ingest passthrough)
and the Tier 2 config view (allowlisted reads, redaction, honest
absence) + propose-only scaffolding.

Pins the slice's honesty rules: a missing instance falls back to the
actor name (pre-instance stores group exactly as before); the
instances section exists only when a producer actually declared
instance identity; config is served only from the allowlist under
the fleet root, with secret-shaped values redacted; and the config
endpoint is as read-only as the rest of the dashboard.
"""

import hashlib
import http.client
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from agenttrace import ingest, query as q, store  # noqa: E402
from agenttrace.schema import SchemaError, new_event, validate_event  # noqa: E402
from dashboard.server import (build_agent, build_agent_config,  # noqa: E402
                              build_agent_events, make_server)


def _ev(seq, source, type_, payload=None, kind="event", trace="t-1",
        ts="2026-11-22T19:25:00Z", instance=None, agent=None):
    e = {"seq": seq, "ts": ts, "trace_id": trace,
         "agent": agent or source, "source": source, "type": type_,
         "kind": kind, "payload": payload or {}}
    if instance is not None:
        e["instance"] = instance
    return e


def _serve(store_path, fleet_root=None):
    server = make_server(store_path, "127.0.0.1", 0, fleet_root=fleet_root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# ---------------------------------------------------------------------
# Schema: the optional instance field
# ---------------------------------------------------------------------

def test_new_event_omits_instance_by_default():
    e = new_event(trace_id="t", ts="2026-11-22T00:00:00Z", source="s",
                  agent="a", type="x", payload={})
    assert "instance" not in e


def test_new_event_carries_declared_instance():
    e = new_event(trace_id="t", ts="2026-11-22T00:00:00Z", source="s",
                  agent="a", type="x", payload={}, instance="chaos-a")
    assert e["instance"] == "chaos-a"
    assert validate_event(e) is e


def test_instance_must_be_a_string_when_present():
    e = new_event(trace_id="t", ts="2026-11-22T00:00:00Z", source="s",
                  agent="a", type="x", payload={})
    e["instance"] = 7
    with pytest.raises(SchemaError):
        validate_event(e)


# ---------------------------------------------------------------------
# Query layer: effective instance, rollups, fleet counts
# ---------------------------------------------------------------------

def test_event_instance_falls_back_to_agent():
    assert q.event_instance(_ev(1, "agent-chaos", "x")) == "agent-chaos"
    assert q.event_instance(
        _ev(1, "agent-chaos", "x", instance="chaos-a")) == "chaos-a"


def test_query_events_instance_filter_uses_effective_instance():
    events = [
        _ev(1, "agent-chaos", "scenario_result", instance="chaos-a"),
        _ev(2, "agent-chaos", "scenario_result", instance="chaos-b"),
        _ev(3, "agent-chaos", "scenario_result"),  # falls back to agent
    ]
    assert [e["seq"] for e in q.query_events(events, instance="chaos-a")] == [1]
    assert [e["seq"] for e in q.query_events(events, instance="agent-chaos")] == [3]


def test_source_summary_instances_split_tallies():
    events = [
        _ev(1, "agent-chaos", "chaos_report", instance="chaos-a"),
        _ev(2, "agent-chaos", "scenario_result",
            {"outcome": "detected"}, instance="chaos-a"),
        _ev(3, "agent-chaos", "chaos_report", instance="chaos-b"),
        _ev(4, "agent-chaos", "scenario_result",
            {"outcome": "missed"}, instance="chaos-b"),
    ]
    s = q.source_summary(events, "agent-chaos", now=0.0)
    assert s["has_instance_data"] is True
    rows = {r["instance"]: r for r in s["instances"]}
    assert set(rows) == {"chaos-a", "chaos-b"}
    assert rows["chaos-a"]["events"] == 2
    assert rows["chaos-a"]["explicit"] is True
    assert rows["chaos-b"]["last_summary"] != ""
    # runs stay runs: grouped by trace, untouched by instance identity
    assert len(s["runs"]) == 1


def test_source_summary_without_instance_data():
    events = [_ev(1, "agent-chaos", "chaos_report"),
              _ev(2, "agent-chaos", "scenario_result", {"outcome": "x"})]
    s = q.source_summary(events, "agent-chaos", now=0.0)
    assert s["has_instance_data"] is False
    # the fallback grouping still exists in the payload, labeled as such
    assert s["instances"][0]["explicit"] is False
    assert s["instances"][0]["instance"] == "agent-chaos"


def test_fleet_table_instance_counts():
    events = [
        _ev(1, "agent-chaos", "chaos_report", instance="chaos-a"),
        _ev(2, "agent-chaos", "chaos_report", instance="chaos-b"),
        _ev(3, "agent-gate", "policy_decision"),
    ]
    rows = {r["source"]: r for r in q.fleet_table(events, now=0.0)}
    assert rows["agent-chaos"]["instances"] == 2
    assert rows["agent-chaos"]["has_instance_data"] is True
    assert rows["agent-gate"]["has_instance_data"] is False


# ---------------------------------------------------------------------
# Ingest: producer-declared instance lands on events (incl. decisions)
# ---------------------------------------------------------------------

def test_ingest_chaos_report_instance(tmp_path):
    report = {
        "run_id": "trace-x", "generated_at": "2026-11-22T00:00:00Z",
        "instance": "chaos-b",
        "headline": {"rows": 1},
        "rows": [{"scenario": "s1", "outcome": "detected"}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    events = ingest.ingest_chaos_report(path)
    assert events and all(e["instance"] == "chaos-b" for e in events)


def test_ingest_canary_verdict_instance_reaches_decision_record(tmp_path):
    verdict = {
        "run_id": "trace-x", "generated_at": "2026-11-22T00:00:00Z",
        "instance": "canary-2", "decision": "BLOCK",
        "reasons": ["drop"], "base_version": "v1",
        "candidate_version": "v2", "success_drop": 0.5,
        "cost_delta_pct": 0.0, "latency_delta_pct": 0.0,
        "base_aggregate": {"success_rate": 1.0},
        "cand_aggregate": {"success_rate": 0.5, "forbidden_hits": 1},
    }
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(verdict), encoding="utf-8")
    events = ingest.ingest_canary_verdict(path)
    assert all(e["instance"] == "canary-2" for e in events)
    assert any(e["kind"] == "decision_record" for e in events)


def test_ingest_without_instance_keeps_events_keyless(tmp_path):
    report = {
        "run_id": "trace-x", "generated_at": "2026-11-22T00:00:00Z",
        "headline": {}, "rows": [],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    events = ingest.ingest_chaos_report(path)
    assert events and all("instance" not in e for e in events)


def test_store_roundtrip_with_instances(tmp_path):
    path = tmp_path / "store.jsonl"
    events = ingest.ingest_chaos_report(_write_report(tmp_path, "chaos-a"))
    events += ingest.ingest_chaos_report(_write_report(tmp_path, "chaos-b"))
    store.append_events(path, events)
    result = store.verify(path)
    assert result.ok and result.count == len(events)
    loaded = store.load_events(path)
    assert {e["instance"] for e in loaded} == {"chaos-a", "chaos-b"}


def _write_report(tmp_path, instance):
    report = {
        "run_id": f"trace-{instance}",
        "generated_at": "2026-11-22T00:00:00Z",
        "instance": instance,
        "headline": {},
        "rows": [{"scenario": "s1", "outcome": "detected"}],
    }
    path = tmp_path / f"report-{instance}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------------
# Config view: allowlist, redaction, honest absence
# ---------------------------------------------------------------------

POLICY = {
    "version": 1,
    "defaultEffect": "deny",
    "agents": [{"name": "demo-agent", "token": "demo-agent-token",
                "allowedTools": ["list_*"]}],
    "screening": {"enabled": True, "pii": "redact"},
}


def _fleet(tmp_path, with_policy=True):
    root = tmp_path / "fleet"
    gate = root / "agent-gate"
    gate.mkdir(parents=True)
    if with_policy:
        (gate / "policy.json").write_text(json.dumps(POLICY, indent=2),
                                          encoding="utf-8")
    return root


def test_config_view_redacts_tokens_and_hashes(tmp_path):
    root = _fleet(tmp_path)
    payload = build_agent_config("agent-gate", str(root))
    assert payload["available"] is True
    doc = payload["documents"][0]
    raw = (root / "agent-gate" / "policy.json").read_bytes()
    assert doc["sha256"] == hashlib.sha256(raw).hexdigest()
    assert doc["config"]["agents"][0]["token"] == "set (redacted)"
    assert "$.agents[0].token" in doc["redacted_paths"]
    # the raw secret value is nowhere in what the server hands out
    assert "demo-agent-token" not in json.dumps(payload)
    # non-secret structure passes through untouched
    assert doc["config"]["screening"] == {"enabled": True, "pii": "redact"}


def test_config_view_honest_absence():
    unset = build_agent_config("agent-gate", None)
    assert unset["available"] is False
    assert "AGENTTRACE_FLEET_ROOT" in unset["reason"]
    unknown = build_agent_config("agent-chaos", "/anywhere")
    assert unknown["available"] is False
    assert "allowlisted" in unknown["reason"]


def test_config_view_missing_file_is_honest(tmp_path):
    root = _fleet(tmp_path, with_policy=False)
    payload = build_agent_config("agent-gate", str(root))
    assert payload["available"] is False
    assert "no allowlisted config file" in payload["reason"]


def test_config_endpoint_and_read_only(tmp_path):
    store_path = tmp_path / "store.jsonl"
    store.append_events(store_path, ingest.ingest_chaos_report(
        _write_report(tmp_path, "chaos-a")))
    root = _fleet(tmp_path)
    server, port = _serve(store_path, fleet_root=str(root))
    try:
        status, data = _request(port, "GET", "/api/agent-config?source=agent-gate")
        assert status == 200
        payload = json.loads(data)
        assert payload["available"] is True
        assert "demo-agent-token" not in data.decode()
        status, _ = _request(port, "POST", "/api/agent-config?source=agent-gate",
                             body=b"{}")
        assert status == 405  # no write surface, not even for config
        status, _ = _request(port, "GET", "/api/agent-config")
        assert status == 400
    finally:
        server.shutdown()


def test_agent_events_instance_filter(tmp_path):
    store_path = tmp_path / "store.jsonl"
    events = ingest.ingest_chaos_report(_write_report(tmp_path, "chaos-a"))
    events += ingest.ingest_chaos_report(_write_report(tmp_path, "chaos-b"))
    store.append_events(store_path, events)
    payload = build_agent_events(store_path, "agent-chaos", instance="chaos-b")
    assert payload["total"] == 2
    assert all(e["instance"] == "chaos-b" for e in payload["events"])
    summary = build_agent(store_path, "agent-chaos")
    assert summary["has_instance_data"] is True
    assert {r["instance"] for r in summary["instances"]} == {"chaos-a", "chaos-b"}


def test_page_carries_slice3_scaffolding():
    page = (REPO / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="ag-instances-sec"' in page
    assert 'id="ag-config"' in page
    assert "DOWNLOAD PATCH" in page
    assert "git apply" in page
    assert "AGENT_INSTANCE" in page
