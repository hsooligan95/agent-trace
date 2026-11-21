"""One-page tree/timeline renderer -- reads ONLY the store.

The renderer's input is a list of stored events (plus, optionally, the
chain verification result). It never opens a producer's files: if the
store cannot tell the story alone, the story does not get told. That
constraint is the point -- this page is what an auditor gets.
"""

from __future__ import annotations

from .query import summarize_event
from .store import VerifyResult


def render_timeline(events: list[dict], *, verify_result: VerifyResult | None = None,
                    title: str = "AgentTrace — one trace, every producer") -> str:
    trace_ids = sorted({e["trace_id"] for e in events})
    sources = sorted({e["source"] for e in events})
    decisions = [e for e in events if e["kind"] == "decision_record"]
    lines = [f"# {title}", ""]
    lines.append(f"Trace ID(s): {', '.join(f'`{t}`' for t in trace_ids)} · "
                 f"{len(events)} events · sources: {', '.join(sources)}")
    if verify_result is not None:
        if verify_result.ok:
            lines.append(f"**Chain OK — {verify_result.count} events, every hash verified.**")
        else:
            lines.append(f"**Chain BROKEN at seq {verify_result.broken_seq}** "
                         f"({verify_result.detail})")
    lines.append("")
    lines.append("```")
    for trace_id in trace_ids:
        lines.append(trace_id)
        for source in sources:
            branch = [e for e in events
                      if e["trace_id"] == trace_id and e["source"] == source]
            if not branch:
                continue
            lines.append(f"├── {source} ({len(branch)} events)")
            for e in branch:
                detail = summarize_event(e)
                lines.append(f"│   #{e['seq']:>3} {e['ts']}  {e['type']:<18} {detail}")
    lines.append("```")
    if decisions:
        lines += ["", "## Decision records (computed decision points only)", ""]
        for e in decisions:
            p = e["payload"]
            lines.append(f"- #{e['seq']} **{p['decision_point']}** -> **{p['choice']}**")
            evidence = p.get("evidence", {})
            if evidence.get("reasons"):
                for reason in evidence["reasons"]:
                    lines.append(f"  - {reason}")
            elif evidence.get("patterns"):
                lines.append(f"  - patterns: {', '.join(evidence['patterns'])}")
            elif evidence.get("reason"):
                lines.append(f"  - {evidence['reason']}")
    lines += ["", "## Timeline (store order)", ""]
    for e in events:
        lines.append(f"- #{e['seq']:>3} `{e['ts']}` **{e['source']}** — "
                     f"{e['type']} — {summarize_event(e)}")
    lines.append("")
    return "\n".join(lines)
