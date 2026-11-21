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
    return ""
