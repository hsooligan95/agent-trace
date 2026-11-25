"""Plug-and-play kit: drop-in emitter, generic drop ingest, wrapper.

These are the integration paths a project with NO AgentTrace wiring
uses -- the tests prove each one end-to-end against a real store and
require the chain to verify after every path.
"""

import json
import subprocess
import sys
from pathlib import Path

from agenttrace import emit as emit_mod
from agenttrace import ingest, store


def test_emit_drop_mode_and_ingest(tmp_path, monkeypatch):
    drop = tmp_path / "drop.jsonl"
    monkeypatch.setenv("AGENTTRACE_DROP", str(drop))
    monkeypatch.delenv("AGENTTRACE_STORE", raising=False)
    monkeypatch.setenv("AGENTTRACE_TRACE_ID", "trace-test-1")
    monkeypatch.setenv("AGENTTRACE_SOURCE", "music-maker")
    assert emit_mod.emit("score_computed", {"score": 0.9}) is True
    events = ingest.ingest_drop(drop)
    assert len(events) == 1
    assert events[0]["trace_id"] == "trace-test-1"
    assert events[0]["source"] == "music-maker"
    assert events[0]["payload"] == {"score": 0.9}
    target = tmp_path / "store.jsonl"
    store.append_events(target, events)
    assert store.verify(target).ok


def test_emit_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTTRACE_STORE", raising=False)
    monkeypatch.delenv("AGENTTRACE_DROP", raising=False)
    assert emit_mod.emit("anything", {}) is False


def test_ingest_drop_loose_lines_get_defaults(tmp_path):
    drop = tmp_path / "loose.jsonl"
    drop.write_text(
        json.dumps({"type": "note", "text": "hello"}) + "\n"
        + json.dumps({"type": "decision_record", "kind": "decision_record",
                      "payload": {"choice": "ship"}, "extra": 1}) + "\n",
        encoding="utf-8")
    events = ingest.ingest_drop(drop, trace_id="trace-loose",
                                source="music-maker")
    assert [e["trace_id"] for e in events] == ["trace-loose"] * 2
    assert events[0]["payload"] == {"text": "hello"}
    assert events[1]["payload"] == {"choice": "ship", "extra": 1}
    assert events[1]["kind"] == "decision_record"


def test_wrapper_runs_any_command_as_traced_run(tmp_path):
    target = tmp_path / "store.jsonl"
    child = (
        "import json, os;"
        "line = json.dumps({'type': 'child_event', 'payload': {'ok': True}});"
        "open(os.environ['AGENTTRACE_DROP'], 'a').write(line + '\\n');"
        "print('child ran')"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "agenttrace.run",
         "--store", str(target), "--source", "music-maker",
         "--capture-output", "--",
         sys.executable, "-c", child],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent)
    assert proc.returncode == 0, proc.stderr
    assert "trace_id=trace-music-maker-" in proc.stdout
    assert "chain_ok=True" in proc.stdout
    result = store.verify(target)
    assert result.ok
    types = [e["type"] for e in store.load_events(target)]
    assert types[0] == "run_started"
    assert "child_event" in types
    assert "stdout_line" in types
    assert types[-1] == "run_finished"
    finished = store.load_events(target)[-1]
    assert finished["payload"]["exit_code"] == 0
    assert finished["payload"]["duration_s"] >= 0


def test_wrapper_propagates_child_exit_code(tmp_path):
    target = tmp_path / "store.jsonl"
    proc = subprocess.run(
        [sys.executable, "-m", "agenttrace.run",
         "--store", str(target), "--source", "demo", "--",
         sys.executable, "-c", "raise SystemExit(3)"],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent)
    assert proc.returncode == 3
    assert store.verify(target).ok
