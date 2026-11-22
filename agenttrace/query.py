"""Query layer over the store: the same layer the CLI (and, in phase 2,
the read-only dashboard) reads through. No side channels -- if a datum
is not a chained event in the store, it is not queryable.
"""

from __future__ import annotations

import re


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


# ---------------------------------------------------------------------
# Dashboard view-models (phase 2 hybrid page): fleet-graph nodes,
# drill-beat progress, decision-record cards, cost breakdown. Like
# everything above, they are derived from stored events alone through
# this query layer -- the dashboard server adds no data of its own.
# ---------------------------------------------------------------------

_STEP_KEY_RE = re.compile(r"step (s\d+)/")


def _step_key(event: dict):
    m = _STEP_KEY_RE.search(event["payload"].get("detail") or "")
    if m:
        return m.group(1)
    p = event["payload"]
    return (p.get("action"), p.get("resource"))


def _kill_seam(br_events: list[dict]):
    """The kill -9 seam in a blast-radius run: a step_started whose step
    was already started earlier -- the step that was in flight when the
    executor died, replayed on resume. None when no step repeats."""
    seen = set()
    for e in br_events:
        if e["type"] == "step_started":
            key = _step_key(e)
            if key in seen:
                return e
            seen.add(key)
    return None


_BEAT_DEFS = [
    ("block", "BLOCK", "canary stops the candidate"),
    ("flagged", "FLAGGED", "gate screens the tool result"),
    ("plan", "PLAN", "blast-radius plan, approved"),
    ("kill", "KILL -9", "executor dies mid-step — replayed on resume"),
    ("rollback", "ROLLBACK", "verify fails → state restored"),
    ("answered", "ANSWERED", "grounded QA reply"),
    ("costed", "COSTED", "ledger spans joined by the trace ID"),
    ("matrix", "MATRIX DONE", "chaos scenario matrix"),
]


def drill_progress(events: list[dict], trace_id: str) -> dict:
    """Drill-beat progress for one trace, from the store alone.

    Each beat lights when its trigger event exists in the trace: the
    canary verdict, the gate screening, the blast-radius plan, the
    kill -9 seam (a step started twice), the rollback, the QA outcome,
    the first ledger span, the chaos report. The most recently lit
    beat is the frontier while the run is incomplete.
    """
    mine = query_events(events, trace_id=trace_id)

    def first(source, type_):
        for e in mine:
            if e["source"] == source and e["type"] == type_:
                return e
        return None

    br = [e for e in mine if e["source"] == "blast-radius"]
    rollback_ev = next((e for e in br if e["type"] in
                        ("rollback_step", "rollback_step_skipped")), None)
    if rollback_ev is None:
        rollback_ev = next((e for e in br if e["type"] == "run_finished"
                            and e["payload"].get("result") == "rolled_back"), None)
    triggers = {
        "block": first("agent-canary", "compare_verdict"),
        "flagged": first("agent-gate", "screening_findings"),
        "plan": first("blast-radius", "plan_created"),
        "kill": _kill_seam(br),
        "rollback": rollback_ev,
        "answered": first("factory-floor-agent", "outcome"),
        "costed": first("agent-ledger", "span"),
        "matrix": first("agent-chaos", "chaos_report"),
    }
    beats, last_lit = [], -1
    for i, (key, label, desc) in enumerate(_BEAT_DEFS):
        t = triggers[key]
        if t is not None:
            last_lit = i
        beats.append({
            "key": key, "label": label, "desc": desc, "lit": t is not None,
            "seq": t["seq"] if t else None, "ts": t["ts"] if t else None,
            "detail": summarize_event(t) if t else "", "frontier": False,
        })
    complete = all(b["lit"] for b in beats)
    if not complete and last_lit >= 0:
        beats[last_lit]["frontier"] = True
    return {"beats": beats, "complete": complete}


def decision_records(events: list[dict], trace_id: str) -> list[dict]:
    """Decision-record cards for one trace, in store order."""
    out = []
    for e in query_events(events, trace_id=trace_id, kind="decision_record"):
        p = e["payload"]
        out.append({
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "decision_point": p.get("decision_point") or e["type"],
            "choice": p.get("choice") or "",
            "summary": summarize_event(e),
        })
    return out


# ---------------------------------------------------------------------
# Incidents: the store's "needs attention" records -- computed decision
# records whose choice stopped something (BLOCK / DENY), and
# blast-radius runs that ended rolled back. Derived like everything
# else here: from stored events alone, in store order. Each incident
# names its anchor event (the decision / the run_finished) and the
# causal window of events leading into it, with a plain-language
# "why" assembled from the window's own records -- never narrated.
# ---------------------------------------------------------------------

_BLOCK_CHOICES = {"BLOCK", "DENY", "DENIED"}
_ROLLBACK_CHOICES = {"ROLLBACK", "ROLLED_BACK"}


def _window_cost(events: list[dict], from_seq: int, to_seq: int):
    """Ledger cost attributable to a seq window, or None.

    Cost exists only where agent-ledger emitted billable step spans;
    a window containing no such span has no attributable cost and is
    reported as None -- never as a borrowed fleet total.
    """
    steps = [e for e in query_events(events, source="agent-ledger")
             if from_seq <= e["seq"] <= to_seq
             and e["payload"].get("attributes", {}).get(
                 "agentledger.kind") == "step"]
    if not steps:
        return None
    return round(sum(float(e["payload"]["attributes"].get(
        "agentledger.cost_usd", 0.0)) for e in steps), 5)


def _decision_incident(events: list[dict], mine: list[dict],
                       anchor: dict, kind: str) -> dict:
    p = anchor["payload"]
    # Causal window: the contiguous run of this producer's events in
    # the trace ending at the decision record -- the computation and
    # the record it produced.
    from_seq = anchor["seq"]
    for e in reversed(mine):
        if e["seq"] > anchor["seq"]:
            continue
        if e["source"] != anchor["source"]:
            break
        from_seq = e["seq"]
    evidence = p.get("evidence") or {}
    why = [{
        "seq": anchor["seq"],
        "text": f"{p.get('decision_point') or anchor['type']}: "
                f"{anchor['agent']} chose {p.get('choice')} "
                f"(computed decision record, decider={p.get('decider')}).",
    }]
    for reason in evidence.get("reasons") or []:
        why.append({"seq": anchor["seq"], "text": str(reason)})
    cand = evidence.get("candidate_version")
    if kind == "block" and cand:
        title = f"{anchor['source']} blocked candidate {cand}"
    else:
        title = f"{anchor['source']} — {p.get('choice')}"
    summary = "; ".join(w["text"] for w in why[1:]) or summarize_event(anchor)
    return {
        "id": f"incident-{anchor['seq']}",
        "kind": kind,
        "trace_id": anchor["trace_id"],
        "source": anchor["source"],
        "agent": anchor["agent"],
        "seq": anchor["seq"],
        "ts": anchor["ts"],
        "title": title,
        "summary": summary,
        "window": {"from_seq": from_seq, "to_seq": anchor["seq"]},
        "why": why,
        "cost_usd": _window_cost(events, from_seq, anchor["seq"]),
    }


def _rollback_incident(events: list[dict], mine: list[dict],
                       anchor: dict) -> dict:
    p = anchor["payload"]
    run_id = p.get("run_id")
    br = [e for e in mine if e["source"] == "blast-radius"
          and e["seq"] <= anchor["seq"]]
    plan = next((e for e in br if e["type"] == "plan_created"
                 and (run_id is None
                      or e["payload"].get("run_id") == run_id)), None)
    from_seq = plan["seq"] if plan else (br[0]["seq"] if br
                                         else anchor["seq"])
    window = [e for e in br if from_seq <= e["seq"] <= anchor["seq"]]
    why: list[dict] = []
    if plan:
        why.append({"seq": plan["seq"], "text":
                    f"Plan created: {plan['payload'].get('detail') or ''}"})
    appr = next((e for e in window if e["type"] == "approval_granted"), None)
    if appr:
        why.append({"seq": appr["seq"], "text":
                    f"Approved by "
                    f"{appr['payload'].get('approved_by') or appr['agent']}."})
    seam = _kill_seam(window)
    if seam is not None:
        key = _step_key(seam)
        first = next(e for e in window if e["type"] == "step_started"
                     and _step_key(e) == key)
        why.append({"seq": seam["seq"], "text":
                    f"Executor died mid-step (kill -9 seam): "
                    f"'{seam['payload'].get('detail') or seam['payload'].get('action')}' "
                    f"started at #{first['seq']} and again at #{seam['seq']} "
                    f"-- the in-flight step was replayed on resume."})
    for e in window:
        if e["type"] == "step_applied" \
                and (e["payload"].get("result") or "ok") != "ok":
            why.append({"seq": e["seq"], "text":
                        f"Step did not complete cleanly "
                        f"({e['payload']['result']}): "
                        f"{e['payload'].get('detail') or ''}"})
    for e in window:
        if e["type"] == "verify_check" \
                and e["payload"].get("result") == "FAIL":
            why.append({"seq": e["seq"], "text":
                        f"Verification check failed: "
                        f"{e['payload'].get('detail') or ''}"})
    vf = next((e for e in window if e["type"] == "verify_failed"), None)
    if vf is not None:
        why.append({"seq": vf["seq"], "text":
                    f"Post-run verification failed -- "
                    f"{vf['payload'].get('detail') or ''}."})
    rb = [e for e in window
          if e["type"] in ("rollback_step", "rollback_step_skipped")]
    if rb:
        why.append({"seq": rb[0]["seq"], "text":
                    f"Rolled back {len(rb)} step(s) in reverse order "
                    f"(#{rb[0]['seq']}-#{rb[-1]['seq']}), undoing each "
                    f"applied step and restoring the pre-run state."})
    why.append({"seq": anchor["seq"], "text":
                f"Run finished: rolled_back -- {p.get('detail') or ''}."})
    bits = []
    if seam is not None:
        bits.append("executor killed mid-step (kill -9); "
                    "the step was replayed on resume")
    if vf is not None:
        bits.append(f"post-run verification failed "
                    f"({vf['payload'].get('detail') or 'see window'})")
    if rb:
        bits.append("every applied step was rolled back in reverse order")
    return {
        "id": f"incident-{anchor['seq']}",
        "kind": "rollback",
        "trace_id": anchor["trace_id"],
        "source": anchor["source"],
        "agent": anchor["agent"],
        "seq": anchor["seq"],
        "ts": anchor["ts"],
        "title": "blast-radius rolled back its run",
        "summary": "; ".join(bits) or summarize_event(anchor),
        "window": {"from_seq": from_seq, "to_seq": anchor["seq"]},
        "why": why,
        "cost_usd": _window_cost(events, from_seq, anchor["seq"]),
    }


def incidents(events: list[dict], trace_id: str) -> list[dict]:
    """Incidents in one trace, in store order.

    Two derivations: (1) computed decision records whose choice is a
    stop -- BLOCK / DENY (a block incident) or ROLLBACK; (2)
    blast-radius runs whose run_finished result is rolled_back (the
    rollback is an event there, not a decision record, so it is
    derived from the run's own records).
    """
    mine = query_events(events, trace_id=trace_id)
    out: list[dict] = []
    for e in mine:
        if e["kind"] != "decision_record":
            continue
        choice = str(e["payload"].get("choice") or "").upper()
        if choice in _BLOCK_CHOICES:
            out.append(_decision_incident(events, mine, e, "block"))
        elif choice in _ROLLBACK_CHOICES:
            out.append(_decision_incident(events, mine, e, "rollback"))
    for e in mine:
        if e["source"] == "blast-radius" and e["type"] == "run_finished" \
                and (e["payload"].get("result") or "") == "rolled_back":
            out.append(_rollback_incident(events, mine, e))
    out.sort(key=lambda i: i["seq"])
    return out


def cost_breakdown(events: list[dict], limit: int = 5) -> list[dict]:
    """Top billable ledger steps by cost, plus a remainder row.

    The cost bars of the dashboard: labels are task / step pairs from
    the span attributes; costs are the spans' agentledger.cost_usd.
    """
    steps = [e for e in query_events(events, source="agent-ledger")
             if e["payload"].get("attributes", {}).get("agentledger.kind") == "step"]
    rows = []
    for e in steps:
        a = e["payload"]["attributes"]
        label = (f"{a.get('agentledger.task_id', '?')} / "
                 f"{a.get('agentledger.step', e['payload'].get('name', ''))}")
        rows.append({"label": label,
                     "cost_usd": round(float(a.get("agentledger.cost_usd", 0.0)), 5)})
    rows.sort(key=lambda r: -r["cost_usd"])
    if len(rows) > limit:
        rest = rows[limit:]
        rows = rows[:limit] + [{
            "label": f"+ {len(rest)} further billable steps",
            "cost_usd": round(sum(r["cost_usd"] for r in rest), 5),
        }]
    return rows


def graph_nodes(events: list[dict], trace_id: str, *, now: float) -> list[dict]:
    """Fleet-graph nodes for one trace: per-producer status, a headline
    verdict and a one-line detail, all derived from the trace's events.
    Counts are per-trace (what this run emitted), status comes from the
    fleet view over the whole store."""
    fleet = {f["source"]: f for f in fleet_status(events, now=now)}
    pending = {p["source"] for p in pending_approvals(events)}
    mine_all = query_events(events, trace_id=trace_id)
    sources = list(KNOWN_SOURCES) + sorted(
        {e["source"] for e in mine_all} - set(KNOWN_SOURCES))
    nodes = []
    for source in sources:
        mine = [e for e in mine_all if e["source"] == source]
        f = fleet.get(source)
        status = f["status"] if f else "idle"
        headline, detail, tone = _node_story(source, mine, status,
                                             source in pending)
        nodes.append({
            "source": source, "status": status, "tone": tone,
            "headline": headline, "detail": detail, "events": len(mine),
            "last_ts": mine[-1]["ts"] if mine else None,
            "last_type": mine[-1]["type"] if mine else None,
        })
    return nodes


def _node_story(source: str, mine: list[dict], status: str,
                is_pending: bool) -> tuple[str, str, str]:
    """(headline, detail, tone) for one graph node, from its events."""
    if not mine:
        return "—", "no events yet", "idle"
    last = mine[-1]
    if is_pending and source == "blast-radius" \
            and not any(e["type"] == "run_finished" for e in mine):
        return "AWAITING APPROVAL", summarize_event(last), "warn"
    if source == "agent-canary":
        v = next((e for e in mine if e["type"] == "compare_verdict"), None)
        if v:
            p = v["payload"]
            dec = p.get("decision") or "VERDICT"
            b = (p.get("base_aggregate") or {}).get("success_rate")
            c = (p.get("cand_aggregate") or {}).get("success_rate")
            forb = (p.get("cand_aggregate") or {}).get("forbidden_hits")
            detail = ""
            if b is not None and c is not None:
                detail = f"success {b:.0%} → {c:.0%}"
                if forb:
                    detail += f" · {forb} forbidden"
            return dec, detail or summarize_event(v), \
                "bad" if dec == "BLOCK" else "ok"
    if source == "agent-gate":
        s = next((e for e in mine if e["type"] == "screening_findings"), None)
        if s:
            return "FLAGGED", \
                f"{len(s['payload'].get('findings', []))} screening patterns matched", \
                "warn"
    if source == "blast-radius":
        seam = _kill_seam(mine)
        fin = next((e for e in reversed(mine) if e["type"] == "run_finished"), None)
        if fin:
            result = fin["payload"].get("result") or ""
            bits = []
            if seam:
                bits.append("kill -9 · replayed")
            if any(e["type"] == "verify_failed" for e in mine):
                bits.append("verify FAIL")
            bits.append(result.replace("_", " "))
            return result.replace("_", " ").upper() or "FINISHED", \
                " · ".join(bits), "warn" if result == "rolled_back" else "ok"
        if seam:
            return "KILLED -9", "step replayed on resume", "bad"
    if source == "factory-floor-agent":
        o = next((e for e in reversed(mine) if e["type"] == "outcome"), None)
        if o:
            out = o["payload"].get("outcome") or ""
            g = next((e for e in mine if e["type"] == "groundedness"), None)
            detail = ""
            if g:
                gp = g["payload"]
                detail = (f"grounded={gp.get('grounded')} · "
                          f"{len(gp.get('claims', []))} claims · "
                          f"thr {gp.get('threshold')}")
            return out.upper() or "DONE", detail or summarize_event(o), \
                "ok" if out == "answered" else "warn"
    if source == "agent-ledger":
        cs = cost_summary(mine)
        if cs["spans"]:
            return "COSTED", f"{cs['spans']} spans · ${cs['total_usd']:.5f}", "idle"
    if source == "agent-chaos":
        r = next((e for e in reversed(mine) if e["type"] == "chaos_report"), None)
        if r:
            h = r["payload"].get("headline", {})
            off = h.get("silent_failure_rate_defenses_off")
            on = h.get("silent_failure_rate_defenses_on")
            detail = f"silent-failure {off} → {on}" if off is not None \
                else summarize_event(r)
            return "MATRIX DONE", detail, "idle"
    if status == "running":
        return "RUNNING", summarize_event(last), "run"
    if status == "blocked":
        return "BLOCKED", summarize_event(last), "warn"
    return status.upper(), summarize_event(last), "idle"


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
