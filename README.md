# AgentTrace

**Every agent, every run, every decision, every tool call — one tamper-evident record, keyed by one trace ID that follows the work across agents, with the *why* captured next to the *what*.**

AgentTrace is the record layer for an agent fleet: a versioned event schema, one append-only **hash-chained** JSONL store, ingestors for each producer's existing machine-readable output, a verifier that proves the chain intact end-to-end (and names the exact event where it breaks), a query CLI, and a one-page tree/timeline rendered **from the store alone**.

It exists because of a measured defect: the portfolio's platform Critic found that *no component emitted a shared trace ID*, so no unified timeline was possible. AgentTrace is that missing spine — correlation is **propagated by the producers at runtime** (canary and gate accept an externally supplied ID via small emit-side additions; blast-radius already keyed its audit by run ID), never reconstructed by the recorder after the fact.

## Where it sits

| Component | Question it answers |
|---|---|
| agent-ledger | What did it cost? |
| agent-gate | What is allowed, enforced at one gateway? |
| **AgentTrace** | **What happened, who decided it, and can you prove the record wasn't altered?** |
| TraceReplay (concept) | A debugging workflow that will read this store |

LangSmith / Langfuse / Arize trace for *debugging*; none offers a hash-chained, tamper-evident record with decision records built to hand to an assessor, cross-framework and cross-language by construction, as a runnable component rather than a hosted platform. The claim is deliberately narrow — the audit axis only. This is not an observability platform and grows no dashboards (a read-only live view is specced as a conditioned phase 2).

## Decision records — fenced, on purpose

A decision record is trustworthy only if the code branch that made the decision emits it, at decision time, from the values that decided it. AgentTrace claims decision records **only for computed decision points**:

- **agent-canary compare verdicts** — PROMOTE/BLOCK with the success rates, forbidden-action counts, and reasons that computed it;
- **agent-gate policy decisions and screening findings** — allow/deny/require-approval and the injection/PII patterns the screeners matched.

The schema distinguishes `decision_record` from `rationale_annotation` (anything an LLM narrates about its own reasoning). No producer in this phase emits rationale annotations, and nothing here fabricates them.

## The flagship drill (real captured output)

One scripted run under one trace ID, `drill/run_drill.py`. **Honest framing, in one sentence: the drill runs two different workloads — a QA agent's promotion pipeline and a remediation agent's execution — governed by one platform (one trace ID, one audit spine, one store), and it does not pretend they are a single agent.**

```
trace ID: trace-drill-9bd6fa2c

--- beat 1: agent-canary compare v1 vs v2-bad (expect BLOCK, exit 2)
canary: decision=BLOCK run_id=trace-drill-9bd6fa2c
  reason: success rate dropped 100% -> 36% (drop 0.64 > allowed 0.05)
  reason: candidate committed 10 forbidden action(s)

--- beat 2: agent-gate poisoned tool result (expect screening findings)
gate: 5 audit entries, all traceId=trace-drill-9bd6fa2c
gate: screening patterns caught: ignore-instructions, export-all-data,
      secret-exfil-url, ssn, email, phone

--- beat 3: blast-radius failed-deployment (approve -> kill -9 -> resume -> rollback)
blast-radius: plan presented; drill answers the approval prompt as operator
blast-radius: 2 steps checkpointed; killing the agent: kill -9
blast-radius: checkpoint a fresh process sees:
  { "run_id": "trace-drill-9bd6fa2c", "status": "executing", "step_index": 2,
    "plan_steps": 6, "approved": true,
    "approved_by": "agent-trace-drill via interactive CLI", ... }
blast-radius: resumed; verification failed on node-3's persistent fault;
  every executed step rolled back (run status: rolled_back)
blast-radius: initial vs final cloud snapshot: IDENTICAL
blast-radius: 46 audit records, all run_id=trace-drill-9bd6fa2c

--- beat 4: ingest all producers into one AgentTrace store
store: 55 events under one trace ID
Chain OK — 55 events, every hash verified.
tamper: flipped 1 byte in seq 8; verify fails at exactly seq 8
cost line (from canary verdict events in this store): base run $0.04422 ·
  candidate run $0.03636 · delta -17.77% -- cheaper, and BLOCKED anyway:
  the gate is a behavior gate, not a cost gate.
```

The rendered timeline (from the store alone — the renderer never opens a producer file) shows the whole run as one tree: the canary BLOCK decision record, the gate's screening decision record, the kill/resume seam (step 3 starts twice — it was in flight when the kill landed and is replayed on resume, exactly the checkpoint-atomicity window blast-radius documents), the `verify_check … result=FAIL` on checkout-api-3, and the rollback steps restoring the initial state.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # 25 tests

# store operations over the bundled producer samples:
.venv/bin/python -m agenttrace.cli ingest --store /tmp/demo.jsonl \
    --canary samples/canary-verdict.json --gate samples/gate-audit.jsonl \
    --blastradius samples/blastradius-audit.jsonl --ledger samples/ledger-otlp.json
.venv/bin/python -m agenttrace.cli verify --store /tmp/demo.jsonl
.venv/bin/python -m agenttrace.cli query  --store /tmp/demo.jsonl --kind decision_record
.venv/bin/python -m agenttrace.cli render --store /tmp/demo.jsonl --out timeline.md
```

The drill additionally needs blast-radius's dependencies (it drives the real blast-radius CLI against a moto server) and agent-gate's built `dist/`:

```bash
.venv/bin/pip install -r ../blast-radius/requirements.txt
.venv/bin/python drill/run_drill.py   # outputs in drill/output/<trace-id>/
```

## The schema contract

Every event carries `schema_version` (currently `"1"`). Ingestion and verification **fail loudly** on an unknown version — an audit system that silently drops or misreads events manufactures false confidence. `tests/test_ingest.py` is a contract suite: each producer's real sample output (in `samples/`) must validate against the store schema, so a producer changing its output shape fails in CI instead of corrupting the record.

## Layout

```
agenttrace/
  schema.py     versioned event schema; loud failure on unknown versions
  store.py      append-only hash-chained JSONL store + verifier
  ingest.py     one ingestor per producer (canary, gate, blast-radius, ledger OTLP)
  decisions.py  decision-record extractors (computed decision points only)
  query.py      query layer (by trace ID / agent / source / type / kind)
  render.py     one-page tree/timeline from the store alone
  cli.py        ingest / verify / query / render
drill/          the flagship drill (run_drill.py + gate_driver.mjs)
samples/        real producer outputs used by the contract tests
tests/          schema, store/chain/tamper, ingest contracts, decisions, query/render
```

## Honest limitations

- **Mechanism, not practice.** The drill's agents run on stub/scripted models against a mock cloud. The trace demonstrates the capture, chaining, decision-record, and verification mechanism end-to-end; it does not prove the practice at production volume with real agents or real assessors, and "audit-grade" should not be read as claiming otherwise.
- **agent-ledger joining is phase 2.** The OTLP ingestor works and is contract-tested against a real ledger export, but ledger spans carry OTLP-native trace IDs, so a ledger run lands in the store under its own trace. Joining a ledger run to a drill trace needs emit-side run-ID plumbing in the ledger sample agent; the drill's cost line is therefore computed from the canary verdict events actually in the store, and labeled as such.
- **Three of six producers.** Canary, gate, and blast-radius emit into the store today. Chaos reports, factory-floor (which needs an emitter built from zero), and FlightSim volume are later phases.
- **The dashboard is not built.** A real-time visual view over this store is specced as phase 2 under hard conditions (read-only, store-only data, no platform features). Nothing in this repo renders live state yet.
- **No redaction at capture yet.** The concept calls for screening tool inputs/outputs on the way into the store (reusing gate's patterns). The demo fleet's stored payloads are producer audit records, which already carry excerpts rather than raw secrets — but a general redaction stage is phase 2, and a production deployment should not ingest raw tool payloads without it.

## License

MIT — see [LICENSE](LICENSE).
