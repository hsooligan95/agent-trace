"""Query layer over the store: the same layer the CLI (and, in phase 2,
the read-only dashboard) reads through. No side channels -- if a datum
is not a chained event in the store, it is not queryable.
"""

from __future__ import annotations

import re


def event_instance(event: dict) -> str:
    """The producing process instance of one event.

    The optional top-level ``instance`` field when the producer
    declared one; otherwise the event's ``agent`` value -- the
    pre-instance behavior, so stores written before the field
    existed group exactly as they always did (one implicit instance
    per actor name).
    """
    return event.get("instance") or event["agent"]


def query_events(events: list[dict], *, trace_id: str | None = None,
                 agent: str | None = None, source: str | None = None,
                 type: str | None = None, kind: str | None = None,
                 instance: str | None = None) -> list[dict]:
    """Filter events by any combination of trace ID, agent, source,
    type, kind, instance (the effective instance -- see
    event_instance)."""
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
    if instance is not None:
        out = [e for e in out if event_instance(e) == instance]
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


# Payload keys that describe the decision itself, not its evidence.
_DECISION_META_KEYS = frozenset({
    "decision_point", "decider", "computed", "choice", "options",
    "evidence",
})


def normalize_decision(event: dict) -> dict:
    """One decision record, built-in or generic, in one shape.

    Built-in producers (canary / gate / factory-floor) emit a bespoke
    structure: decision_point, decider, computed, and an evidence
    dict. Generic producers (emit() / file-drop -- e.g. music-maker's
    score_computed, a refactor's verdict) emit a decision_record whose
    payload is just the choice plus the deciding values loose in the
    payload. Both are the same artifact: the point falls back to the
    event type, the decider to the emitting agent, and the evidence
    to the payload's non-meta fields. Nothing is invented -- a field
    the producer did not emit stays absent, never rendered as None.
    """
    p = event["payload"]
    evidence = p.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        evidence = {k: v for k, v in p.items()
                    if k not in _DECISION_META_KEYS}
    out = {
        "decision_point": p.get("decision_point") or event["type"],
        "decider": p.get("decider") or event["agent"],
        "choice": p.get("choice") or "",
        "evidence": evidence,
    }
    if p.get("computed") is not None:
        out["computed"] = p["computed"]
    if p.get("options"):
        out["options"] = p["options"]
    return out


def _evidence_summary(evidence: dict, limit: int = 5) -> str:
    """Compact ``k=v`` rendering of an evidence dict (scalars only,
    nested values shown as their key count -- detail lives in the
    store, the summary is a headline)."""
    parts = []
    for key, value in evidence.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        elif isinstance(value, (list, dict)):
            parts.append(f"{key}[{len(value)}]")
        if len(parts) >= limit:
            break
    return ", ".join(parts)


def decision_records(events: list[dict], trace_id: str) -> list[dict]:
    """Decision-record cards for one trace, in store order."""
    out = []
    for e in query_events(events, trace_id=trace_id, kind="decision_record"):
        d = normalize_decision(e)
        card = {
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "decision_point": d["decision_point"],
            "choice": d["choice"],
            "decider": d["decider"],
            "evidence": d["evidence"],
            "summary": summarize_event(e),
        }
        if "computed" in d:
            card["computed"] = d["computed"]
        out.append(card)
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
                f"(computed decision record, "
                f"decider={normalize_decision(anchor)['decider']}).",
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


# ---------------------------------------------------------------------
# Dashboard slice 1 view-models: admin overview (Tier 1) and
# per-agent-type pages (Tier 2). Like everything above, derived from
# stored events alone. Two honesty rules are load-bearing here:
#   * No blended health score. Status stays categorical, with its
#     components visible (fleet_status's rule), because a confident
#     number over an unauthenticated store manufactures assurance.
#   * Cost is reported only where agent-ledger actually emitted
#     spans; a source with no spans gets cost None -- never a
#     borrowed fleet total, never a silent zero.
# Instance identity (slice 3): producers may declare an optional
# top-level ``instance`` per event. Tier 2 groups a type's events by
# trace and calls those rows RUNS; when a store carries declared
# instances it additionally groups by (source, instance) and calls
# those rows INSTANCES. A store with no declared instance anywhere
# gets no instances section at all -- the runs labeling stands.
# ---------------------------------------------------------------------

PRODUCER_ROLES = {
    "agent-canary": "Promotion gate — compares a candidate agent version "
                    "against the baseline on a golden suite; blocks regressions.",
    "agent-gate": "Policy gateway — screens tool calls and results, enforces "
                  "allow / deny / require-approval at the tool boundary.",
    "blast-radius": "Change executor — plans infrastructure changes, runs them "
                    "under approval, verifies, and rolls back on failure.",
    "factory-floor-agent": "Grounded QA agent — answers over retrieved passages "
                           "and checks every claim for groundedness before replying.",
    "agent-ledger": "Cost ledger — OpenTelemetry spans attributing cost and "
                    "latency to individual agent steps.",
    "agent-chaos": "Chaos harness — injects faults into agent runs and measures "
                   "silent-failure rates, defenses off vs on.",
}

_BLOCK_SET = {"BLOCK", "DENY", "DENIED"}
_ROLLBACK_SET = {"ROLLBACK", "ROLLED_BACK"}
SPARK_BUCKETS = 24


def _choice_bucket(choice) -> str:
    c = str(choice or "").upper()
    if c in _BLOCK_SET:
        return "block"
    if c in _ROLLBACK_SET:
        return "rollback"
    if c == "ALLOW":
        return "allow"
    return "other"


def decision_tallies(events: list[dict]) -> dict:
    """Decision tallies over a set of events (one source's, or all).

    Buckets are allow / block / rollback / other, over computed
    decision records. blast-radius runs that finished rolled_back
    count toward rollback too: the rollback there is an event, not a
    decision record -- the same rule incidents() derives by.
    """
    by_choice: dict[str, int] = {}
    by_point: dict[str, int] = {}
    total = 0
    for e in events:
        if e["kind"] != "decision_record":
            continue
        p = e["payload"]
        choice = str(p.get("choice") or "?")
        by_choice[choice] = by_choice.get(choice, 0) + 1
        point = p.get("decision_point") or e["type"]
        by_point[point] = by_point.get(point, 0) + 1
        total += 1
    buckets = {"allow": 0, "block": 0, "rollback": 0, "other": 0}
    for choice, n in by_choice.items():
        buckets[_choice_bucket(choice)] += n
    rollback_runs = sum(
        1 for e in events
        if e["source"] == "blast-radius" and e["type"] == "run_finished"
        and (e["payload"].get("result") or "") == "rolled_back")
    buckets["rollback"] += rollback_runs
    return {"total": total, "by_choice": by_choice, "by_point": by_point,
            "buckets": buckets, "rollback_runs": rollback_runs}


def anomaly_kind(event: dict) -> str | None:
    """The anomaly class of one event, or None.

    The replay's jump-to-anomaly vocabulary, from the producer
    conventions already in the store: a computed decision that
    stopped something (block / rollback), a blast-radius run that
    ended rolled back, a failed verification check, a chaos fault
    that was injected and went undetected, and a billable ledger
    step (a step that cost money). Everything else returns None.
    """
    p, t = event["payload"], event["type"]
    if event["kind"] == "decision_record":
        bucket = _choice_bucket(p.get("choice"))
        return bucket if bucket in ("block", "rollback") else None
    if event["source"] == "blast-radius":
        if t == "run_finished" and (p.get("result") or "") == "rolled_back":
            return "rollback"
        if t == "verify_failed" or (t == "verify_check"
                                     and p.get("result") == "FAIL"):
            return "failed-check"
    if event["source"] == "agent-chaos" and t == "scenario_result" \
            and p.get("fault_injected") and not p.get("detected"):
        return "silent-failure"
    if event["source"] == "agent-ledger" and t == "span" \
            and p.get("attributes", {}).get("agentledger.kind") == "step":
        return "cost"
    return None


def _reason_labels(event: dict) -> list[str]:
    """Why-labels carried by one decision record's evidence."""
    if event["kind"] != "decision_record":
        return []
    ev = event["payload"].get("evidence") or {}
    labels = [str(r) for r in ev.get("reasons") or []]
    if ev.get("reason"):
        labels.append(str(ev["reason"]))
    labels += [f"pattern: {p}" for p in ev.get("patterns") or []]
    return labels


def _trigger_labels(event: dict) -> list[str]:
    """Failure / rollback-trigger labels from producer conventions."""
    p, t = event["payload"], event["type"]
    if event["source"] == "blast-radius":
        if t == "verify_check" and p.get("result") == "FAIL":
            return [f"verify check failed: {p.get('detail') or 'check'}"]
        if t == "verify_failed":
            return [("post-run verification failed: "
                     f"{p.get('detail') or ''}").rstrip(": ")]
        if t == "step_applied" and (p.get("result") or "ok") != "ok":
            return [(f"step ended {p['result']}: "
                     f"{p.get('detail') or ''}").rstrip(": ")]
    if event["source"] == "agent-chaos" and t == "scenario_result" \
            and p.get("fault_injected") and not p.get("detected"):
        return [f"undetected fault: {p.get('scenario')}"
                f" ({p.get('fault') or 'fault injected'})"]
    return []


def decision_reasons(events: list[dict], limit: int = 12) -> list[dict]:
    """Decision reasons + failure triggers, counted, most frequent
    first. Counts without reasons are useless; this is the reasons."""
    counts: dict[str, dict] = {}
    for e in events:
        for label in _reason_labels(e):
            row = counts.setdefault(label, {"label": label, "count": 0,
                                            "kind": "reason"})
            row["count"] += 1
        for label in _trigger_labels(e):
            row = counts.setdefault(label, {"label": label, "count": 0,
                                            "kind": "trigger"})
            row["count"] += 1
    return sorted(counts.values(),
                  key=lambda r: (-r["count"], r["label"]))[:limit]


def outcome_tallies(events: list[dict]) -> list[dict]:
    """Run/scenario outcome labels for one source's events, counted."""
    counts: dict[str, int] = {}
    for e in events:
        p, t = e["payload"], e["type"]
        label = None
        if t in ("run_finished", "run_end"):
            label = p.get("result") or p.get("outcome")
        elif t == "outcome":
            label = p.get("outcome")
        elif t == "scenario_result":
            label = p.get("outcome")
        if label:
            counts[str(label)] = counts.get(str(label), 0) + 1
    return [{"label": k, "count": v} for k, v in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _rate_buckets(events: list[dict], window: tuple | None = None,
                  buckets: int = SPARK_BUCKETS) -> list[int]:
    """Event counts per time bucket over a (start, end) window.

    The window defaults to the events' own span; callers pass a
    shared window so sparklines compare on one axis. Falls back to
    position buckets when timestamps don't span (single instant)."""
    if not events:
        return [0] * buckets
    if window is None:
        tss = [t for t in (_parse_ts(e["ts"]) for e in events)
               if t is not None]
        window = (min(tss), max(tss)) if tss else (None, None)
    start, end = window
    out = [0] * buckets
    if start is None or end is None or end <= start:
        for i, _e in enumerate(events):
            out[min(buckets - 1, i * buckets // len(events))] += 1
        return out
    span = end - start
    for e in events:
        t = _parse_ts(e["ts"])
        if t is None:
            continue
        out[min(buckets - 1, int((t - start) / span * buckets))] += 1
    return out


def _time_window(events: list[dict]) -> tuple:
    tss = [t for t in (_parse_ts(e["ts"]) for e in events) if t is not None]
    return (min(tss), max(tss)) if tss else (None, None)


def _run_rows(events: list[dict], source: str, mine: list[dict]) -> list[dict]:
    """Per-trace runs of one source -- proto-instances, labeled runs.

    The store has no instance field; one row is one trace's worth of
    this source's events, NOT one process instance.
    """
    rows = []
    for trace_id in dict.fromkeys(e["trace_id"] for e in mine):
        run = [e for e in mine if e["trace_id"] == trace_id]
        cs = cost_summary(run)
        outcome = ""
        for e in reversed(run):
            if e["type"] in ("run_finished", "run_end", "outcome"):
                outcome = str(e["payload"].get("result")
                              or e["payload"].get("outcome") or "")
                break
        rows.append({
            "trace_id": trace_id,
            "events": len(run),
            "first_seq": run[0]["seq"], "last_seq": run[-1]["seq"],
            "first_ts": run[0]["ts"], "last_ts": run[-1]["ts"],
            "decisions": decision_tallies(run)["buckets"],
            "decisions_total": decision_tallies(run)["total"],
            "cost_usd": cs["total_usd"] if cs["spans"] else None,
            "outcome": outcome,
            "last_summary": summarize_event(run[-1]),
        })
    rows.sort(key=lambda r: r["first_seq"])
    return rows


def _instance_rows(mine: list[dict]) -> list[dict]:
    """Per-instance rows for one source's events, grouped by the
    effective instance (declared ``instance`` field, else the actor
    name -- see event_instance).

    ``explicit`` is True when at least one of the row's events carried
    a declared instance; a row built purely from the actor-name
    fallback is the pre-instance behavior wearing an instance label,
    and the UI says so rather than dressing it up.
    """
    rows = []
    for instance in dict.fromkeys(event_instance(e) for e in mine):
        own = [e for e in mine if event_instance(e) == instance]
        cs = cost_summary(own)
        outcome = ""
        for e in reversed(own):
            if e["type"] in ("run_finished", "run_end", "outcome"):
                outcome = str(e["payload"].get("result")
                              or e["payload"].get("outcome") or "")
                break
        rows.append({
            "instance": instance,
            "explicit": any("instance" in e for e in own),
            "agents": sorted({e["agent"] for e in own}),
            "traces": sorted({e["trace_id"] for e in own}),
            "events": len(own),
            "first_seq": own[0]["seq"], "last_seq": own[-1]["seq"],
            "first_ts": own[0]["ts"], "last_ts": own[-1]["ts"],
            "decisions": decision_tallies(own)["buckets"],
            "decisions_total": decision_tallies(own)["total"],
            "cost_usd": cs["total_usd"] if cs["spans"] else None,
            "outcome": outcome,
            "last_summary": summarize_event(own[-1]),
        })
    rows.sort(key=lambda r: r["first_seq"])
    return rows


def fleet_table(events: list[dict], *, now: float) -> list[dict]:
    """Tier 1 fleet table: one row per agent type, whole store.

    Status is fleet_status's categorical rule (blocked > running >
    idle) with its components beside it -- deliberately no blended
    score. Sparklines share the store's time window so rows compare.
    """
    fleet = fleet_status(events, now=now)
    pending = {p["source"] for p in pending_approvals(events)}
    window = _time_window(events)
    rows = []
    for f in fleet:
        source = f["source"]
        mine = query_events(events, source=source)
        cs = cost_summary(mine)
        tallies = decision_tallies(mine)
        if mine:
            headline, detail, tone = _node_story(
                source, mine, f["status"], source in pending)
        else:
            headline, detail, tone = "—", "no events yet", "idle"
        rows.append({
            "source": source, "agent": f["agent"], "status": f["status"],
            "events": len(mine),
            "instances": len({event_instance(e) for e in mine}),
            "has_instance_data": any("instance" in e for e in mine),
            "runs": len({e["trace_id"] for e in mine}),
            "traces": sorted({e["trace_id"] for e in mine}),
            "decisions": tallies["buckets"],
            "decisions_total": tallies["total"],
            "cost_usd": cs["total_usd"] if cs["spans"] else None,
            "cost_spans": cs["spans"],
            "sparkline": _rate_buckets(mine, window=window),
            "last_seq": f["last_seq"], "last_ts": f["last_ts"],
            "last_type": f["last_type"], "last_summary": f["last_summary"],
            "headline": headline, "detail": detail, "tone": tone,
        })
    return rows


def all_incidents(events: list[dict]) -> list[dict]:
    """Incidents across every trace in the store, in store order --
    the overview's needs-attention strip."""
    out: list[dict] = []
    for trace_id in dict.fromkeys(e["trace_id"] for e in events):
        out.extend(incidents(events, trace_id))
    out.sort(key=lambda i: i["seq"])
    return out


def overview(events: list[dict], *, now: float) -> dict:
    """Tier 1 admin overview payload, from the store alone."""
    tallies = decision_tallies(events)
    feed = [{
        "seq": e["seq"], "ts": e["ts"], "source": e["source"],
        "decision_point": e["payload"].get("decision_point") or e["type"],
        "choice": e["payload"].get("choice") or "",
        "summary": summarize_event(e),
    } for e in events if e["kind"] == "decision_record"]
    feed.sort(key=lambda d: -d["seq"])
    first_ts = min((e["ts"] for e in events), default=None)
    last_ts = max((e["ts"] for e in events), default=None)
    return {
        "events": len(events),
        "traces": sorted({e["trace_id"] for e in events}),
        "producers_active": len({e["source"] for e in events}),
        "decisions": tallies,
        "decision_feed": feed[:8],
        "cost": cost_summary(events),
        "cost_steps": cost_breakdown(events),
        "pending_approvals": pending_approvals(events),
        "window": {"first_ts": first_ts, "last_ts": last_ts},
        "fleet": fleet_table(events, now=now),
    }


def source_summary(events: list[dict], source: str, *, now: float) -> dict | None:
    """Tier 2 per-agent-type payload. None for a source that is
    neither known nor present in the store."""
    mine = query_events(events, source=source)
    if not mine and source not in KNOWN_SOURCES:
        return None
    fleet = {f["source"]: f for f in fleet_status(events, now=now)}
    f = fleet.get(source) or {"status": "idle"}
    pending = {p["source"] for p in pending_approvals(events)}
    if mine:
        headline, detail, tone = _node_story(source, mine, f["status"],
                                             source in pending)
    else:
        headline, detail, tone = "—", "no events yet", "idle"
    cs = cost_summary(mine)
    facets = {}
    for field in ("kind", "type", "trace_id", "agent"):
        counts: dict[str, int] = {}
        for e in mine:
            counts[e[field]] = counts.get(e[field], 0) + 1
        facets[field] = counts
    instance_counts: dict[str, int] = {}
    for e in mine:
        inst = event_instance(e)
        instance_counts[inst] = instance_counts.get(inst, 0) + 1
    facets["instance"] = instance_counts
    return {
        "source": source,
        "role": PRODUCER_ROLES.get(source, ""),
        "agents": sorted({e["agent"] for e in mine}),
        "status": f["status"],
        "headline": headline, "detail": detail, "tone": tone,
        "events": len(mine),
        "first_ts": mine[0]["ts"] if mine else None,
        "last_ts": mine[-1]["ts"] if mine else None,
        "decisions": decision_tallies(mine),
        "reasons": decision_reasons(mine),
        "outcomes": outcome_tallies(mine),
        "cost": {"spans": cs["spans"], "billable_steps": cs["billable_steps"],
                 "total_usd": cs["total_usd"] if cs["spans"] else None},
        "sparkline": _rate_buckets(mine),
        "facets": facets,
        "runs": _run_rows(events, source, mine),
        "instances": _instance_rows(mine),
        "has_instance_data": any("instance" in e for e in mine),
        "pending": [p for p in pending_approvals(events)
                    if p["source"] == source],
    }


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
        if p.get("decision_point") and p.get("decider") is not None \
                and p.get("computed") is not None:
            # Built-in bespoke shape: the original rendering, verbatim.
            return (f"DECISION {p.get('decision_point')}: {p.get('choice')} "
                    f"(decider={p.get('decider')}, "
                    f"computed={p.get('computed')})")
        # Generic shape (emit / file-drop): same headline, decider
        # from the emitting agent, evidence from the deciding values
        # in the payload -- absent fields are omitted, never None.
        d = normalize_decision(event)
        summary = (f"DECISION {d['decision_point']}: {d['choice']} "
                   f"(decider={d['decider']})")
        evidence = _evidence_summary(d["evidence"])
        if evidence:
            summary += f" evidence: {evidence}"
        return summary
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
