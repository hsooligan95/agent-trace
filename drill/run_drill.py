#!/usr/bin/env python3
"""AgentTrace flagship drill — one trace ID, two workloads, one platform.

WHAT THIS RUNS (honestly framed): several distinct workloads governed
by one platform -- a promotion gate (agent-canary), a screening
gateway (agent-gate), a remediation agent (blast-radius), a grounded
QA agent answering a question (factory-floor-agent), a
cost-instrumented ops agent (agent-ledger's sample agent), and a
failure-injection matrix (agent-chaos). They are not one agent and
this script does not pretend otherwise -- what is shared is the
platform around them: one trace ID, propagated by each producer at
runtime, and one tamper-evident store (AgentTrace) that records all
of them.

Beats:
  1. agent-canary compare v1 vs v2-bad under the trace ID -> BLOCK.
  2. agent-gate screens a poisoned tool result under the same ID.
  3. blast-radius failed-deployment under the same ID: plan -> the
     drill answers the interactive approval prompt as the operator
     (actor "agent-trace-drill via interactive CLI", recorded in the
     audit log) -> kill -9 after two checkpointed steps -> a fresh
     process resumes -> verification fails on node-3's persistent
     fault -> full rollback -> final cloud state proven identical to
     the initial snapshot.
  4. factory-floor-agent answers a question under the same ID; its
     emit-side run record (plan, retrieval, groundedness scores,
     outcome) lands in the same store, groundedness decision record
     included.
  5. agent-ledger's sample agent runs its baseline suite stamped with
     the same ID (root-span `agenttrace.trace_id`); its spans join
     this trace, so the drill's cost line is real ledger accounting.
  6. agent-chaos runs its scenario matrix under the same ID; its
     report is ingested (headline + every scenario row).
  7. Every producer artifact is ingested into one AgentTrace store;
     the chain verifies; a one-byte tamper is detected at the exact
     event; the one-page timeline renders from the store alone.

Run:  .venv/bin/python drill/run_drill.py
Outputs land in drill/output/<trace-id>/ (store.jsonl, timeline.md,
transcript artifacts). Local only; the "cloud" is a moto server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORTFOLIO = REPO.parent
sys.path.insert(0, str(REPO))

from agenttrace import ingest, store  # noqa: E402
from agenttrace.render import render_timeline  # noqa: E402

TRACE_ID = f"trace-drill-{uuid.uuid4().hex[:8]}"
OUT = REPO / "drill" / "output" / TRACE_ID
VENV_PY = REPO / ".venv" / "bin" / "python"
CANARY_PY = PORTFOLIO / "agent-canary" / ".venv" / "bin" / "python"
FF_PY = PORTFOLIO / "factory-floor-agent" / ".venv" / "bin" / "python"
LEDGER_PY = PORTFOLIO / "agent-ledger" / ".venv" / "bin" / "python"
CHAOS_PY = PORTFOLIO / "agent-chaos" / ".venv" / "bin" / "python"
BR_ROOT = PORTFOLIO / "blast-radius"
MOTO_PORT = 5521
MOTO_URL = f"http://127.0.0.1:{MOTO_PORT}"


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, env=None, cwd=None, expect=0):
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(cmd, env=full_env, cwd=cwd, capture_output=True, text=True)
    if expect is not None and proc.returncode != expect:
        say(proc.stdout[-3000:])
        say(proc.stderr[-3000:])
        raise SystemExit(f"command failed (exit {proc.returncode}, wanted {expect}): {cmd}")
    return proc


def br_env() -> dict:
    return {
        "BLASTRADIUS_AWS_ENDPOINT": MOTO_URL,
        "BLASTRADIUS_RUNS_DIR": str(OUT / "br-runs"),
        "PYTHONPATH": str(BR_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def br_cmd(*args: str) -> list[str]:
    return [str(VENV_PY), "-m", "blastradius.cli", "--actor", "agent-trace-drill", *args]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    say(f"=== AgentTrace flagship drill ===")
    say(f"trace ID: {TRACE_ID}")
    say("framing: several distinct workloads (promotion gate, screening gateway,")
    say("remediation agent, grounded QA agent, cost-instrumented ops agent, chaos")
    say("matrix), governed by one platform, recorded in one store.\n")

    # --- Beat 1: canary BLOCK ------------------------------------------------
    say("--- beat 1: agent-canary compare v1 vs v2-bad (expect BLOCK, exit 2)")
    verdict_path = OUT / "canary-verdict.json"
    run([str(CANARY_PY), "-m", "agentcanary", "compare",
         "--base", "v1", "--candidate", "v2-bad",
         "--out", str(OUT / "canary-report.md"),
         "--json-out", str(verdict_path)],
        env={"AGENTCANARY_RUN_ID": TRACE_ID, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=PORTFOLIO / "agent-canary", expect=2)
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["run_id"] == TRACE_ID and verdict["decision"] == "BLOCK"
    say(f"canary: decision=BLOCK run_id={verdict['run_id']}")
    for reason in verdict["reasons"]:
        say(f"  reason: {reason}")

    # --- Beat 2: gate screens a poisoned tool result -------------------------
    say("\n--- beat 2: agent-gate poisoned tool result (expect screening findings)")
    gate_state = OUT / "gate-state"
    run(["node", str(REPO / "drill" / "gate_driver.mjs"), TRACE_ID, str(gate_state)],
        cwd=PORTFOLIO / "agent-gate", expect=0)
    gate_entries = [json.loads(l) for l in
                    (gate_state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
                    if l.strip()]
    assert gate_entries and all(e["traceId"] == TRACE_ID for e in gate_entries)
    screening = next(e for e in gate_entries if e["type"] == "screening_findings")
    patterns = [f["pattern"] for f in screening["findings"]]
    say(f"gate: {len(gate_entries)} audit entries, all traceId={TRACE_ID}")
    say(f"gate: screening patterns caught: {', '.join(patterns)}")

    # --- Beat 3: blast-radius kill / resume / verify-fail / rollback ---------
    say("\n--- beat 3: blast-radius failed-deployment (approve -> kill -9 -> resume -> rollback)")
    moto = subprocess.Popen(
        [str(VENV_PY), "-m", "moto.server", "-H", "127.0.0.1", "-p", str(MOTO_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(MOTO_URL + "/moto-api/", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        env = br_env()
        run(br_cmd("seed", "--scenario", "failed-deployment"), env=env, expect=0)
        run(br_cmd("snapshot", "--out", str(OUT / "initial.json")), env=env, expect=0)

        # Start the run interactively; the drill (as operator) approves.
        proc = subprocess.Popen(
            br_cmd("run", "--scenario", "failed-deployment", "--run-id", TRACE_ID,
                   "--interactive", "--step-delay", "3"),
            env={**os.environ, **env}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=0)
        buffer: list[str] = []
        prompted = threading.Event()

        def drain() -> None:
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    return
                buffer.append(ch)
                if "Approve this plan?" in "".join(buffer[-64:]):
                    prompted.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        if not prompted.wait(timeout=120):
            proc.kill()
            raise SystemExit("blast-radius never reached the approval prompt")
        say("blast-radius: plan presented; drill answers the approval prompt as operator")
        proc.stdin.write("y\n")
        proc.stdin.flush()

        audit_path = OUT / "br-runs" / TRACE_ID / "audit.jsonl"
        deadline = time.time() + 120
        applied = 0
        while time.time() < deadline:
            if audit_path.exists():
                applied = audit_path.read_text(encoding="utf-8").count(
                    '"event": "step_applied"')
                if applied >= 2:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        assert applied >= 2, f"expected >=2 checkpointed steps before kill, saw {applied}"
        say(f"blast-radius: {applied} steps checkpointed; killing the agent: kill -9")
        proc.kill()  # SIGKILL
        proc.wait()
        reader.join(timeout=5)
        (OUT / "run1.log").write_text("".join(buffer), encoding="utf-8")

        status = run(br_cmd("status", "--run-id", TRACE_ID), env=env, expect=0)
        say("blast-radius: checkpoint a fresh process sees:")
        say("  " + status.stdout.strip().replace("\n", "\n  "))

        resumed = run(br_cmd("resume", "--run-id", TRACE_ID), env=env, expect=0)
        assert "rolled_back" in resumed.stdout, resumed.stdout
        say("blast-radius: resumed; verification failed on node-3's persistent fault;")
        say("  every executed step rolled back (run status: rolled_back)")

        run(br_cmd("snapshot", "--out", str(OUT / "final.json")), env=env, expect=0)
        run(br_cmd("compare", "--a", str(OUT / "initial.json"),
                   "--b", str(OUT / "final.json")), env=env, expect=0)
        say("blast-radius: initial vs final cloud snapshot: IDENTICAL")
    finally:
        moto.terminate()

    br_events = [json.loads(l) for l in
                 (OUT / "br-runs" / TRACE_ID / "audit.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert br_events and all(e["run_id"] == TRACE_ID for e in br_events)
    kinds = {e["event"] for e in br_events}
    assert {"plan_created", "approval_granted", "rollback_step", "run_finished"} <= kinds
    say(f"blast-radius: {len(br_events)} audit records, all run_id={TRACE_ID}")

    # --- Beat 4: factory-floor answers under the same trace ID ----------------
    say("\n--- beat 4: factory-floor-agent answers a question (expect grounded)")
    ff_record = OUT / "factory-floor-trace.jsonl"
    ff = run([str(FF_PY), "run_agent.py",
              "What is a state graph in LangGraph and how does state flow through it?"],
             env={"FACTORY_FLOOR_TRACE_ID": TRACE_ID,
                  "FACTORY_FLOOR_TRACE_OUT": str(ff_record),
                  "PYTHONDONTWRITEBYTECODE": "1"},
             cwd=PORTFOLIO / "factory-floor-agent", expect=0)
    assert "Result: grounded" in ff.stdout, ff.stdout[-1000:]
    ff_events = [json.loads(l) for l in
                 ff_record.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert ff_events and all(e["trace_id"] == TRACE_ID for e in ff_events)
    groundedness = next(e for e in ff_events if e["type"] == "groundedness")
    claims = groundedness["payload"]["claims"]
    say(f"factory-floor: {len(ff_events)} record events under traceId={TRACE_ID}")
    say(f"factory-floor: groundedness grounded={groundedness['payload']['grounded']} "
        f"claims={len(claims)} min_overlap={min(c['overlap'] for c in claims)} "
        f"threshold={groundedness['payload']['threshold']}")

    # --- Beat 5: ledger sample agent, stamped with the same trace ID ----------
    say("\n--- beat 5: agent-ledger sample agent baseline (expect spans joined by stamp)")
    run([str(LEDGER_PY), "-m", "sample_agent.run", "baseline",
         "--trace-id", TRACE_ID],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        cwd=PORTFOLIO / "agent-ledger", expect=0)
    # The sample agent writes its OTLP export into its own runs/ dir
    # (a regenerable artifact); the drill copies it out for ingestion.
    ledger_export = OUT / "ledger-otlp.json"
    ledger_export.write_text(
        (PORTFOLIO / "agent-ledger" / "runs" / "baseline-otlp.json")
        .read_text(encoding="utf-8"), encoding="utf-8")
    ledger_doc = json.loads(ledger_export.read_text(encoding="utf-8"))
    ledger_spans = [s for rs in ledger_doc["resourceSpans"]
                    for ss in rs["scopeSpans"] for s in ss["spans"]]
    stamped = [s for s in ledger_spans
               if any(a["key"] == "agenttrace.trace_id"
                      and a["value"].get("stringValue") == TRACE_ID
                      for a in s["attributes"])]
    assert len(stamped) == 1, f"expected exactly 1 stamped root span, saw {len(stamped)}"
    say(f"ledger: {len(ledger_spans)} spans exported; root span stamped "
        f"agenttrace.trace_id={TRACE_ID}")

    # --- Beat 6: chaos matrix under the same trace ID --------------------------
    say("\n--- beat 6: agent-chaos scenario matrix (expect silent failures 100% -> 0%)")
    run([str(CHAOS_PY), "-m", "agentchaos", "run",
         "--out", str(OUT / "chaos"), "--run-id", TRACE_ID],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        cwd=PORTFOLIO / "agent-chaos", expect=0)
    chaos_report = json.loads((OUT / "chaos" / "report.json").read_text(encoding="utf-8"))
    assert chaos_report["run_id"] == TRACE_ID
    headline = chaos_report["headline"]
    say(f"chaos: {len(chaos_report['rows'])} scenario rows under run_id={TRACE_ID}; "
        f"silent-failure rate off={headline['silent_failure_rate_defenses_off']:.0%} "
        f"on={headline['silent_failure_rate_defenses_on']:.0%}")

    # --- Beat 7: ingest into one AgentTrace store -----------------------------
    say("\n--- beat 7: ingest all producers into one AgentTrace store")
    store_path = OUT / "store.jsonl"
    events = []
    events += ingest.ingest_canary_verdict(verdict_path)
    events += ingest.ingest_gate_audit(gate_state / "audit.jsonl")
    events += ingest.ingest_blastradius_audit(OUT / "br-runs" / TRACE_ID / "audit.jsonl")
    events += ingest.ingest_factory_floor_trace(ff_record)
    events += ingest.ingest_ledger_otlp(ledger_export)
    events += ingest.ingest_chaos_report(OUT / "chaos" / "report.json")
    assert all(e["trace_id"] == TRACE_ID for e in events)
    sources = sorted({e["source"] for e in events})
    say(f"store sources: {', '.join(sources)}")
    store.append_events(store_path, events)
    result = store.verify(store_path)
    assert result.ok
    say(f"store: {result.count} events under one trace ID")
    say(f"Chain OK — {result.count} events, every hash verified.")

    # Tamper proof: flip one byte in a stored gate screening event.
    lines = store_path.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, l in enumerate(lines)
               if json.loads(l)["source"] == "agent-gate"
               and json.loads(l)["type"] == "screening_findings")
    needle = '"source": "agent-gate"'
    pos = lines[idx].index(needle) + len('"source": "agent-')
    tampered = lines.copy()
    tampered[idx] = lines[idx][:pos] + "X" + lines[idx][pos + 1:]
    tampered_path = OUT / "store.tampered.jsonl"
    tampered_path.write_text("\n".join(tampered) + "\n", encoding="utf-8")
    bad = store.verify(tampered_path)
    assert not bad.ok and bad.broken_seq == idx + 1
    say(f"tamper: flipped 1 byte in seq {idx + 1}; verify fails at exactly seq {bad.broken_seq}")

    # Cost line: REAL agent-ledger accounting, from the ledger span
    # events in this store — the sample ops agent's baseline run,
    # joined to this trace by the ID its root span declared. (Phase 1's
    # drill had to borrow canary's verdict costs and label them as
    # such; the ledger join is the phase-2 fix, and this line is it.)
    ledger_events = [e for e in events if e["source"] == "agent-ledger"]
    step_events = [e for e in ledger_events
                   if e["payload"]["attributes"].get("agentledger.kind") == "step"]
    total_cost = sum(float(e["payload"]["attributes"].get("agentledger.cost_usd", 0.0))
                     for e in step_events)
    say(f"cost line (from agent-ledger spans in this store, joined by the "
        f"propagated trace ID): {len(ledger_events)} spans · {len(step_events)} "
        f"billable steps · total ${total_cost:.5f} — the sample ops agent's "
        f"baseline run under this trace.")

    page = render_timeline(store.load_events(store_path), verify_result=result)
    (OUT / "timeline.md").write_text(page, encoding="utf-8")
    say(f"\ntimeline rendered from the store alone -> {OUT / 'timeline.md'}")
    say("\nDRILL COMPLETE — canary BLOCK, gate screening, approval, kill -9, resume,")
    say("verify-fail, rollback to identical state, factory-floor grounded answer,")
    say("ledger spans joined by propagated ID, chaos matrix ingested, one chained")
    say("store, tamper caught at the exact event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
