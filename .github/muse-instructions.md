# Copilot instructions — agent-trace

You are working in **AgentTrace**, the tamper-evident record layer for an
agent fleet: a versioned event schema, one append-only hash-chained JSONL
store, per-producer ingestors, a chain verifier, a query CLI, TraceReplay,
and a read-only live dashboard rendered from the store alone.

## Stack

- Python 3, standard-library first; package `agenttrace/` (store, ingest,
  query, replay, redact, cli), dashboard in `dashboard/`, drills in `drill/`.
- A bounded Rust accent lives in `rust/agenttrace-verify/` — a standalone
  chain-verifier CLI. The Python verifier stays primary.

## Tests

```bash
.venv/bin/python -m pytest   # 60 tests; the Rust agreement test runs when cargo is on PATH
```

## Architecture notes

- Events are schema v1 (`schema_version: "1"`). Ingestion and verification
  fail loudly on unknown versions — never silently coerce or skip.
- The store is append-only and SHA-256 hash-chained. Appends refuse to
  extend a broken chain, and the verifier names the exact broken `seq`.
- Decision records come ONLY from computed decision points (canary
  verdicts, gate policy/screening outcomes, groundedness results). Never
  fabricate rationale: an LLM self-explanation, if stored at all, is
  labeled `rationale_annotation`, never `decision_record`.
- Correlation is propagated by producers at runtime under one trace ID —
  never reconstructed after the fact. Producer-side changes are emit-side
  only; never change a producer's logic to serve the recorder.
- The dashboard is read-only: it displays pending approvals, it never
  resolves them. All dashboard data flows through `agenttrace/query.py`
  from the store — no side channels.
- Redaction happens at capture/ingest; a redacted value never reaches the
  store, and the stored event records which pattern classes fired.

## Shared rules

- Use Unix LF line endings in every file; never CRLF.
- Never commit secrets, tokens, or credentials — real or synthetic-looking.
- Tests must pass before you describe a change as complete; run the test
  command above and report honestly if you cannot.
- Prefer the smallest complete implementation: reuse what already exists in
  this repo before adding new code, files, or dependencies.
