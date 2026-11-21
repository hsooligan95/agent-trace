"""Query layer over the store: the same layer the CLI (and, in phase 2,
the read-only dashboard) reads through. No side channels -- if a datum
is not a chained event in the store, it is not queryable.
"""

from __future__ import annotations


def query_events(events: list[dict], *, trace_id: str | None = None,
                 agent: str | None = None, source: str | None = None,
                 type: str | None = None, kind: str | None = None) -> list[dict]:
    """Filter events by any combination of trace ID, agent, source, type, kind."""
    out = events
    if trace_id is not None:
        out = [e for e in out if e["trace_id"] == trace_id]
    if agent is not None:
        out = [e for e in out if e["agent"] == agent]
    if source is not None:
        out = [e for e in out if e["source"] == source]
    if type is not None:
        out = [e for e in out if e["type"] == type]
    if kind is not None:
        out = [e for e in out if e["kind"] == kind]
    return out


KNOWN_SOURCES = [
    "agent-canary", "agent-gate", "blast-radius",
    "factory-floor-agent", "agent-ledger", "agent-chaos",
]

# An agent whose latest event is older than this (seconds, against the
# reference clock) is reported idle; fresher than this, running --
# unless it is blocked on a pending approval, which outranks both.
ACTIVE_WINDOW_SECONDS = 20.0


def _parse_ts(ts: str):
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def pending_approvals(events: list[dict]) -> list[dict]:
    """Approvals the store shows as requested but not yet resolved.

    Display data only -- approvals are resolved at the producer, never
    here. blast-radius: a plan_created with no later approval_granted /
    approval_denied. agent-gate: a policy_decision whose effect is
    require-approval with no later tool_executed for the same tool.
    """
    pending: list[dict] = []
    for e in events:
        source, etype, p = e["source"], e["type"], e["payload"]
        if source == "blast-radius" and etype == "plan_created":
            pending.append({
                "source": source, "agent": e["agent"], "seq": e["seq"],
                "ts": e["ts"], "what": "remediation plan",
                "detail": p.get("detail") or "",
            })
        elif source == "blast-radius" and etype in ("approval_granted", "approval_denied"):
            pending = [x for x in pending if x["source"] != "blast-radius"]
        elif source == "agent-gate" and etype == "policy_decision" \
                and p.get("effect") in ("require_approval", "require-approval"):
            pending.append({
                "source": source, "agent": e["agent"], "seq": e["seq"],
                "ts": e["ts"], "what": f"tool call: {p.get('tool')}",
                "detail": p.get("reason") or "",
            })
        elif source == "agent-gate" and etype == "tool_executed":
            pending = [x for x in pending
                       if not (x["source"] == "agent-gate"
                               and x["what"] == f"tool call: {p.get('tool')}")]
    return pending


def cost_summary(events: list[dict]) -> dict:
    """Cost accounting from the agent-ledger span events in the store.

    Mirrors the drill's cost line: billable steps are spans whose
    agentledger.kind is "step"; the total is their agentledger.cost_usd.
    Events from any other source carry no cost and are ignored.
    """
    spans = query_events(events, source="agent-ledger")
    steps = [e for e in spans
             if e["payload"].get("attributes", {}).get("agentledger.kind") == "step"]
    total = sum(float(e["payload"]["attributes"].get("agentledger.cost_usd", 0.0))
                for e in steps)
    return {"spans": len(spans), "billable_steps": len(steps),
            "total_usd": round(total, 5)}


def fleet_status(events: list[dict], *, now: float) -> list[dict]:
    """Per-agent status derived from the store alone.

    One entry per known producer plus any other source present in the
    store: status (blocked on a pending approval > running > idle),
    last event, event count, and that source's ledger cost if any.
    """
    pending_sources = {p["source"] for p in pending_approvals(events)}
    sources = list(KNOWN_SOURCES) + sorted(
        {e["source"] for e in events} - set(KNOWN_SOURCES))
    fleet = []
    for source in sources:
        mine = query_events(events, source=source)
        entry = {
            "source": source,
            "agent": mine[-1]["agent"] if mine else source,
            "status": "idle",
            "events": len(mine),
            "last_seq": mine[-1]["seq"] if mine else None,
            "last_ts": mine[-1]["ts"] if mine else None,
            "last_type": mine[-1]["type"] if mine else None,
            "last_summary": summarize_event(mine[-1]) if mine else "",
            "cost_usd": cost_summary(mine)["total_usd"] if mine else 0.0,
        }
        if source in pending_sources:
            entry["status"] = "blocked"
        elif mine:
            last = _parse_ts(mine[-1]["ts"])
            if last is not None and now - last <= ACTIVE_WINDOW_SECONDS:
                entry["status"] = "running"
        fleet.append(entry)
    return fleet


def trace_tree(events: list[dict], trace_id: str) -> list[dict]:
    """Span tree / timeline for one trace, from the store alone.

    Branches per source in first-appearance order; each event a node.
    agent-ledger spans carry a depth computed from their OTLP
    parent_span_id chain, so the span tree nests; other producers'
    events sit at depth 0 in store order.
    """
    mine = query_events(events, trace_id=trace_id)
    span_depth: dict[str, int] = {}
    for e in mine:
        if e["source"] == "agent-ledger":
            sid = e["payload"].get("span_id")
            parent = e["payload"].get("parent_span_id")
            if sid:
                span_depth[sid] = span_depth.get(parent, -1) + 1 if parent else 0
    branches = []
    for source in dict.fromkeys(e["source"] for e in mine):
        nodes = []
        for e in query_events(mine, source=source):
            depth = 0
            if e["source"] == "agent-ledger":
                depth = span_depth.get(e["payload"].get("span_id"), 0)
            nodes.append({
                "seq": e["seq"], "ts": e["ts"], "agent": e["agent"],
                "type": e["type"], "kind": e["kind"], "depth": depth,
                "summary": summarize_event(e),
            })
        branches.append({"source": source, "events": nodes})
    return branches


def summarize_event(event: dict) -> str:
    """One-line human summary of an event, by producer shape."""
    p = event["payload"]
    source, etype = event["source"], event["type"]
    if event["kind"] == "decision_record":
        return (f"DECISION {p.get('decision_point')}: {p.get('choice')} "
                f"(decider={p.get('decider')}, computed={p.get('computed')})")
    if source == "agent-canary" and etype == "compare_verdict":
        return (f"compare {p.get('base_version')} vs {p.get('candidate_version')}: "
                f"decision={p.get('decision')} reasons={len(p.get('reasons', []))}")
    if source == "agent-gate":
        detail = p.get("tool") or p.get("agent") or ""
        if etype == "screening_findings":
            detail = "patterns=" + ",".join(f["pattern"] for f in p["findings"])
        elif etype == "policy_decision":
            detail = f"{p.get('tool')} -> {p.get('effect')} ({p.get('reason')})"
        return detail
    if source == "blast-radius":
        return " ".join(x for x in [
            p.get("action") or "", p.get("resource") or "",
            f"result={p.get('result')}", p.get("detail") or ""] if x)
    if source == "agent-ledger":
        cost = p.get("attributes", {}).get("agentledger.cost_usd")
        return f"span {p.get('name')}" + (f" cost=${cost}" if cost is not None else "")
    if source == "factory-floor-agent":
        if etype == "run_start":
            return f"question: {p.get('question')}"
        if etype == "plan":
            return f"plan: {p.get('plan')}"
        if etype == "retrieval":
            return f"{len(p.get('hits', []))} passages (attempts={p.get('attempts')})"
        if etype == "groundedness":
            return (f"grounded={p.get('grounded')} threshold={p.get('threshold')} "
                    f"claims={len(p.get('claims', []))}")
        if etype in ("outcome", "run_end"):
            return f"outcome={p.get('outcome')}"
        return ""
    if source == "agent-chaos":
        if etype == "chaos_report":
            h = p.get("headline", {})
            return (f"silent-failure off={h.get('silent_failure_rate_defenses_off')} "
                    f"on={h.get('silent_failure_rate_defenses_on')} "
                    f"({p.get('rows')} rows)")
        if etype == "scenario_result":
            return (f"{p.get('scenario')} mode={p.get('mode')} "
                    f"outcome={p.get('outcome')} detected={p.get('detected')}")
        return ""
    return ""
