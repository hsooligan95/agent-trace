#!/usr/bin/env python3
"""Build the nightly-drill status files from one completed drill run.

Reads the drill output directory produced by drill/run_drill.py
(store.jsonl + timeline.md), then INDEPENDENTLY re-verifies the store:
the drill asserts the chain as it goes, but this script loads the
finished store cold, re-runs the hash-chain verifier, re-runs the
one-byte tamper probe on a throwaway copy, and recomputes the cost
line from the agent-ledger span events -- the same accounting the
drill prints, recomputed from the record rather than copied from
stdout. If the chain does not verify, this script exits non-zero and
writes nothing: a red nightly run must never publish a green status.

Outputs (into --ci-dir, committed back to this repo by the workflow):
  last-drill.json          machine-readable status (see fields below)
  last-drill-timeline.md   copy of the drill's rendered timeline
  chain-anchors.jsonl      append-only log of signed chain heads
                           (only when AGENTTRACE_SIGNING_KEY is set --
                           see agenttrace/anchor.py; the F1 fix)

Anchoring: with the signing key present, the finished store's chain
head is signed and appended to the anchor log, after the existing
log's signatures are re-verified. If the log does not verify (a past
anchor was altered, or the key changed), NO new head is anchored, a
failure record is appended to the log instead, and this script exits
non-zero with the failing detail and the last good anchor -- a
rewritten history must turn the nightly run red, never publish a
green status over it.

Run:  .venv/bin/python ci/make_status.py --out-dir drill/output/<trace-id>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agenttrace import anchor, store  # noqa: E402


def tamper_probe(store_path: Path, tmp_path: Path) -> dict:
    """Flip one byte in a stored gate screening event (the drill's own
    probe, repeated against the finished store) and confirm the
    verifier names exactly that seq. Returns a small report dict."""
    lines = store_path.read_text(encoding="utf-8").splitlines()
    idx = next(
        (i for i, line in enumerate(lines)
         if json.loads(line)["source"] == "agent-gate"
         and json.loads(line)["type"] == "screening_findings"),
        None,
    )
    if idx is None:
        return {"ran": False, "reason": "no agent-gate screening event in store"}
    needle = '"source": "agent-gate"'
    pos = lines[idx].index(needle) + len('"source": "agent-')
    tampered = lines.copy()
    tampered[idx] = lines[idx][:pos] + "X" + lines[idx][pos + 1:]
    tmp_path.write_text("\n".join(tampered) + "\n", encoding="utf-8")
    bad = store.verify(tmp_path)
    return {
        "ran": True,
        "flipped_seq": idx + 1,
        "detected": (not bad.ok) and bad.broken_seq == idx + 1,
        "broken_seq": bad.broken_seq,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True,
                    help="drill output dir containing store.jsonl + timeline.md")
    ap.add_argument("--ci-dir", default=str(REPO / "ci"),
                    help="where last-drill.json / last-drill-timeline.md land")
    ap.add_argument("--environment", default="local",
                    help="where this run executed (github-actions / local-simulation / ...)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    ci_dir = Path(args.ci_dir)
    store_path = out_dir / "store.jsonl"
    timeline_path = out_dir / "timeline.md"
    if not store_path.exists() or not timeline_path.exists():
        print(f"missing store.jsonl or timeline.md in {out_dir}", file=sys.stderr)
        return 2

    result = store.verify(store_path)
    if not result.ok:
        print(f"CHAIN BROKEN at seq {result.broken_seq}: {result.detail}",
              file=sys.stderr)
        return 1

    events = store.load_events(store_path)
    trace_ids = sorted({e["trace_id"] for e in events})
    assert len(trace_ids) == 1, f"expected one trace ID, saw {trace_ids}"
    sources = Counter(e["source"] for e in events)
    decisions = [
        {"source": e["source"], "choice": e["payload"].get("choice")}
        for e in events if e["type"] == "decision_record"
    ]

    # Cost line: real agent-ledger accounting recomputed from the
    # ledger span events in this store (same rule as the drill).
    ledger_events = [e for e in events if e["source"] == "agent-ledger"]
    step_events = [e for e in ledger_events
                   if e["payload"]["attributes"].get("agentledger.kind") == "step"]
    total_cost = sum(float(e["payload"]["attributes"].get("agentledger.cost_usd", 0.0))
                     for e in step_events)

    probe = tamper_probe(store_path, out_dir / "store.status-probe.jsonl")
    if probe["ran"] and not probe["detected"]:
        print("tamper probe FAILED: one-byte flip not detected at its seq",
              file=sys.stderr)
        return 1

    # F1 anchoring: sign this run's chain head into the external
    # anchor log -- but only on top of a log that itself verifies.
    anchors_path = ci_dir / "chain-anchors.jsonl"
    signing_key = os.environ.get(anchor.ENV_VAR, "")
    if signing_key:
        try:
            anchor.key_from_env()
        except ValueError as exc:
            print(f"ANCHOR FAILED: {exc}", file=sys.stderr)
            return 1
        log_result = anchor.verify_anchor_log(anchors_path, signing_key)
        if not log_result.ok:
            streak = anchor.append_failure_record(
                anchors_path, log_result.detail, log_result.last_good_anchor)
            print(f"ANCHOR VERIFICATION FAILED: {log_result.detail}",
                  file=sys.stderr)
            print(f"last good anchor: "
                  f"{json.dumps(log_result.last_good_anchor)}",
                  file=sys.stderr)
            print(f"consecutive anchor failures: {streak} "
                  f"(no new head anchored)", file=sys.stderr)
            return 1
        entry = anchor.anchor_store(store_path, anchors_path, signing_key,
                                    environment=args.environment)
        chain_anchor = {
            "anchored": True,
            "trace_id": entry["trace_id"],
            "event_count": entry["event_count"],
            "head": entry["head"],
            "signature": entry["signature"],
            "key_id": entry["key_id"],
            "anchored_at": entry["anchored_at"],
            "anchors_total": anchor.verify_anchor_log(
                anchors_path, signing_key).anchors_checked,
        }
    else:
        chain_anchor = {
            "anchored": False,
            "reason": f"{anchor.ENV_VAR} is not set in this environment; "
                      f"chain head not signed",
        }

    now = datetime.now(timezone.utc)
    status = {
        "trace_id": trace_ids[0],
        "drill_date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "environment": args.environment,
        "event_count": result.count,
        "chain_ok": result.ok,
        "chain_verdict": f"Chain OK — {result.count} events, every hash verified.",
        "sources": dict(sorted(sources.items())),
        "decision_records": decisions,
        "cost": {
            "ledger_spans": len(ledger_events),
            "billable_steps": len(step_events),
            "total_usd": round(total_cost, 5),
            "line": (f"{len(ledger_events)} spans · {len(step_events)} billable "
                     f"steps · total ${total_cost:.5f}"),
        },
        "tamper_probe": probe,
        "chain_anchor": chain_anchor,
    }

    ci_dir.mkdir(parents=True, exist_ok=True)
    (ci_dir / "last-drill.json").write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(timeline_path, ci_dir / "last-drill-timeline.md")
    print(json.dumps(status, indent=2))
    print(f"\nwrote {ci_dir / 'last-drill.json'}")
    print(f"wrote {ci_dir / 'last-drill-timeline.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
