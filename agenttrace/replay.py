"""TraceReplay: step through a stored trace and diff two traces.

The debugging workflow that reads AgentTrace's own store -- nothing
else. A replay walks one trace's events in ``seq`` order and, at any
point, reconstructs the state the store implied *at that point*: which
producers had reported, what had been decided so far, what the run had
cost so far. A diff compares two traces (a drill and its re-run, say)
event-class by event-class -- an event class is ``(source, type,
kind)`` -- and names every divergence: classes present in only one
trace, classes whose counts differ, the first position where the two
class sequences diverge, decision choices that differ, and the cost
delta. Timestamps, seq numbers, and hashes are identity, not behavior,
so they are deliberately excluded from the comparison: two honest
re-runs of the same drill should diff clean even though no timestamp
matches.
"""

from __future__ import annotations

from collections import Counter

from .query import (
    _parse_ts,
    cost_summary,
    decision_records,
    fleet_status,
    query_events,
    summarize_event,
)


def event_class(event: dict) -> tuple[str, str, str]:
    """The behavioral class of an event: (source, type, kind)."""
    return (event["source"], event["type"], event["kind"])


def state_at(events: list[dict], trace_id: str, *, upto_seq: int | None = None) -> dict:
    """Reconstruct the state of one trace at a point in its history.

    ``events`` is a whole store (any traces); only ``trace_id`` events
    with ``seq <= upto_seq`` (all of them, by default) are considered.
    The returned state is exactly what the query layer derives from
    that prefix: fleet status (clocked at the prefix's last event, so
    replay is deterministic and wall-clock-free), decisions so far,
    cost so far, per-source counts, and the last event seen.
    """
    mine = query_events(events, trace_id=trace_id)
    if upto_seq is not None:
        mine = [e for e in mine if e["seq"] <= upto_seq]
    if not mine:
        return {
            "trace_id": trace_id, "upto_seq": upto_seq, "events": 0,
            "last_seq": None, "last_ts": None, "last_event": None,
            "sources": {}, "fleet": [], "decisions": [],
            "cost": {"spans": 0, "billable_steps": 0, "total_usd": 0.0},
        }
    last = mine[-1]
    now = _parse_ts(last["ts"]) or 0.0
    return {
        "trace_id": trace_id,
        "upto_seq": upto_seq,
        "events": len(mine),
        "last_seq": last["seq"],
        "last_ts": last["ts"],
        "last_event": {
            "seq": last["seq"], "source": last["source"],
            "type": last["type"], "kind": last["kind"],
            "summary": summarize_event(last),
        },
        "sources": dict(Counter(e["source"] for e in mine)),
        "fleet": fleet_status(mine, now=now),
        "decisions": decision_records(mine, trace_id),
        "cost": cost_summary(mine),
    }


def replay_steps(events: list[dict], trace_id: str):
    """Yield ``(event, state)`` for each event of the trace, in seq
    order -- the state reconstructed immediately after that event."""
    mine = query_events(events, trace_id=trace_id)
    for event in mine:
        yield event, state_at(events, trace_id, upto_seq=event["seq"])


def diff_traces(events_a: list[dict], trace_id_a: str,
                events_b: list[dict], trace_id_b: str) -> dict:
    """Diff two traces event-class by event-class.

    ``events_a`` / ``events_b`` are the stores the traces live in (the
    same list twice when both traces share one store). Returns a dict
    with ``equal`` (True only when no divergence was found) and
    ``divergences``: a list of ``{"kind", "detail", ...}`` records
    naming each difference -- class presence, class counts, first
    sequence divergence, decision-choice differences, cost delta.
    """
    a = query_events(events_a, trace_id=trace_id_a)
    b = query_events(events_b, trace_id=trace_id_b)
    divergences: list[dict] = []

    counts_a, counts_b = Counter(map(event_class, a)), Counter(map(event_class, b))
    for cls in sorted(set(counts_a) | set(counts_b)):
        label = "/".join(cls)
        if cls not in counts_a:
            divergences.append({
                "kind": "class_only_in_b", "event_class": label,
                "detail": f"{label}: absent from {trace_id_a}, "
                          f"{counts_b[cls]}x in {trace_id_b}",
            })
        elif cls not in counts_b:
            divergences.append({
                "kind": "class_only_in_a", "event_class": label,
                "detail": f"{label}: {counts_a[cls]}x in {trace_id_a}, "
                          f"absent from {trace_id_b}",
            })
        elif counts_a[cls] != counts_b[cls]:
            divergences.append({
                "kind": "class_count_differs", "event_class": label,
                "detail": f"{label}: {counts_a[cls]}x in {trace_id_a} vs "
                          f"{counts_b[cls]}x in {trace_id_b}",
            })

    seq_a, seq_b = [event_class(e) for e in a], [event_class(e) for e in b]
    for i, (ca, cb) in enumerate(zip(seq_a, seq_b)):
        if ca != cb:
            divergences.append({
                "kind": "sequence_diverges", "position": i + 1,
                "detail": f"event #{i + 1}: {trace_id_a} has "
                          f"{'/'.join(ca)}, {trace_id_b} has {'/'.join(cb)}",
            })
            break
    if len(seq_a) != len(seq_b) and seq_a[:min(len(seq_a), len(seq_b))] \
            == seq_b[:min(len(seq_a), len(seq_b))]:
        divergences.append({
            "kind": "length_differs",
            "detail": f"{trace_id_a} has {len(seq_a)} events, "
                      f"{trace_id_b} has {len(seq_b)}; the shorter is a "
                      "prefix of the longer",
        })

    dec_a = [(d["decision_point"], d["choice"])
             for d in decision_records(a, trace_id_a)]
    dec_b = [(d["decision_point"], d["choice"])
             for d in decision_records(b, trace_id_b)]
    if dec_a != dec_b:
        divergences.append({
            "kind": "decisions_differ",
            "detail": f"decisions: {trace_id_a}={dec_a} vs "
                      f"{trace_id_b}={dec_b}",
        })

    cost_a, cost_b = cost_summary(a), cost_summary(b)
    if cost_a["total_usd"] != cost_b["total_usd"]:
        divergences.append({
            "kind": "cost_differs",
            "detail": f"cost: {trace_id_a}=${cost_a['total_usd']:.5f} "
                      f"({cost_a['billable_steps']} steps) vs "
                      f"{trace_id_b}=${cost_b['total_usd']:.5f} "
                      f"({cost_b['billable_steps']} steps)",
        })

    return {
        "trace_a": trace_id_a, "trace_b": trace_id_b,
        "events_a": len(a), "events_b": len(b),
        "equal": not divergences,
        "divergences": divergences,
    }
