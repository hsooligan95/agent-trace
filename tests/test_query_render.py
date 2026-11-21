from pathlib import Path

from agenttrace import ingest, store
from agenttrace.query import query_events
from agenttrace.render import render_timeline

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _loaded_store(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    return store.load_events(path)


def test_query_by_trace_id(tmp_path):
    events = _loaded_store(tmp_path)
    matched = query_events(events, trace_id="trace-spike-efadd3b5")
    assert len(matched) == len(events)  # all three producers share the ID


def test_query_by_agent_and_type(tmp_path):
    events = _loaded_store(tmp_path)
    gate = query_events(events, agent="demo-agent")
    assert gate and all(e["source"] == "agent-gate" for e in gate)
    blocks = query_events(events, type="decision_record")
    assert blocks and all(e["kind"] == "decision_record" for e in blocks)
    assert query_events(events, trace_id="no-such-trace") == []


def test_render_from_store_alone(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    events = store.load_events(path)
    page = render_timeline(events, verify_result=store.verify(path))
    assert "trace-spike-efadd3b5" in page
    assert "Chain OK" in page
    assert "agent-canary" in page and "agent-gate" in page
    assert "Decision records" in page
    assert "BLOCK" in page
