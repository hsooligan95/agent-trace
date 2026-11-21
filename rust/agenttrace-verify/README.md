# agenttrace-verify (Rust)

A standalone Rust CLI that verifies an AgentTrace store's hash chain —
and can count its events by producer — with no Python runtime. It is
the portfolio's bounded Rust accent: Rust belongs at the layer under
the agents (fast, portable, single-binary tooling an assessor can run
anywhere), while the Python verifier in `agenttrace/store.py` remains
**primary** and is the reference implementation of the chain contract.

```bash
cargo build --release
./target/release/agenttrace-verify verify <store.jsonl>
# Chain OK — 117 events, every hash verified.        (exit 0)
# Chain BROKEN at seq 8: hash mismatch: ...          (exit 1)

./target/release/agenttrace-verify count <store.jsonl>
# agent-canary: 2
# agent-gate: 7
# ...
# total: 117
```

The contract it mirrors, exactly: `schema_version` must be `"1"` or it
fails loudly (exit 2); `seq` is the 1-based position; `prev_hash`
links to the previous event's hash (genesis = 64 zero hex chars); each
hash is SHA-256 over the canonical JSON of the event without its
`hash` field, where canonical means Python's
`json.dumps(obj, sort_keys=True, separators=(",", ":"))` with
`ensure_ascii=True` — `src/main.rs` reproduces that byte format,
including `\uXXXX` escaping of non-ASCII, so the two verifiers agree
on the same stores, including naming the same broken seq on a tampered
store. That agreement is a test, not an assertion:
`tests/test_rust_verifier.py` in the parent repo builds this crate
(when cargo is available) and runs both verifiers over the sample
store and a tampered copy.

**Build status: VERIFIED (2026-11-21).** With rustc/cargo 1.100.0
installed via rustup, `cargo build --release` compiles the crate
clean, `tests/test_rust_verifier.py` passes with no skip, and the
binary agrees with the Python verifier on real stores: the flagship
drill store (`drill/output/trace-drill-45d404d0/store.jsonl`) verifies
**Chain OK — 117 events**, and its tampered sibling fails at exactly
**seq 8** ("hash mismatch: stored event content was altered") — the
same seq and the same detail the Python verifier reports. On a
machine without a Rust toolchain the agreement test still skips
(loudly, in its skip reason) rather than claiming a pass it didn't
run.
