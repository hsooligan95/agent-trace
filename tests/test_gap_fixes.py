"""The top-3 kit gaps found by real refactor use (2026-11-25):

1. Multi-step work under ONE trace through the shipped entry points
   (the wrapper must accept and forward an explicit trace ID).
2. ``ingest --drop`` must be idempotent: re-ingesting a drop file
   adds zero events; a partially-new file adds only its new lines.
3. Generic decision records (emit / file-drop shape: choice + the
   deciding values loose in the payload, no bespoke decision_point /
   decider / evidence structure) are first-class: the query layer
   renders them with a real decision point, decider, and evidence
   summary -- never ``None`` where the data exists -- and the
   dashboard's decision list carries them like the built-ins'.
"""

import json
import subprocess
import sys
from pathlib import Path

from agenttrace import ingest, store
from agenttrace.query import decision_records, summarize_event

ROOT = Path(__file__).resolve().parent.parent


def _run_wrapper(store_path, trace_id, marker):
    child = (
        "import json, os;"
        f"line = json.dumps({{'type': 'step', 'payload': {{'marker': '{marker}'}}}});"
        "open(os.environ['AGENTTRACE_DROP'], 'a').write(line + '\\n')"
    )
    return subprocess.run(
        [sys.executable, "-m", "agenttrace.run",
         "--store", str(store_path), "--source", "multi-step",
         "--trace-id", trace_id, "--",
         sys.executable, "-c", child],
        capture_output=True, text=True, cwd=ROOT)


def test_two_wrapper_invocations_share_one_trace(tmp_path):
    target = tmp_path / "store.jsonl"
    first = _run_wrapper(target, "trace-job-1", "one")
    assert first.returncode == 0, first.stderr
    assert "trace_id=trace-job-1" in first.stdout
    second = _run_wrapper(target, "trace-job-1", "two")
    assert second.returncode == 0, second.stderr
    assert store.verify(target).ok
    events = store.load_events(target)
    assert {e["trace_id"] for e in events} == {"trace-job-1"}
    types = [e["type"] for e in events]
    assert types.count("run_started") == 2
    assert types.count("run_finished") == 2
    markers = [e["payload"].get("marker") for e in events
               if e["type"] == "step"]
    assert markers == ["one", "two"]


def _drop_file(tmp_path, lines, name="drop.jsonl"):
    drop = tmp_path / name
    drop.write_text("".join(json.dumps(l) + "\n" for l in lines),
                    encoding="utf-8")
    return drop


_STEP = {"type": "writer_converted",
         "payload": {"writer": "queue_job.write_job"}}
_VERDICT = {"type": "refactor_verdict", "kind": "decision_record",
            "payload": {"choice": "KEEP", "criterion": "no new failures"}}


def test_drop_reingest_adds_zero_events(tmp_path):
    target = tmp_path / "store.jsonl"
    drop = _drop_file(tmp_path, [_STEP, _VERDICT])
    added, skipped = ingest.ingest_drop_into_store(
        target, drop, trace_id="trace-refactor", source="refactor")
    assert (added, skipped) == (2, 0)
    added, skipped = ingest.ingest_drop_into_store(
        target, drop, trace_id="trace-refactor", source="refactor")
    assert (added, skipped) == (0, 2)
    assert len(store.load_events(target)) == 2
    assert store.verify(target).ok


def test_drop_partial_overlap_adds_only_new_lines(tmp_path):
    target = tmp_path / "store.jsonl"
    drop = _drop_file(tmp_path, [_STEP, _VERDICT])
    ingest.ingest_drop_into_store(target, drop, trace_id="trace-r",
                                  source="refactor")
    bigger = _drop_file(tmp_path, [
        _STEP, _VERDICT,
        {"type": "tests_run", "payload": {"suite": "atomic", "ok": True}},
    ], name="drop2.jsonl")
    added, skipped = ingest.ingest_drop_into_store(
        target, bigger, trace_id="trace-r", source="refactor")
    assert (added, skipped) == (1, 2)
    assert len(store.load_events(target)) == 3
    assert store.verify(target).ok


def test_identical_lines_within_one_file_both_count(tmp_path):
    target = tmp_path / "store.jsonl"
    drop = _drop_file(tmp_path, [_STEP, _STEP])
    added, skipped = ingest.ingest_drop_into_store(
        target, drop, trace_id="trace-r", source="refactor")
    assert (added, skipped) == (2, 0)
    # ...but re-ingesting that same file still adds nothing.
    added, skipped = ingest.ingest_drop_into_store(
        target, drop, trace_id="trace-r", source="refactor")
    assert (added, skipped) == (0, 2)
    assert store.verify(target).ok


def test_drop_events_carry_stable_identity(tmp_path):
    drop = _drop_file(tmp_path, [_STEP])
    first = ingest.ingest_drop(drop, trace_id="trace-r", source="refactor")
    second = ingest.ingest_drop(drop, trace_id="trace-r", source="refactor")
    assert first[0]["drop_id"] == second[0]["drop_id"]


_GENERIC_SCORE = {
    "schema_version": "1", "trace_id": "trace-song-1",
    "ts": "2026-11-25T00:00:00+00:00", "source": "music-maker",
    "agent": "music-maker", "type": "score_computed",
    "kind": "decision_record", "seq": 1, "prev_hash": "x", "hash": "y",
    "payload": {"choice": "KEEP", "quality_score": 0.9994,
                "quality_threshold": 0.7, "relative_score": 55.87,
                "gate_reasons": [], "human_review_required": True},
}


def test_generic_decision_summary_has_no_nones():
    summary = summarize_event(_GENERIC_SCORE)
    assert "None" not in summary
    assert summary.startswith("DECISION score_computed: KEEP")
    assert "decider=music-maker" in summary
    assert "quality_score=0.9994" in summary


def test_generic_decision_record_card_is_first_class():
    (card,) = decision_records([_GENERIC_SCORE], "trace-song-1")
    assert card["decision_point"] == "score_computed"
    assert card["choice"] == "KEEP"
    assert card["decider"] == "music-maker"
    assert card["evidence"]["quality_score"] == 0.9994
    assert card["evidence"]["quality_threshold"] == 0.7


def test_builtin_decision_summary_unchanged():
    builtin = {
        "schema_version": "1", "trace_id": "trace-drill",
        "ts": "2026-11-25T00:00:00+00:00", "source": "agent-canary",
        "agent": "agent-canary", "type": "decision_record",
        "kind": "decision_record", "seq": 1, "prev_hash": "x", "hash": "y",
        "payload": {"decision_point": "agent-canary.compare",
                    "decider": "agent-canary", "computed": True,
                    "choice": "BLOCK", "options": ["PROMOTE", "BLOCK"],
                    "evidence": {"reasons": ["drop"]}},
    }
    assert summarize_event(builtin) == (
        "DECISION agent-canary.compare: BLOCK "
        "(decider=agent-canary, computed=True)")
    (card,) = decision_records([builtin], "trace-drill")
    assert card["decision_point"] == "agent-canary.compare"
    assert card["decider"] == "agent-canary"
    assert card["computed"] is True
    assert card["evidence"] == {"reasons": ["drop"]}
