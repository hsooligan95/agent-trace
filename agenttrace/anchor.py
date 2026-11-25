"""Signed chain heads and the external anchor log (Red Team F1 fix).

The hash chain in store.py proves internal consistency only: anyone
who can write the store can rewrite it wholesale, recompute every
hash, and produce a store that verifies "Chain OK" -- the chain is
anchored to nothing outside itself (Red Team Audit Run #1, F1).

This module adds the missing provenance:

* The chain head (trace ID + event count + head hash) is signed with
  HMAC-SHA256 under a key that lives OUTSIDE the trace data -- the
  AGENTTRACE_SIGNING_KEY environment variable (a repository secret in
  CI). Store writers never see the key, so a rewritten history cannot
  produce a valid signature for its new head.
* Signed heads are appended to an anchor log (ci/chain-anchors.jsonl
  in the nightly drill) that lives outside the store and is committed
  back by CI, so git history witnesses it. Each anchor also signs the
  previous anchor's signature, chaining the log itself: editing or
  deleting a past anchor breaks every later signature.
* Anchored verification FAILS -- it does not merely flag -- when the
  chain is broken, when the store's head matches no anchor, when an
  anchor's signature does not check out under the current key, or
  when the anchor log itself was tampered with.

Plain store.verify() is unchanged: existing histories keep verifying
exactly as before. Anchored verification is the strict mode the
nightly drill gates on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import store as store_mod

ENV_VAR = "AGENTTRACE_SIGNING_KEY"
MIN_KEY_CHARS = 16


@dataclass
class AnchorResult:
    ok: bool
    detail: str = ""
    anchors_checked: int = 0
    last_good_anchor: dict | None = None


def key_from_env() -> str:
    """Read and validate the signing key from the environment.

    The key is never stored in the repo, the store, or the anchor log;
    only a short fingerprint (key_id) is recorded, so a reader can
    tell WHICH key signed an anchor without learning anything about
    the key itself.
    """
    key = os.environ.get(ENV_VAR, "")
    if not key:
        raise ValueError(f"{ENV_VAR} is not set")
    if len(key) < MIN_KEY_CHARS:
        raise ValueError(
            f"{ENV_VAR} is too short ({len(key)} chars); "
            f"use at least {MIN_KEY_CHARS}")
    return key


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _sign(key: str, payload: dict) -> str:
    msg = store_mod.canonical(payload).encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def _signature_payload(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "signature"}


def head_of(path: str | Path) -> tuple[str, int, str]:
    """Return (trace_id, event_count, head_hash) for a store.

    Raises ValueError if the store's chain does not verify -- a head
    is only worth signing once the chain beneath it checks out.
    """
    result = store_mod.verify(path)
    if not result.ok:
        raise ValueError(
            f"refusing to take the head of a broken chain: "
            f"seq {result.broken_seq} ({result.detail})")
    events = store_mod.load_events(path)
    if not events:
        raise ValueError("empty store has no chain head")
    trace_ids = {e["trace_id"] for e in events}
    if len(trace_ids) != 1:
        raise ValueError(f"store holds multiple trace IDs: {sorted(trace_ids)}")
    return events[0]["trace_id"], len(events), events[-1]["hash"]


def load_anchor_entries(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def verify_anchor_log(path: str | Path, key: str) -> AnchorResult:
    """Verify every anchor signature and the log's internal chaining.

    Failure records (kind=anchor_verification_failure) carry no
    signature and are skipped here; count_failure_streak() reads them.
    """
    entries = load_anchor_entries(path)
    prev_sig = None
    last_good = None
    checked = 0
    for i, entry in enumerate(entries, start=1):
        if entry.get("kind") != "anchor":
            continue
        if entry.get("key_id") != key_id(key):
            return AnchorResult(
                False,
                f"anchor #{i} was signed under a different key "
                f"(key_id {entry.get('key_id')})",
                checked, last_good)
        if entry.get("prev_anchor_signature") != prev_sig:
            return AnchorResult(
                False,
                f"anchor #{i} does not chain to the previous anchor "
                f"(an earlier anchor was edited or deleted)",
                checked, last_good)
        expected = _sign(key, _signature_payload(entry))
        if not hmac.compare_digest(expected, entry.get("signature", "")):
            return AnchorResult(
                False, f"anchor #{i} signature mismatch "
                       f"(anchor record was altered)", checked, last_good)
        prev_sig = entry["signature"]
        last_good = entry
        checked += 1
    return AnchorResult(True, f"{checked} anchor(s) verified", checked, last_good)


def anchor_store(store_path: str | Path, anchors_path: str | Path,
                 key: str, *, environment: str = "") -> dict:
    """Sign the store's current head and append it to the anchor log.

    Refuses to anchor on top of a log that does not itself verify:
    extending a tampered log would launder the tampering.
    """
    log_result = verify_anchor_log(anchors_path, key)
    if not log_result.ok:
        raise ValueError(f"refusing to anchor: anchor log fails "
                         f"verification ({log_result.detail})")
    trace_id, count, head = head_of(store_path)
    entry = {
        "kind": "anchor",
        "version": 1,
        "trace_id": trace_id,
        "event_count": count,
        "head": head,
        "anchored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": environment,
        "prev_anchor_signature":
            log_result.last_good_anchor["signature"] if log_result.last_good_anchor else None,
        "key_id": key_id(key),
    }
    entry["signature"] = _sign(key, _signature_payload(entry))
    anchors_path = Path(anchors_path)
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    with anchors_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def append_failure_record(anchors_path: str | Path, detail: str,
                          last_good_anchor: dict | None) -> int:
    """Record an anchor-verification failure in the log and return the
    current streak of consecutive failures (reset by any anchor)."""
    record = {
        "kind": "anchor_verification_failure",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detail": detail,
        "last_good_anchor": last_good_anchor,
    }
    anchors_path = Path(anchors_path)
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    with anchors_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return count_failure_streak(anchors_path)


def count_failure_streak(anchors_path: str | Path) -> int:
    streak = 0
    for entry in load_anchor_entries(anchors_path):
        if entry.get("kind") == "anchor":
            streak = 0
        elif entry.get("kind") == "anchor_verification_failure":
            streak += 1
    return streak


def verify_against_anchors(store_path: str | Path,
                           anchors_path: str | Path,
                           key: str) -> AnchorResult:
    """Strict verification: chain + signed head vs the anchor log.

    Fails when the chain is broken, the anchor log does not verify,
    or the store's head matches no anchor. A store that extends an
    anchored prefix (same trace, anchored head intact at its recorded
    count, more events after) is reported as not-yet-anchored, never
    as verified.
    """
    result = store_mod.verify(store_path)
    if not result.ok:
        return AnchorResult(
            False, f"chain broken at seq {result.broken_seq}: {result.detail}")
    log_result = verify_anchor_log(anchors_path, key)
    if not log_result.ok:
        return AnchorResult(False, f"anchor log: {log_result.detail}",
                            log_result.anchors_checked,
                            log_result.last_good_anchor)
    trace_id, count, head = head_of(store_path)
    events = store_mod.load_events(store_path)
    for entry in load_anchor_entries(anchors_path):
        if entry.get("kind") != "anchor" or entry["trace_id"] != trace_id:
            continue
        if entry["head"] == head and entry["event_count"] == count:
            return AnchorResult(
                True,
                f"chain intact and head anchored "
                f"({count} events, head {head[:16]}..., "
                f"anchored {entry['anchored_at']})",
                log_result.anchors_checked, entry)
        if count > entry["event_count"] and \
                events[entry["event_count"] - 1]["hash"] == entry["head"]:
            return AnchorResult(
                False,
                f"store extends the anchored head by "
                f"{count - entry['event_count']} event(s); the extension "
                f"is not anchored yet",
                log_result.anchors_checked, entry)
    return AnchorResult(
        False,
        f"no anchor matches this store's head "
        f"(trace {trace_id}, {count} events, head {head[:16]}...): "
        f"the history was rewritten or never anchored",
        log_result.anchors_checked, log_result.last_good_anchor)
