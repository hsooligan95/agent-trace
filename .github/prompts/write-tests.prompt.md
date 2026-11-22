---
mode: agent
description: Write tests for a named module in agent-trace
---

Write tests for the module I name, following this repo's existing test
style, layout, and fixtures. Cover the behavior the module documents,
including these invariants where they apply:

- Decision records come ONLY from computed decision points; an LLM self-explanation is at most a rationale_annotation, never a decision_record.
- The store is append-only and hash-chained: appends refuse to extend a broken chain, and the verifier names the exact broken seq.
- Events are schema v1; ingestion and verification fail loudly on unknown versions — never silently coerce or skip.

Then run the test suite:

    .venv/bin/python -m pytest

Show the result. Do not claim done unless the suite is green; if a
test cannot pass, say why instead of weakening the assertion.
