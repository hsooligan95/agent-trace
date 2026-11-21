"""TraceReplay: deterministic replay / state reconstruction over the
store, and trace-vs-trace diffs that name divergences -- including a
planted one. Drill-store tests use the real captured drill outputs and
skip if those local artifacts are absent.
"""

import copy
from pathlib import Path

import pytest

from agenttrace import ingest, store
from agenttrace.cli import main as cli_main
from agenttrace.replay import diff_traces, replay_steps, state_at

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
DRILL_A = (Path(__file__).resolve().parent.parent / "drill" / "output"
           / "trace-drill-45d404d0" / "store.jsonl")
DRILL_B = (Path(__file__).resolve().parent.parent / "drill" / "output"
           / "trace-drill-217a8fd6" / "store.jsonl")

needs_drills = pytest.mark.skipif(
    not (DRILL_A.exists() and DRILL_B.exists()),
    reason="captured drill stores not present")


def _sample_events(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    store.append_events(path, ingest.ingest_ledger_otlp(SAMPLES / "ledger-otlp-joined.json"))
    return store.load_events(path)


def test_state_at_end_of_trace(tmp_path):
    events = _sample_events(tmp_path)
    state = state_at(events, "trace-spike-efadd3b5")
    assert state["events"] == len(events)
    assert state["cost"]["total_usd"] > 0
    choices = [d["choice"] for d in state["decisions"]]
    assert "BLOCK" in choices and "flagged" in choices


def test_state_at_prefix_is_a_prefix(tmp_path):
    events = _sample_events(tmp_path)
    third = events[2]
    state = state_at(events, "trace-spike-efadd3b5", upto_seq=third["seq"])
    assert state["events"] == 3
    assert state["last_seq"] == third["seq"]
    # The ledger spans land later in this store: no cost yet.
    assert state["cost"]["total_usd"] == 0.0
    assert set(state["sources"]) == {"agent-canary", "agent-gate"}


def test_replay_steps_walk_in_seq_order(tmp_path):
    events = _sample_events(tmp_path)
    steps = list(replay_steps(events, "trace-spike-efadd3b5"))
    assert [e["seq"] for e, _ in steps] == [e["seq"] for e in events]
    # State grows monotonically and ends at the full state.
    counts = [s["events"] for _, s in steps]
    assert counts == list(range(1, len(events) + 1))
    assert steps[-1][1]["cost"] == state_at(events, "trace-spike-efadd3b5")["cost"]


def test_state_at_unknown_trace_is_empty(tmp_path):
    events = _sample_events(tmp_path)
    state = state_at(events, "no-such-trace")
    assert state["events"] == 0 and state["decisions"] == []


def test_diff_trace_against_itself_is_equal(tmp_path):
    events = _sample_events(tmp_path)
    result = diff_traces(events, "trace-spike-efadd3b5",
                         events, "trace-spike-efadd3b5")
    assert result["equal"] and result["divergences"] == []


def test_diff_finds_planted_divergence(tmp_path):
    events = _sample_events(tmp_path)
    planted = copy.deepcopy(events)
    # Plant: the re-run's canary PROMOTES instead of BLOCKing, and one
    # gate event class is swapped for a different type.
    for e in planted:
        if e["kind"] == "decision_record" \
                and e["payload"].get("decision_point") == "agent-canary.compare":
            e["payload"]["choice"] = "PROMOTE"
    gate_event = next(e for e in planted if e["source"] == "agent-gate"
                      and e["type"] == "tool_executed")
    gate_event["type"] = "tool_withheld"
    result = diff_traces(events, "trace-spike-efadd3b5",
                         planted, "trace-spike-efadd3b5")
    assert not result["equal"]
    kinds = {d["kind"] for d in result["divergences"]}
    assert "decisions_differ" in kinds
    assert "class_only_in_b" in kinds  # agent-gate/tool_withheld/event
    assert "sequence_diverges" in kinds
    named = " ".join(d["detail"] for d in result["divergences"])
    assert "tool_withheld" in named and "PROMOTE" in named


@needs_drills
def test_drill_store_replay_full_state():
    events = store.load_events(DRILL_A)
    state = state_at(events, "trace-drill-45d404d0")
    assert state["events"] == 117
    assert state["cost"] == {"spans": 42, "billable_steps": 31,
                              "total_usd": 0.57313}
    assert len(state["decisions"]) == 4
    assert set(state["sources"]) == {
        "agent-canary", "agent-gate", "blast-radius",
        "factory-floor-agent", "agent-ledger", "agent-chaos",
    }


@needs_drills
def test_two_real_drill_runs_diff_equal():
    """Two independent executions of the flagship drill are
    behaviorally identical under TraceReplay's diff (timestamps and
    hashes excluded by design)."""
    a = store.load_events(DRILL_A)
    b = store.load_events(DRILL_B)
    result = diff_traces(a, "trace-drill-45d404d0",
                         b, "trace-drill-217a8fd6")
    assert result["equal"], result["divergences"]


@needs_drills
def test_drill_diff_finds_planted_divergence():
    a = store.load_events(DRILL_A)
    planted = copy.deepcopy(a)
    del planted[10]  # an event silently missing from the re-run
    result = diff_traces(a, "trace-drill-45d404d0",
                         planted, "trace-drill-45d404d0")
    assert not result["equal"]
    assert {d["kind"] for d in result["divergences"]} & {
        "class_count_differs", "sequence_diverges", "length_differs"}


def test_cli_replay_and_diff(tmp_path, capsys):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    assert cli_main(["replay", "--store", str(path),
                     "--trace-id", "trace-spike-efadd3b5"]) == 0
    out = capsys.readouterr().out
    assert "DECISION" in out and "BLOCK" in out
    assert cli_main(["diff", "--store", str(path),
                     "--trace-a", "trace-spike-efadd3b5",
                     "--trace-b", "trace-spike-efadd3b5"]) == 0
    assert "behaviorally identical" in capsys.readouterr().out
