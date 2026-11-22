---
mode: agent
description: Review the current diff against agent-trace's invariants
---

Review my current diff against this repo's invariants:

- Decision records come ONLY from computed decision points; an LLM self-explanation is at most a rationale_annotation, never a decision_record.
- The store is append-only and hash-chained: appends refuse to extend a broken chain, and the verifier names the exact broken seq.
- Events are schema v1; ingestion and verification fail loudly on unknown versions — never silently coerce or skip.
- The dashboard is read-only and reads only through agenttrace/query.py from the store — no side channels, never resolves approvals.

Flag every violation with file and line. Also flag any change that
alters a documented behavior contract (output shapes, exit codes,
event schemas, recorded artifacts) without updating the docs and
tests that describe it. No style nitpicks — correctness and contract
violations only.
