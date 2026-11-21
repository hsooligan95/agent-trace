"""Decision records -- extracted only from computed decision points.

A decision record is trustworthy only if it is written by the code
branch that made the decision, at decision time, from the values that
decided it. In this fleet, exactly two producers meet that bar today:

- agent-canary's compare verdict (PROMOTE / BLOCK, computed from the
  golden-suite diff: success rates, forbidden-action counts, thresholds);
- agent-gate's policy decisions and screening findings (allow / deny /
  approval-required and injection/PII pattern hits, computed by the
  gateway's policy engine and screeners);
- factory-floor-agent's groundedness check (answer / abstain, computed
  from per-claim overlap scores against a fixed threshold — the values
  are the checker's own, emitted by the agent as data).

These extractors re-shape those producers' own emitted outputs -- every
field below is copied or computed from the producer's artifact, never
narrated. Anything an LLM says about its own reasoning would be a
``rationale_annotation`` instead; no producer in this phase emits one,
and these extractors never fabricate one.
"""

from __future__ import annotations

from .schema import KIND_DECISION_RECORD, new_event


def canary_decision_events(verdict: dict, *, trace_id: str) -> list[dict]:
    """One decision_record event from a canary compare verdict dict."""
    base = verdict.get("base_aggregate", {})
    cand = verdict.get("cand_aggregate", {})
    record = {
        "decision_point": "agent-canary.compare",
        "decider": "agent-canary",
        "computed": True,
        "choice": verdict["decision"],
        "options": ["PROMOTE", "BLOCK"],
        "evidence": {
            "reasons": verdict.get("reasons", []),
            "base_version": verdict.get("base_version"),
            "candidate_version": verdict.get("candidate_version"),
            "base_success_rate": base.get("success_rate"),
            "candidate_success_rate": cand.get("success_rate"),
            "success_drop": verdict.get("success_drop"),
            "candidate_forbidden_hits": cand.get("forbidden_hits"),
            "cost_delta_pct": verdict.get("cost_delta_pct"),
            "latency_delta_pct": verdict.get("latency_delta_pct"),
        },
    }
    return [new_event(
        trace_id=trace_id,
        ts=verdict["generated_at"],
        source="agent-canary",
        agent="agent-canary",
        type="decision_record",
        kind=KIND_DECISION_RECORD,
        payload=record,
    )]


def gate_decision_events(entry: dict, *, trace_id: str) -> list[dict]:
    """Decision records from one agent-gate audit entry (0 or 1 records).

    Only ``policy_decision`` and ``screening_findings`` entries are
    computed decision points; session/tool bookkeeping entries yield no
    record.
    """
    etype = entry.get("type")
    if etype == "policy_decision":
        record = {
            "decision_point": "agent-gate.policy",
            "decider": "agent-gate",
            "computed": True,
            "choice": entry.get("effect"),
            "options": ["allow", "deny", "require-approval"],
            "evidence": {
                "tool": entry.get("tool"),
                "stage": entry.get("stage"),
                "reason": entry.get("reason"),
                "call_id": entry.get("callId"),
            },
        }
    elif etype == "screening_findings":
        findings = entry.get("findings", [])
        record = {
            "decision_point": "agent-gate.screening",
            "decider": "agent-gate",
            "computed": True,
            "choice": "flagged",
            "options": ["clean", "flagged"],
            "evidence": {
                "tool": entry.get("tool"),
                "call_id": entry.get("callId"),
                "patterns": [f.get("pattern") for f in findings],
                "kinds": sorted({f.get("kind") for f in findings}),
            },
        }
    else:
        return []
    return [new_event(
        trace_id=trace_id,
        ts=entry["ts"],
        source="agent-gate",
        agent=entry.get("agent", "agent-gate"),
        type="decision_record",
        kind=KIND_DECISION_RECORD,
        payload=record,
    )]


def factory_floor_decision_events(groundedness_payload: dict, *, trace_id: str,
                                  ts: str) -> list[dict]:
    """One decision_record from a factory-floor groundedness event.

    The groundedness check is a computed decision point: the checker
    scored every answer claim's overlap against the retrieved passages
    and compared it to a fixed threshold; the answer/abstain outcome
    follows mechanically. This extractor reshapes the checker's own
    emitted numbers — it adds no narration, and the agent's record
    carries no "options considered" claim (the agent enumerates none).
    """
    claims = groundedness_payload.get("claims", [])
    grounded = bool(groundedness_payload.get("grounded"))
    record = {
        "decision_point": "factory-floor-agent.groundedness",
        "decider": "factory-floor-agent",
        "computed": True,
        "choice": "answered" if grounded else "abstained",
        "options": ["answered", "abstained"],
        "evidence": {
            "grounded": grounded,
            "threshold": groundedness_payload.get("threshold"),
            "attempts": groundedness_payload.get("attempts"),
            "claims_checked": len(claims),
            "claims_grounded": sum(1 for c in claims if c.get("grounded")),
            "min_claim_overlap": min((c["overlap"] for c in claims), default=None),
            "claims": claims,
        },
    }
    return [new_event(
        trace_id=trace_id,
        ts=ts,
        source="factory-floor-agent",
        agent="factory-floor-agent",
        type="decision_record",
        kind=KIND_DECISION_RECORD,
        payload=record,
    )]
