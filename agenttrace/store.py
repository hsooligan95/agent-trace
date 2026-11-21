"""The AgentTrace store: one append-only, hash-chained JSONL file.

Each stored event carries ``seq`` (1-based position), ``prev_hash`` (the
previous event's hash, or the genesis value), and ``hash`` (SHA-256 over
the canonical JSON of the event without its ``hash`` field). Rewriting,
deleting, or reordering any historical event breaks the chain at that
link, and the verifier names the exact ``seq`` where it breaks.

The store is deliberately just a file: no server, no account. That is
the portfolio contract -- a reviewer can run the verifier in seconds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .redact import redact_events
from .schema import validate_event

GENESIS = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def event_hash(event_without_hash: dict) -> str:
    return hashlib.sha256(canonical(event_without_hash).encode()).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    count: int
    broken_seq: int | None = None
    detail: str = ""


def load_events(path: str | Path) -> list[dict]:
    """Load and schema-validate every event in a store file.

    Raises UnknownSchemaVersionError if any event carries a schema
    version this build does not support -- loudly, never silently.
    """
    path = Path(path)
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(validate_event(json.loads(line), stored=True))
    return events


def verify(path: str | Path) -> VerifyResult:
    """Verify the hash chain end-to-end.

    Returns ok=True only if every event's seq, prev_hash linkage, and
    recomputed hash check out. On failure, ``broken_seq`` is the exact
    stored position where the chain breaks. Raises
    UnknownSchemaVersionError on an unsupported schema version.
    """
    path = Path(path)
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    prev = GENESIS
    for i, line in enumerate(lines, start=1):
        entry = json.loads(line)
        # Schema gate first: an unknown version must fail loudly even here.
        validate_event(entry, stored=True)
        if entry["seq"] != i:
            return VerifyResult(False, len(lines), entry.get("seq", i),
                                f"seq discontinuity: position {i} holds seq {entry['seq']}")
        if entry["prev_hash"] != prev:
            return VerifyResult(False, len(lines), entry["seq"],
                                "prev_hash does not match the previous event's hash")
        body = {k: v for k, v in entry.items() if k != "hash"}
        if event_hash(body) != entry["hash"]:
            return VerifyResult(False, len(lines), entry["seq"],
                                "hash mismatch: stored event content was altered")
        prev = entry["hash"]
    return VerifyResult(True, len(lines), None, "chain intact")


def append_events(path: str | Path, events: list[dict]) -> int:
    """Validate and append pre-store events, extending the chain.

    The existing store (if any) is verified first: appending to a broken
    chain would launder tampered history into a longer chain, so a
    broken existing store is refused. Returns the number appended.
    """
    path = Path(path)
    prev = GENESIS
    start_seq = 1
    if path.exists() and path.read_text(encoding="utf-8").strip():
        result = verify(path)
        if not result.ok:
            raise ValueError(
                f"refusing to append: existing store fails verification "
                f"at seq {result.broken_seq} ({result.detail})"
            )
        existing = load_events(path)
        prev = existing[-1]["hash"]
        start_seq = len(existing) + 1
    # Redaction at capture: scrub secret/PII patterns from payloads
    # before anything is hashed or written, so the store never holds
    # the raw values (see redact.py). Idempotent -- ingestors already
    # applied it; this pass guarantees no caller bypasses it.
    events = redact_events(events)
    for event in events:
        validate_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for offset, event in enumerate(events):
            entry = {**event, "seq": start_seq + offset, "prev_hash": prev}
            entry["hash"] = event_hash(entry)
            validate_event(entry, stored=True)
            prev = entry["hash"]
            fh.write(json.dumps(entry) + "\n")
    return len(events)
