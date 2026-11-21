"""Redaction at capture: scrub secret/PII patterns from event payloads
BEFORE they are written to the store.

The pattern set is ported from agent-gate's screening layer
(``agent-gate/src/screen.ts``) -- the same names, the same regexes, the
same replacement tokens for the PII/secret classes -- so a pattern name
means the same thing at the gateway and in the record. Two classes:

- **PII / secrets** (ssn, credit-card, aws-access-key, private-key,
  email, phone): gate redacts these in place; AgentTrace does the same
  on the way into the store.
- **Injection / exfil** (ignore-instructions, export-all-data,
  secret-exfil-url, ...): gate *flags* these and leaves the text for
  the agent-facing warning. In an audit store the calculus differs --
  the raw exfil URL or instruction string is itself the dangerous
  payload, and the store is meant to be handed to an assessor -- so
  AgentTrace scrubs the matched text too, replacing it with a token
  naming the pattern class.

Redaction is **visible, never silent**: a scrubbed event carries a
top-level ``redaction`` record -- the sorted pattern classes that fired
and the total match count. It never carries the redacted values, not
even excerpts. A clean event is returned unchanged (no ``redaction``
field, payload byte-identical), so redaction leaves no trace on events
that had nothing to hide.

Raw producer artifacts are never touched: redaction runs on the
in-memory events the ingestors produce, at the moment they enter the
store (``ingest`` output and ``store.append_events`` both pass through
here, idempotently).
"""

from __future__ import annotations

import copy
import re

# Ported from agent-gate/src/screen.ts INJECTION_PATTERNS.
INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore-instructions", re.compile(
        r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions|prompts|rules)", re.IGNORECASE)),
    ("disregard-instructions", re.compile(
        r"disregard\s+(all\s+)?(previous|prior|your)\s+"
        r"(instructions|rules|training)", re.IGNORECASE)),
    ("export-all-data", re.compile(
        r"(export|send|upload|forward|exfiltrate)\s+(all\s+)?(the\s+)?"
        r"(data|files|records|credentials|secrets)", re.IGNORECASE)),
    ("fake-system-turn", re.compile(
        r"(^|\n)\s*(system\s*:|<\s*system\s*>|\[system\])", re.IGNORECASE)),
    ("new-instructions", re.compile(
        r"your\s+(new\s+)?(instructions|task|orders)\s+(are|is)\s*:",
        re.IGNORECASE)),
    ("reveal-system-prompt", re.compile(
        r"(reveal|print|repeat|show)\s+(your\s+)?"
        r"(system\s+prompt|instructions|prompt)", re.IGNORECASE)),
    ("role-override", re.compile(
        r"you\s+are\s+now\s+(a|an|in)\b", re.IGNORECASE)),
    # Gate's detection pattern ends at the "="; for redaction the match
    # is extended to consume the whole URL, otherwise the secret value
    # after the "=" would survive the scrub.
    ("secret-exfil-url", re.compile(
        r"https?://[^\s\"']+\?[^\s\"']*(token|key|secret|data)=[^\s\"']*",
        re.IGNORECASE)),
]

# Ported from agent-gate/src/screen.ts PII_PATTERNS (same replacements).
PII_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    ("credit-card", re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"),
     "[REDACTED:CARD]"),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "[REDACTED:AWS-KEY]"),
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "[REDACTED:PRIVATE-KEY]"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "[REDACTED:EMAIL]"),
    ("phone", re.compile(r"\b(?:\+1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
     "[REDACTED:PHONE]"),
]

# Every pattern class this module can fire, by name.
PATTERN_NAMES = ([name for name, _ in INJECTION_PATTERNS]
                 + [name for name, _, _ in PII_PATTERNS])


def _scrub_text(text: str, fired: dict[str, int]) -> str:
    """Scrub one string, recording per-pattern match counts in ``fired``."""
    out = text
    # Exfil URLs first: the URL pattern consumes the whole URL (including
    # any email-shaped or key-shaped substring inside it), so it must
    # run before the narrower PII patterns carve it up piecemeal.
    for name, pattern in INJECTION_PATTERNS:
        def _sub(m, _name=name):
            fired[_name] = fired.get(_name, 0) + 1
            return f"[REDACTED:{_name.upper()}]"
        out = pattern.sub(_sub, out)
    for name, pattern, replacement in PII_PATTERNS:
        def _sub(m, _name=name, _repl=replacement):
            fired[_name] = fired.get(_name, 0) + 1
            return _repl
        out = pattern.sub(_sub, out)
    return out


def _scrub_value(value, fired: dict[str, int]):
    if isinstance(value, str):
        return _scrub_text(value, fired)
    if isinstance(value, dict):
        return {k: _scrub_value(v, fired) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, fired) for v in value]
    if isinstance(value, tuple):
        return [_scrub_value(v, fired) for v in value]
    return value


def redact_event(event: dict) -> dict:
    """Return the event with its payload scrubbed.

    If any pattern fired, the returned event carries
    ``redaction: {"patterns": [...], "matches": n}`` -- pattern class
    names and a count only, never a redacted value. If nothing fired,
    the event is returned unchanged (the same content, no flag), except
    that a pre-existing ``redaction`` record from an earlier pass is
    preserved. Idempotent: scrubbing an already-scrubbed event changes
    nothing and does not double-count.
    """
    fired: dict[str, int] = {}
    payload = _scrub_value(event.get("payload", {}), fired)
    if not fired:
        return event
    out = copy.deepcopy(event)
    out["payload"] = payload
    prior = event.get("redaction") or {}
    patterns = sorted(set(prior.get("patterns", [])) | set(fired))
    out["redaction"] = {
        "patterns": patterns,
        "matches": int(prior.get("matches", 0)) + sum(fired.values()),
    }
    return out


def redact_events(events: list[dict]) -> list[dict]:
    """Redact a list of pre-store events (see redact_event)."""
    return [redact_event(e) for e in events]
