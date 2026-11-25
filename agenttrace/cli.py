"""AgentTrace CLI: ingest / verify / query / render / replay / diff.

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
from .replay import diff_traces, replay_steps, state_at
from .schema import SchemaError

INGESTORS = {
    "canary": ("agent-canary verdict JSON", ingest_mod.ingest_canary_verdict),
    "gate": ("agent-gate audit JSONL", ingest_mod.ingest_gate_audit),
    "blastradius": ("blast-radius audit JSONL", ingest_mod.ingest_blastradius_audit),
    "ledger": ("agent-ledger OTLP JSON", ingest_mod.ingest_ledger_otlp),
    "factoryfloor": ("factory-floor-agent trace JSONL", ingest_mod.ingest_factory_floor_trace),
    "chaos": ("agent-chaos report JSON", ingest_mod.ingest_chaos_report),
}


def cmd_ingest(args) -> int:
    total = 0
    attempted = False
    if getattr(args, "drop", None):
        attempted = True
        added, skipped = ingest_mod.ingest_drop_into_store(
            args.store, args.drop, trace_id=args.trace_id,
            source=args.source)
        print(f"ingested {added} events from drop file ({args.drop})"
              + (f"; {skipped} already in store, skipped" if skipped
                 else ""))
        total += added
    for kind, path in vars(args).items():
        if kind in INGESTORS and path:
            attempted = True
            events = INGESTORS[kind][1](path)
            store_mod.append_events(args.store, events)
            print(f"ingested {len(events)} events from {INGESTORS[kind][0]} ({path})")
            total += len(events)
    if not attempted:
        print("nothing to ingest: pass at least one producer artifact", file=sys.stderr)
        return 2
    result = store_mod.verify(args.store)
    print(f"store now holds {result.count} events; chain ok={result.ok}")
    return 0 if result.ok else 1


def cmd_verify(args) -> int:
    result = store_mod.verify(args.store)
    if not result.ok:
        print(f"Chain BROKEN at seq {result.broken_seq}: {result.detail} "
              f"({result.count} events in store)")
        return 1
    print(f"Chain OK — {result.count} events, every hash verified.")
    if not args.anchors:
        return 0
    # Strict mode (Red Team F1): the chain alone proves consistency,
    # not provenance -- check the signed head against the anchor log.
    from . import anchor as anchor_mod
    try:
        key = anchor_mod.key_from_env()
    except ValueError as exc:
        print(f"anchored verification unavailable: {exc}", file=sys.stderr)
        return 2
    anchored = anchor_mod.verify_against_anchors(args.store, args.anchors, key)
    if anchored.ok:
        print(f"Anchor OK — {anchored.detail}")
        return 0
    print(f"Anchor FAILED — {anchored.detail}")
    return 1


def cmd_anchor(args) -> int:
    from . import anchor as anchor_mod
    try:
        key = anchor_mod.key_from_env()
        entry = anchor_mod.anchor_store(
            args.store, args.anchors, key, environment=args.environment or "")
    except ValueError as exc:
        print(f"cannot anchor: {exc}", file=sys.stderr)
        return 2
    print(f"anchored {entry['event_count']} events, head {entry['head'][:16]}... "
          f"signature {entry['signature'][:16]}... -> {args.anchors}")
    return 0


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


def _print_state(state: dict) -> None:
    print(f"state @ seq {state['last_seq']} ({state['events']} events, "
          f"last {state['last_ts']})")
    for f in state["fleet"]:
        if f["events"]:
            print(f"  {f['source']:<20} {f['status']:<8} "
                  f"events={f['events']} last={f['last_type']}")
    for d in state["decisions"]:
        print(f"  DECISION #{d['seq']} {d['decision_point']}: {d['choice']}")
    cost = state["cost"]
    print(f"  cost so far: ${cost['total_usd']:.5f} "
          f"({cost['spans']} spans, {cost['billable_steps']} billable steps)")


def cmd_replay(args) -> int:
    events = store_mod.load_events(args.store)
    if args.step:
        for event, state in replay_steps(events, args.trace_id):
            print(f"#{event['seq']:>3} {event['source']:<12} "
                  f"{event['type']:<18} {summarize_event(event)}")
            if args.upto_seq is not None and event["seq"] >= args.upto_seq:
                break
        print("--")
    state = state_at(events, args.trace_id, upto_seq=args.upto_seq)
    if not state["events"]:
        print(f"no events for trace {args.trace_id}", file=sys.stderr)
        return 2
    _print_state(state)
    return 0


def cmd_diff(args) -> int:
    events_a = store_mod.load_events(args.store)
    events_b = store_mod.load_events(args.store_b) if args.store_b else events_a
    if not query_events(events_a, trace_id=args.trace_a):
        print(f"no events for trace {args.trace_a} in {args.store}",
              file=sys.stderr)
        return 2
    if not query_events(events_b, trace_id=args.trace_b):
        print(f"no events for trace {args.trace_b} in "
              f"{args.store_b or args.store}", file=sys.stderr)
        return 2
    result = diff_traces(events_a, args.trace_a, events_b, args.trace_b)
    print(f"{result['trace_a']}: {result['events_a']} events · "
          f"{result['trace_b']}: {result['events_b']} events")
    if result["equal"]:
        print("traces are behaviorally identical "
              "(event classes, order, decisions, cost)")
        return 0
    for d in result["divergences"]:
        print(f"DIVERGENCE [{d['kind']}] {d['detail']}")
    print(f"-- {len(result['divergences'])} divergence(s)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenttrace", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest producer artifacts into a store")
    p.add_argument("--store", required=True)
    p.add_argument("--canary", help="agent-canary verdict JSON")
    p.add_argument("--gate", help="agent-gate audit JSONL")
    p.add_argument("--blastradius", help="blast-radius audit JSONL")
    p.add_argument("--ledger", help="agent-ledger OTLP JSON export")
    p.add_argument("--factoryfloor", help="factory-floor-agent trace JSONL")
    p.add_argument("--chaos", help="agent-chaos report JSON")
    p.add_argument("--drop", help="generic drop JSONL from any project "
                                  "(see INTEGRATION.md)")
    p.add_argument("--trace-id", help="fallback trace ID for loose drop lines")
    p.add_argument("--source", help="fallback source for loose drop lines")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("verify", help="verify a store's hash chain")
    p.add_argument("--store", required=True)
    p.add_argument("--anchors",
                   help="anchor log to also check the signed chain head "
                        "against (strict mode; needs AGENTTRACE_SIGNING_KEY)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("anchor", help="sign the store's chain head and "
                                      "append it to the anchor log")
    p.add_argument("--store", required=True)
    p.add_argument("--anchors", required=True,
                   help="anchor log file (created if absent)")
    p.add_argument("--environment",
                   help="where this anchor was taken (ci / local / ...)")
    p.set_defaults(func=cmd_anchor)

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

    p = sub.add_parser("replay", help="TraceReplay: step through a trace "
                                      "and reconstruct its state")
    p.add_argument("--store", required=True)
    p.add_argument("--trace-id", required=True)
    p.add_argument("--upto-seq", type=int,
                   help="reconstruct state at this seq instead of the end")
    p.add_argument("--step", action="store_true",
                   help="print each event as it is stepped through")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("diff", help="TraceReplay: diff two traces "
                                    "event-class by event-class")
    p.add_argument("--store", required=True)
    p.add_argument("--trace-a", required=True)
    p.add_argument("--trace-b", required=True)
    p.add_argument("--store-b",
                   help="second store file (default: same store)")
    p.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SchemaError as exc:
        print(f"schema error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
