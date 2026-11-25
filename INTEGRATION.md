# Integrating AgentTrace into an existing project — 3 steps

AgentTrace is a record layer, not a framework: it never sits in your
project's execution path, and every integration below is additive.
Pick ONE step — they go from zero code to richer records, and you can
stop at the first one that gives you what you need.

**The contract, in one paragraph:** a run is identified by one trace
ID. Configuration is environment variables only — `AGENTTRACE_TRACE_ID`
(which run), `AGENTTRACE_SOURCE` (which project), `AGENTTRACE_STORE`
(path of the hash-chained store JSONL) or `AGENTTRACE_DROP` (path of a
plain JSONL drop file that gets chained into the store at ingest).
Events are JSON objects with `schema_version, trace_id, ts, source,
agent, type, kind, payload`. Everything else — chaining, redaction of
secrets/PII at capture, verification, query, replay, the dashboard —
is AgentTrace's job, not yours.

## Step 1 — Zero code: wrap the command you already run

```bash
python -m agenttrace.run --store <store.jsonl> --source music-maker \
    [--capture-output] [--ingest-jsonl <existing-log-or-rollout.jsonl>] \
    -- <any command you already run>
```

The wrapper mints the trace ID, sets the env vars above for the child
process, records `run_started` / `run_finished` (exit code + duration)
in the store, ingests the child's drop file and any `--ingest-jsonl`
files, verifies the chain, prints the trace ID, and exits with the
child's own exit code. The wrapped project is not modified in any way.

**Multi-step work under one trace:** pass `--trace-id <id>` (and the
same `--store`) on every invocation. Each run appends its own
`run_started` / `run_finished` pair — and everything its child emits
— to that same trace, so a build → test → deploy pipeline is one
trace with three runs, not three unrelated traces:

```bash
ID=trace-release-2026-11-25
python -m agenttrace.run --store s.jsonl --source ci --trace-id $ID -- make build
python -m agenttrace.run --store s.jsonl --source ci --trace-id $ID -- make test
python -m agenttrace.run --store s.jsonl --source ci --trace-id $ID -- make deploy
```

Runs under one trace are sequential: the per-trace drop file is
shared, so don't run two invocations with the same trace ID at the
same time.

## Step 2 — File drop: append JSON lines from any language

For per-step detail, the project (or a shell wrapper around it)
appends one JSON object per line to `$AGENTTRACE_DROP`:

```json
{"type": "score_computed", "payload": {"score": 0.91, "verdict": "KEEP"}}
{"type": "decision_record", "kind": "decision_record", "payload": {"choice": "ship"}}
```

Loose lines are fine: missing `trace_id` / `source` / `ts` / `agent`
are filled at ingest (from `--trace-id` / `--source`, then defaults),
and unrecognized keys are preserved inside `payload`, never dropped.
Full pre-store events (all core fields) are used exactly as written.
Import with:

```bash
python -m agenttrace.cli ingest --store <store.jsonl> \
    --drop <file.jsonl> --trace-id <id> --source <name>
```

**Drop ingest is idempotent.** Every drop-ingested event carries a
`drop_id`: a hash of its source line, the line's index in the file,
and the trace it resolves to. Re-ingesting the same file adds zero
events (the CLI reports how many were skipped); ingesting a file
that grew adds only the new lines. Two identical lines in one file
still count as two events — their indexes differ. Treat drop files
as append-only: editing a line in place changes its identity.

This is the non-Python path: a Node/TS project needs nothing but the
ability to append a line to a file — no AgentTrace code runs in it.

## Step 3 — Drop-in emitter (Python, one file)

Copy `agenttrace/emit.py` into the project (single file, stdlib only)
or import it from the installed package, then:

```python
from agenttrace.emit import emit
emit("score_computed", {"score": 0.91, "verdict": "KEEP"})
```

With `AGENTTRACE_STORE` set and the package importable, events are
chained into the store immediately; standalone copies write the drop
file instead (Step 2's ingest chains them). With neither variable
set, `emit()` is a no-op — instrumented code behaves identically
inside and outside a traced run.

## Worked example — music-maker (verified 2026-11-25, Andrew's laptop)

No music-maker code was changed. Its existing analysis CLI was run
through the Step 1 wrapper against the dashboard's live store:

```bash
cd ~/projects/music-maker
PYTHONPATH=~/agenttrace-demo/agent-trace \
  ~/agenttrace-demo/agent-trace/.venv/bin/python -m agenttrace.run \
  --store ~/agenttrace-demo/agent-trace/dashboard/live-store.jsonl \
  --source music-maker --capture-output \
  -- python3 scripts/score_audio.py \
       output/final/poppy-kpop-summer-corrected-001_00001.mp3
```

Result, read back from the store and the dashboard API:

- trace ID `trace-music-maker-95087f2a`, exit code 0, duration 1.164 s
- 65 music-maker events appended (run_started + the score report's
  stdout lines + run_finished) on top of the drill's 117
- store total 182 events — `Chain OK — 182 events, every hash verified`
- dashboard `/api/state`: traces `[trace-drill-64898b97,
  trace-music-maker-95087f2a]`, active trace = the music-maker run

## Verify / view any integrated project

```bash
python -m agenttrace.cli verify --store <store.jsonl>
python -m agenttrace.cli query  --store <store.jsonl> --source music-maker
python -m agenttrace.cli render --store <store.jsonl>
python -m dashboard.server --store <store.jsonl>   # read-only, :8765
```

## Known friction (honest)

- The package is used from a checkout in the demo above
  (`PYTHONPATH=<agent-trace checkout>`); there is no published
  pip install yet.
- The wrapper captures stdout only with `--capture-output`, and caps
  it at 500 lines; stderr is inherited, not recorded.
- Drop-file events written concurrently by several processes of one
  project are ingested in file order, not true time order.
- Two dashboard/incident tests read generated fixture stores under
  `drill/output/` and `dashboard/live-store.jsonl` (gitignored), so a
  fresh clone's suite is green only after a drill + demo run have
  generated them — on the laptop, the suite went 124 passed / 3
  skipped once the fixtures existed.
