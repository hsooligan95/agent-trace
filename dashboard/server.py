#!/usr/bin/env python3
"""Read-only dashboard server for the AgentTrace store.

Stdlib only. Serves one static page and a read-only JSON API whose
entire payload is built from the store file through the same query
layer the CLI uses (agenttrace.query) plus the store's own verifier
(agenttrace.store) and replay engine (agenttrace.replay). The
incident endpoints (/api/incidents, /api/incident?seq=N) are derived
the same way: incidents from the query layer, per-step hash
consistency recomputed from the chain, state before/after from
TraceReplay. The slice-1 tier endpoints (/api/overview, /api/agent,
/api/agent-events, /api/walk) are the same pattern -- query-layer
derivations over the store, nothing else. Slice 2 adds the
generalized replay (/api/replay: a step-through over any scope of a
trace, per-step state from TraceReplay, anomaly classes from the
query layer) and trace compare (/api/compare: TraceReplay's
diff_traces over two traces of the store). The server holds no state
of its own, opens no
producer artifact, and rejects every non-GET request with 405: there
is no approve / pause / kill / config surface here, by design.

Usage:
    python -m dashboard.server --store drill/output/<trace>/store.jsonl
    python -m dashboard.server --store /path/to/any-store.jsonl --port 8765
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttrace import query as query_mod  # noqa: E402
from agenttrace import replay as replay_mod  # noqa: E402
from agenttrace import store as store_mod  # noqa: E402
from agenttrace.schema import validate_event  # noqa: E402

HERE = Path(__file__).resolve().parent
STREAM_CAP = 150
TREE_CAP_PER_BRANCH = 400


def load_store_events(path: Path) -> list[dict]:
    """Load stored events, tolerating one incomplete trailing line.

    The dashboard tails a store that a live run is appending to; a
    final line caught mid-write is dropped for this read and picked up
    complete on the next poll. Any other malformed line raises --
    loudly, like everywhere else in AgentTrace.
    """
    if not path.exists():
        return []
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = []
    for i, line in enumerate(lines):
        try:
            events.append(validate_event(json.loads(line), stored=True))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return events


def chain_status(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {"status": "empty", "count": 0, "broken_seq": None, "detail": "store is empty"}
    try:
        result = store_mod.verify(path)
    except Exception as exc:  # mid-append read or schema error: report, don't crash
        return {"status": "error", "count": 0, "broken_seq": None, "detail": str(exc)}
    if result.ok:
        return {"status": "ok", "count": result.count, "broken_seq": None,
                "detail": "every hash verified"}
    return {"status": "broken", "count": result.count,
            "broken_seq": result.broken_seq, "detail": result.detail}


def build_state(path: Path) -> dict:
    """The full dashboard payload, from the store alone."""
    events = load_store_events(path)
    chain = chain_status(path)
    traces = sorted({e["trace_id"] for e in events})
    active_trace = events[-1]["trace_id"] if events else None
    tree = []
    if active_trace:
        tree = query_mod.trace_tree(events, active_trace)
        for branch in tree:
            branch["truncated"] = len(branch["events"]) > TREE_CAP_PER_BRANCH
            branch["events"] = branch["events"][-TREE_CAP_PER_BRANCH:]
    stream = [{
        "seq": e["seq"], "ts": e["ts"], "source": e["source"],
        "agent": e["agent"], "type": e["type"], "kind": e["kind"],
        "summary": query_mod.summarize_event(e),
    } for e in events[-STREAM_CAP:]][::-1]
    return {
        "store": path.name,
        "server_time": time.time(),
        "events": len(events),
        "chain": chain,
        "traces": traces,
        "active_trace": active_trace,
        "fleet": query_mod.fleet_status(events, now=time.time()),
        "pending_approvals": query_mod.pending_approvals(events),
        "cost": query_mod.cost_summary(events),
        "tree": tree,
        "stream": stream,
        # Hybrid page view-models (fleet graph + beat ribbon + cards):
        # same query layer, same store, nothing server-side.
        "nodes": query_mod.graph_nodes(events, active_trace, now=time.time())
                 if active_trace else [],
        "beats": query_mod.drill_progress(events, active_trace)
                 if active_trace else {"beats": [], "complete": False},
        "decisions": query_mod.decision_records(events, active_trace)
                     if active_trace else [],
        "incidents": query_mod.incidents(events, active_trace)
                     if active_trace else [],
        "cost_steps": query_mod.cost_breakdown(events),
    }


def build_events_after(path: Path, after_seq: int) -> dict:
    """Incremental poll payload: events newer than after_seq + chain."""
    events = load_store_events(path)
    fresh = query_mod.query_events(events)
    fresh = [e for e in fresh if e["seq"] > after_seq]
    return {
        "events": [{
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "agent": e["agent"], "type": e["type"], "kind": e["kind"],
            "summary": query_mod.summarize_event(e),
        } for e in fresh],
        "count": len(events),
        "chain": chain_status(path),
    }


def chain_flags(events: list[dict]) -> dict[int, bool]:
    """Per-seq hash-consistency flags, recomputed from genesis.

    An event is flagged True only if its seq position, prev_hash
    linkage, and recomputed hash all check out *and* every earlier
    event did too: past the first break, nothing is trusted. This is
    an internal-consistency check on the store copy being served --
    it proves the record was not altered relative to itself, not who
    wrote it.
    """
    flags: dict[int, bool] = {}
    prev = store_mod.GENESIS
    ok = True
    for pos, e in enumerate(events, start=1):
        if ok:
            body = {k: v for k, v in e.items() if k != "hash"}
            ok = (e.get("seq") == pos and e.get("prev_hash") == prev
                  and store_mod.event_hash(body) == e.get("hash"))
        flags[e["seq"]] = ok
        prev = e.get("hash")
    return flags


def _short(value, limit: int = 300) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def payload_diff(prev: dict | None, cur: dict) -> list[dict] | None:
    """Field-level diff of two payloads: added / removed / changed
    keys with before/after values. None for the first step of a
    window (nothing before it to diff against)."""
    if prev is None:
        return None
    out = []
    for key in sorted(set(prev) | set(cur)):
        if key not in prev:
            out.append({"key": key, "change": "added",
                        "before": None, "after": _short(cur[key])})
        elif key not in cur:
            out.append({"key": key, "change": "removed",
                        "before": _short(prev[key]), "after": None})
        elif prev[key] != cur[key]:
            out.append({"key": key, "change": "changed",
                        "before": _short(prev[key]),
                        "after": _short(cur[key])})
    return out


def _compact_state(state: dict) -> dict:
    """The incident view's state summary: counts, decisions so far,
    cost so far, per-source counts -- from TraceReplay's state_at."""
    return {
        "events": state["events"],
        "last_seq": state["last_seq"],
        "cost_usd": state["cost"]["total_usd"],
        "decisions": [{"seq": d["seq"],
                       "decision_point": d["decision_point"],
                       "choice": d["choice"]} for d in state["decisions"]],
        "sources": state["sources"],
    }


def build_incidents(path: Path) -> dict:
    """Incident list for the store's active trace."""
    events = load_store_events(path)
    active_trace = events[-1]["trace_id"] if events else None
    return {
        "trace_id": active_trace,
        "incidents": query_mod.incidents(events, active_trace)
                     if active_trace else [],
    }


def build_incident_detail(path: Path, anchor_seq: int,
                          trace_id: str | None = None) -> dict | None:
    """One incident, in full: the anchor record, its causal window as
    a step-through (payload + diff vs the previous step + per-event
    hash consistency per step), and the TraceReplay state immediately
    before and after the anchor event. None if no incident in the
    trace is anchored at anchor_seq."""
    events = load_store_events(path)
    if not events:
        return None
    trace_id = trace_id or events[-1]["trace_id"]
    incident = next((i for i in query_mod.incidents(events, trace_id)
                     if i["seq"] == anchor_seq), None)
    if incident is None:
        return None
    lo, hi = incident["window"]["from_seq"], incident["window"]["to_seq"]
    window = [e for e in query_mod.query_events(events, trace_id=trace_id)
              if lo <= e["seq"] <= hi]
    flags = chain_flags(events)
    steps = []
    prev_payload = None
    for e in window:
        steps.append({
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "agent": e["agent"], "type": e["type"], "kind": e["kind"],
            "summary": query_mod.summarize_event(e),
            "payload": e["payload"],
            "diff": payload_diff(prev_payload, e["payload"]),
            "verified": flags.get(e["seq"], False),
            "is_anchor": e["seq"] == anchor_seq,
        })
        prev_payload = e["payload"]
    anchor = next(e for e in window if e["seq"] == anchor_seq)
    return {
        "incident": incident,
        "trace_id": trace_id,
        "anchor": {
            "seq": anchor["seq"], "ts": anchor["ts"],
            "source": anchor["source"], "agent": anchor["agent"],
            "type": anchor["type"], "kind": anchor["kind"],
            "payload": anchor["payload"],
        },
        "steps": steps,
        "first_unverified_seq": next(
            (s["seq"] for s in steps if not s["verified"]), None),
        "state_before": _compact_state(replay_mod.state_at(
            events, trace_id, upto_seq=anchor_seq - 1)),
        "state_after": _compact_state(replay_mod.state_at(
            events, trace_id, upto_seq=anchor_seq)),
    }


def build_overview(path: Path) -> dict:
    """Tier 1 (admin overview) payload: needs-attention incidents
    across all traces, chain consistency + head, system tiles with
    visible components, the fleet table, the decision feed. Derived
    from the store alone, like everything here."""
    events = load_store_events(path)
    head = events[-1] if events else None
    return {
        "store": path.name,
        "server_time": time.time(),
        "chain": chain_status(path),
        "head": {"seq": head["seq"], "hash": head["hash"]} if head else None,
        "incidents": query_mod.all_incidents(events),
        **query_mod.overview(events, now=time.time()),
    }


def build_agent(path: Path, source: str) -> dict | None:
    """Tier 2 (per-agent-type) payload, or None for an unknown source."""
    events = load_store_events(path)
    summary = query_mod.source_summary(events, source, now=time.time())
    if summary is None:
        return None
    summary["chain"] = chain_status(path)
    return summary


def build_agent_events(path: Path, source: str, *, kind: str | None = None,
                       type: str | None = None, trace_id: str | None = None,
                       decisions_only: bool = False,
                       limit: int = 150) -> dict:
    """Tier 2 faceted event stream for one source: newest first,
    capped, with the matched total so the cap is never silent."""
    events = load_store_events(path)
    mine = query_mod.query_events(events, source=source, kind=kind,
                                  type=type, trace_id=trace_id)
    if decisions_only:
        mine = [e for e in mine if e["kind"] == "decision_record"]
    total = len(mine)
    rows = [{
        "seq": e["seq"], "ts": e["ts"], "trace_id": e["trace_id"],
        "agent": e["agent"], "type": e["type"], "kind": e["kind"],
        "summary": query_mod.summarize_event(e),
    } for e in mine[-limit:]][::-1]
    return {"source": source, "total": total, "shown": len(rows),
            "events": rows}


# Event types that close out a producer's run -- the walk's anchor
# when present, else the run's last event.
_WALK_TERMINAL_TYPES = ("run_finished", "compare_verdict", "chaos_report",
                        "outcome", "run_end")


def build_walk(path: Path, source: str,
               trace_id: str | None = None) -> dict | None:
    """A step-through over one run of one source in one trace -- the
    incident view's machinery, scoped to a run (slice 1's reuse;
    slice 2 generalizes replay). Same shape as build_incident_detail:
    steps with payload + diff + per-step hash consistency, and the
    TraceReplay state before/after the anchor. None when the source
    has no events in the trace."""
    events = load_store_events(path)
    if not events:
        return None
    trace_id = trace_id or events[-1]["trace_id"]
    mine = query_mod.query_events(events, trace_id=trace_id, source=source)
    if not mine:
        return None
    anchor = next((e for e in reversed(mine)
                   if e["type"] in _WALK_TERMINAL_TYPES), mine[-1])
    flags = chain_flags(events)
    steps = []
    prev_payload = None
    for e in mine:
        steps.append({
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "agent": e["agent"], "type": e["type"], "kind": e["kind"],
            "summary": query_mod.summarize_event(e),
            "payload": e["payload"],
            "diff": payload_diff(prev_payload, e["payload"]),
            "verified": flags.get(e["seq"], False),
            "is_anchor": e["seq"] == anchor["seq"],
        })
        prev_payload = e["payload"]
    tallies = query_mod.decision_tallies(mine)
    b = tallies["buckets"]
    why = [{"seq": mine[0]["seq"], "text":
            f"{source} in {trace_id}: {len(mine)} events, "
            f"#{mine[0]['seq']}–#{mine[-1]['seq']}."}]
    if tallies["total"] or b["rollback"]:
        why.append({"seq": anchor["seq"], "text":
                    f"Decisions in this run: {tallies['total']} computed "
                    f"record(s) — allow {b['allow']} · block {b['block']} · "
                    f"rollback {b['rollback']} · other {b['other']}."})
    for r in query_mod.decision_reasons(mine, limit=4):
        why.append({"seq": anchor["seq"],
                    "text": f"{r['label']} (×{r['count']})"})
    incident = {
        "id": f"walk-{source}-{anchor['seq']}",
        "kind": "run",
        "trace_id": trace_id,
        "source": source,
        "agent": anchor["agent"],
        "seq": anchor["seq"],
        "ts": anchor["ts"],
        "title": f"{source} — run in {trace_id}",
        "summary": query_mod.summarize_event(anchor),
        "window": {"from_seq": mine[0]["seq"], "to_seq": mine[-1]["seq"]},
        "why": why,
        "cost_usd": (query_mod.cost_summary(mine)["total_usd"]
                     if source == "agent-ledger"
                     and query_mod.cost_summary(mine)["spans"] else None),
    }
    return {
        "incident": incident,
        "trace_id": trace_id,
        "anchor": {
            "seq": anchor["seq"], "ts": anchor["ts"],
            "source": anchor["source"], "agent": anchor["agent"],
            "type": anchor["type"], "kind": anchor["kind"],
            "payload": anchor["payload"],
        },
        "steps": steps,
        "first_unverified_seq": next(
            (s["seq"] for s in steps if not s["verified"]), None),
        "state_before": _compact_state(replay_mod.state_at(
            events, trace_id, upto_seq=anchor["seq"] - 1)),
        "state_after": _compact_state(replay_mod.state_at(
            events, trace_id, upto_seq=anchor["seq"])),
    }


def build_replay(path: Path, trace_id: str | None = None, *,
                 from_seq: int | None = None,
                 to_seq: int | None = None) -> dict | None:
    """Generalized replay (slice 2): a step-through over any scope of
    one trace -- the whole trace, or a seq window inside it. Every
    step carries its payload, its diff vs the previous step *in
    scope*, its per-event hash consistency, its anomaly class (query
    layer), and the TraceReplay state immediately after it. Source
    and decisions-only filtering are display concerns the client
    applies over these steps -- the counts it needs ride along -- so
    diffs and states never depend on what is filtered out of view.
    None for an unknown trace; a window outside the trace is a
    valid, empty replay."""
    events = load_store_events(path)
    if not events:
        return None
    traces = sorted({e["trace_id"] for e in events})
    trace_id = trace_id or events[-1]["trace_id"]
    mine = query_mod.query_events(events, trace_id=trace_id)
    if not mine:
        return None
    lo = from_seq if from_seq is not None else mine[0]["seq"]
    hi = to_seq if to_seq is not None else mine[-1]["seq"]
    window = [e for e in mine if lo <= e["seq"] <= hi]
    flags = chain_flags(events)
    steps = []
    prev_payload = None
    for e in window:
        steps.append({
            "seq": e["seq"], "ts": e["ts"], "source": e["source"],
            "agent": e["agent"], "type": e["type"], "kind": e["kind"],
            "summary": query_mod.summarize_event(e),
            "payload": e["payload"],
            "diff": payload_diff(prev_payload, e["payload"]),
            "verified": flags.get(e["seq"], False),
            "anomaly": query_mod.anomaly_kind(e),
            "state": _compact_state(replay_mod.state_at(
                events, trace_id, upto_seq=e["seq"])),
        })
        prev_payload = e["payload"]
    anomalies: dict[str, int] = {}
    for s in steps:
        if s["anomaly"]:
            anomalies[s["anomaly"]] = anomalies.get(s["anomaly"], 0) + 1
    return {
        "trace_id": trace_id,
        "traces": traces,
        "scope": {"from_seq": lo, "to_seq": hi,
                  "trace_from_seq": mine[0]["seq"],
                  "trace_to_seq": mine[-1]["seq"],
                  "trace_events": len(mine)},
        "sources": dict(Counter(e["source"] for e in window)),
        "anomalies": anomalies,
        "steps": steps,
        "first_unverified_seq": next(
            (s["seq"] for s in steps if not s["verified"]), None),
    }


def build_compare(path: Path, trace_a: str, trace_b: str) -> dict | None:
    """Trace compare (slice 2): TraceReplay's diff_traces over two
    traces of the served store -- event classes, decision choices,
    and cost. Timestamps, seq numbers, and hashes are identity, not
    behavior, and the engine excludes them, so two honest re-runs of
    the same drill diff clean. None when either trace is absent."""
    events = load_store_events(path)
    present = {e["trace_id"] for e in events}
    if trace_a not in present or trace_b not in present:
        return None
    return replay_mod.diff_traces(events, trace_a, events, trace_b)


class DashboardHandler(BaseHTTPRequestHandler):
    store_path: Path = Path("store.jsonl")
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(200, (HERE / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json(200, build_state(self.store_path))
            elif parsed.path == "/api/events":
                qs = parse_qs(parsed.query)
                after = int(qs.get("after_seq", ["0"])[0])
                self._json(200, build_events_after(self.store_path, after))
            elif parsed.path == "/api/overview":
                self._json(200, build_overview(self.store_path))
            elif parsed.path == "/api/agent":
                qs = parse_qs(parsed.query)
                source = qs.get("source", [""])[0]
                payload = build_agent(self.store_path, source) if source else None
                if payload is None:
                    self._json(404, {"error": "no such agent type"})
                else:
                    self._json(200, payload)
            elif parsed.path == "/api/agent-events":
                qs = parse_qs(parsed.query)
                source = qs.get("source", [""])[0]
                if not source:
                    self._json(400, {"error": "source is required"})
                else:
                    try:
                        limit = max(1, min(500, int(
                            qs.get("limit", ["150"])[0])))
                    except ValueError:
                        limit = 150
                    self._json(200, build_agent_events(
                        self.store_path, source,
                        kind=qs.get("kind", [None])[0] or None,
                        type=qs.get("type", [None])[0] or None,
                        trace_id=qs.get("trace", [None])[0] or None,
                        decisions_only=qs.get("decisions", ["0"])[0] == "1",
                        limit=limit))
            elif parsed.path == "/api/walk":
                qs = parse_qs(parsed.query)
                source = qs.get("source", [""])[0]
                walk = build_walk(self.store_path, source,
                                  qs.get("trace", [None])[0]) if source else None
                if walk is None:
                    self._json(404, {"error": "no such run"})
                else:
                    self._json(200, walk)
            elif parsed.path == "/api/replay":
                qs = parse_qs(parsed.query)

                def _int_or_none(name):
                    raw = qs.get(name, [None])[0]
                    try:
                        return int(raw) if raw is not None else None
                    except ValueError:
                        return None

                payload = build_replay(
                    self.store_path, qs.get("trace", [None])[0],
                    from_seq=_int_or_none("from"),
                    to_seq=_int_or_none("to"))
                if payload is None:
                    self._json(404, {"error": "no such trace"})
                else:
                    self._json(200, payload)
            elif parsed.path == "/api/compare":
                qs = parse_qs(parsed.query)
                trace_a = qs.get("trace_a", [""])[0]
                trace_b = qs.get("trace_b", [""])[0]
                payload = build_compare(self.store_path, trace_a, trace_b) \
                    if trace_a and trace_b else None
                if payload is None:
                    self._json(404, {"error": "no such trace pair"})
                else:
                    self._json(200, payload)
            elif parsed.path == "/api/incidents":
                self._json(200, build_incidents(self.store_path))
            elif parsed.path == "/api/incident":
                qs = parse_qs(parsed.query)
                try:
                    seq = int(qs.get("seq", [""])[0])
                except ValueError:
                    seq = -1
                detail = build_incident_detail(
                    self.store_path, seq,
                    qs.get("trace", [None])[0]) if seq >= 0 else None
                if detail is None:
                    self._json(404, {"error": "no such incident"})
                else:
                    self._json(200, detail)
            elif parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _reject(self) -> None:
        self._json(405, {"error": "read-only dashboard: only GET is supported; "
                                  "approvals are resolved at the producer, never here"})

    do_POST = _reject
    do_PUT = _reject
    do_DELETE = _reject
    do_PATCH = _reject
    do_OPTIONS = _reject
    do_HEAD = _reject


def make_server(store_path: str | Path, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundDashboardHandler", (DashboardHandler,),
                   {"store_path": Path(store_path)})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.server", description=__doc__)
    parser.add_argument("--store", required=True, help="AgentTrace store JSONL to view")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.store, args.host, args.port)
    print(f"AgentTrace dashboard (read-only) -> http://{args.host}:{args.port}/ "
          f"store={args.store}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
