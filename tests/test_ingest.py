"""Contract tests: each producer's real sample output must ingest into
schema-valid events. If a producer changes its output shape, these fail
-- loudly, in CI, instead of silently dropping events in the store.
"""

from pathlib import Path

from agenttrace import ingest
from agenttrace.schema import (
    KIND_DECISION_RECORD,
    KIND_RATIONALE_ANNOTATION,
    validate_event,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _assert_all_valid(events):
    assert events, "ingestor produced no events"
    for event in events:
        validate_event(event)
        assert event["kind"] != KIND_RATIONALE_ANNOTATION


def test_canary_sample_contract():
    events = ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json")
    _assert_all_valid(events)
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    assert any(e["type"] == "compare_verdict" for e in events)


def test_gate_sample_contract():
    events = ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl")
    _assert_all_valid(events)
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    types = {e["type"] for e in events}
    assert "screening_findings" in types
    assert "policy_decision" in types


def test_blastradius_sample_contract():
    events = ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl")
    _assert_all_valid(events)
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    assert all(e["source"] == "blast-radius" for e in events)
    assert any(e["type"] == "run_finished" for e in events)


def test_ledger_sample_contract():
    events = ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp.json")
    _assert_all_valid(events)
    assert all(e["source"] == "agent-ledger" for e in events)
    assert all(e["type"] == "span" for e in events)
    # Cost-bearing spans exist in the real export.
    assert any("agentledger.cost_usd" in e["payload"]["attributes"]
               for e in events)


def test_factory_floor_sample_contract():
    events = ingest.ingest_factory_floor_trace(SAMPLES / "factory-floor-trace.jsonl")
    _assert_all_valid(events)
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    types = [e["type"] for e in events]
    assert types[:4] == ["run_start", "plan", "retrieval", "groundedness"]
    assert "outcome" in types and "run_end" in types
    retrieval = next(e for e in events if e["type"] == "retrieval")
    assert retrieval["payload"]["hits"]  # real passages, recorded as data


def test_factory_floor_decision_record_from_groundedness():
    events = ingest.ingest_factory_floor_trace(SAMPLES / "factory-floor-trace.jsonl")
    records = [e for e in events if e["kind"] == KIND_DECISION_RECORD]
    assert len(records) == 1
    record = records[0]["payload"]
    assert record["decision_point"] == "factory-floor-agent.groundedness"
    assert record["computed"] is True
    assert record["choice"] == "answered"
    evidence = record["evidence"]
    assert evidence["threshold"] == 0.35
    assert evidence["claims_checked"] == len(evidence["claims"])
    assert evidence["min_claim_overlap"] is not None
    # The agent enumerates no options for its answer text; the record
    # must not pretend otherwise — its options are the checker's two
    # mechanical outcomes, nothing narrated.
    assert record["options"] == ["answered", "abstained"]


def test_chaos_sample_contract():
    events = ingest.ingest_chaos_report(SAMPLES / "chaos-report.json")
    _assert_all_valid(events)
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    assert all(e["source"] == "agent-chaos" for e in events)
    assert events[0]["type"] == "chaos_report"
    rows = [e for e in events if e["type"] == "scenario_result"]
    assert len(rows) == 12
    outcomes = {e["payload"]["outcome"] for e in rows}
    assert "failed_silently" in outcomes and "recovered" in outcomes
    # Chaos emits audit-style results, not computed decision records
    # for AgentTrace's purposes in this phase.
    assert not [e for e in events if e["kind"] == KIND_DECISION_RECORD]


def test_ledger_joined_sample_joins_external_trace():
    events = ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp-joined.json")
    _assert_all_valid(events)
    # Every span joined the producer-declared external trace ID...
    assert all(e["trace_id"] == "trace-spike-efadd3b5" for e in events)
    # ...while the native OTLP identity is preserved in the payload.
    otlp_ids = {e["payload"]["otlp_trace_id"] for e in events}
    assert len(otlp_ids) == 1 and otlp_ids != {"trace-spike-efadd3b5"}
    assert all(e["payload"]["agenttrace_trace_id"] == "trace-spike-efadd3b5"
               for e in events)
    assert any("agentledger.cost_usd" in e["payload"]["attributes"]
               for e in events)


def test_ledger_unstamped_sample_keeps_otlp_identity():
    events = ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp.json")
    _assert_all_valid(events)
    trace_ids = {e["trace_id"] for e in events}
    assert trace_ids == {e["payload"]["otlp_trace_id"] for e in events}
    assert all("agenttrace_trace_id" not in e["payload"] for e in events)


def test_all_producers_together_in_one_store(tmp_path):
    path = tmp_path / "store.jsonl"
    from agenttrace import store
    total = 0
    total += store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    total += store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    total += store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    total += store.append_events(path, ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp.json"))
    total += store.append_events(path, ingest.ingest_factory_floor_trace(SAMPLES / "factory-floor-trace.jsonl"))
    total += store.append_events(path, ingest.ingest_chaos_report(SAMPLES / "chaos-report.json"))
    result = store.verify(path)
    assert result.ok and result.count == total


def test_six_producers_one_trace(tmp_path):
    """Every producer's sample was emitted under trace-spike-efadd3b5
    (the ledger one via its root-span stamp): the whole fleet lands in
    one store under one trace ID, and the chain verifies."""
    path = tmp_path / "store.jsonl"
    from agenttrace import store
    total = 0
    total += store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    total += store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    total += store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    total += store.append_events(path, ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp-joined.json"))
    total += store.append_events(path, ingest.ingest_factory_floor_trace(SAMPLES / "factory-floor-trace.jsonl"))
    total += store.append_events(path, ingest.ingest_chaos_report(SAMPLES / "chaos-report.json"))
    events = store.load_events(path)
    assert len(events) == total
    assert {e["trace_id"] for e in events} == {"trace-spike-efadd3b5"}
    assert {e["source"] for e in events} == {
        "agent-canary", "agent-gate", "blast-radius",
        "agent-ledger", "factory-floor-agent", "agent-chaos",
    }
    result = store.verify(path)
    assert result.ok and result.count == total


def test_decision_records_only_from_computed_points():
    canary = ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json")
    records = [e for e in canary if e["kind"] == KIND_DECISION_RECORD]
    assert len(records) == 1
    record = records[0]["payload"]
    assert record["computed"] is True
    assert record["choice"] == "BLOCK"
    assert record["evidence"]["reasons"]  # copied from the verdict itself

    gate = ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl")
    gate_records = [e for e in gate if e["kind"] == KIND_DECISION_RECORD]
    points = {r["payload"]["decision_point"] for r in gate_records}
    assert points == {"agent-gate.policy", "agent-gate.screening"}
    screening = next(r for r in gate_records
                     if r["payload"]["decision_point"] == "agent-gate.screening")
    assert "ignore-instructions" in screening["payload"]["evidence"]["patterns"]

    # blast-radius emits audit records, not decision records, in this phase.
    br = ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl")
    assert not [e for e in br if e["kind"] == KIND_DECISION_RECORD]
