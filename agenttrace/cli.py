"""AgentTrace CLI: ingest / verify / query / render.

Exit codes: 0 = success (for verify: chain intact); 1 = verification
failed (broken at the reported seq); 2 = usage or schema error --
including an unknown schema version, which fails loudly by design.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ingest as ingest_mod
from . import store as store_mod
from .query import query_events, summarize_event
from .render import render_timeline
from .schema import SchemaError

INGESTORS = {
    "canary": ("agent-canary verdict JSON", ingest_mod.ingest_canary_verdict),
    "gate": ("agent-gate audit JSONL", ingest_mod.ingest_gate_audit),
    "blastradius": ("blast-radius audit JSONL", ingest_mod.ingest_blastradius_audit),
    "ledger": ("agent-ledger OTLP JSON", ingest_mod.ingest_ledger_otlp),
}


def cmd_ingest(args) -> int:
    total = 0
    for kind, path in vars(args).items():
        if kind in INGESTORS and path:
            events = INGESTORS[kind][1](path)
            store_mod.append_events(args.store, events)
            print(f"ingested {len(events)} events from {INGESTORS[kind][0]} ({path})")
            total += len(events)
    if not total:
        print("nothing to ingest: pass at least one producer artifact", file=sys.stderr)
        return 2
    result = store_mod.verify(args.store)
    print(f"store now holds {result.count} events; chain ok={result.ok}")
    return 0 if result.ok else 1


def cmd_verify(args) -> int:
    result = store_mod.verify(args.store)
    if result.ok:
        print(f"Chain OK — {result.count} events, every hash verified.")
        return 0
    print(f"Chain BROKEN at seq {result.broken_seq}: {result.detail} "
          f"({result.count} events in store)")
    return 1


def cmd_query(args) -> int:
    events = store_mod.load_events(args.store)
    matched = query_events(events, trace_id=args.trace_id, agent=args.agent,
                           source=args.source, type=args.type, kind=args.kind)
    for e in matched:
        print(f"#{e['seq']:>3} {e['ts']} {e['source']:<12} {e['type']:<18} "
              f"agent={e['agent']} {summarize_event(e)}")
    print(f"-- {len(matched)} of {len(events)} events matched")
    return 0


def cmd_render(args) -> int:
    events = store_mod.load_events(args.store)
    if args.trace_id:
        events = query_events(events, trace_id=args.trace_id)
    result = store_mod.verify(args.store)
    page = render_timeline(events, verify_result=result)
    if args.out:
        Path(args.out).write_text(page, encoding="utf-8")
        print(f"rendered {len(events)} events -> {args.out}")
    else:
        print(page)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenttrace", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest producer artifacts into a store")
    p.add_argument("--store", required=True)
    p.add_argument("--canary", help="agent-canary verdict JSON")
    p.add_argument("--gate", help="agent-gate audit JSONL")
    p.add_argument("--blastradius", help="blast-radius audit JSONL")
    p.add_argument("--ledger", help="agent-ledger OTLP JSON export")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("verify", help="verify a store's hash chain")
    p.add_argument("--store", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("query", help="query events in a store")
    p.add_argument("--store", required=True)
    p.add_argument("--trace-id")
    p.add_argument("--agent")
    p.add_argument("--source")
    p.add_argument("--type")
    p.add_argument("--kind")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("render", help="render the one-page tree/timeline")
    p.add_argument("--store", required=True)
    p.add_argument("--trace-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SchemaError as exc:
        print(f"schema error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
