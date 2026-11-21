//! agenttrace-verify: standalone Rust verifier for AgentTrace stores.
//!
//! A bounded accent, not a replacement: the Python verifier
//! (`agenttrace.store.verify`) is primary. This binary exists so a
//! store can be checked with no Python runtime -- one static binary on
//! an assessor's machine -- and so large chains can be verified fast.
//!
//! It mirrors the Python verifier's contract exactly:
//!   * schema_version must be "1"; anything else fails LOUDLY (exit 2);
//!   * seq must be the 1-based line position, prev_hash must link to
//!     the previous event's hash (genesis = 64 zero hex chars);
//!   * each event's hash is SHA-256 over the canonical JSON of the
//!     event without its `hash` field, where canonical means Python's
//!     `json.dumps(obj, sort_keys=True, separators=(",", ":"))` with
//!     `ensure_ascii=True` -- the writer below reproduces that byte
//!     format, including \uXXXX escaping of non-ASCII;
//!   * on a break, it names the exact stored seq, like Python.
//!
//! Exit codes mirror the Python CLI: 0 = chain intact (or count done),
//! 1 = chain broken at the reported seq, 2 = usage / schema error.
//!
//! Usage:
//!   agenttrace-verify verify <store.jsonl>
//!   agenttrace-verify count  <store.jsonl>   # events by producer

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::process::ExitCode;

const GENESIS: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";
const SUPPORTED_VERSION: &str = "1";

// ---------------------------------------------------------------------
// Canonical JSON, byte-compatible with Python's
// json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
// ---------------------------------------------------------------------

fn write_canonical_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) > 0x7f => {
                // ensure_ascii: \uXXXX, with a surrogate pair above BMP.
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_canonical(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        // serde_json::Number prints integers exactly and floats in
        // shortest round-trip form, matching Python's repr for the
        // value shapes this schema carries (rates, costs, counts).
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_canonical_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        // serde_json's default Map is a BTreeMap: keys iterate in
        // sorted order, matching Python's sort_keys.
        Value::Object(map) => {
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical_string(k, out);
                out.push(':');
                write_canonical(val, out);
            }
            out.push('}');
        }
    }
}

fn event_hash(event_without_hash: &Value) -> String {
    let mut canonical = String::new();
    write_canonical(event_without_hash, &mut canonical);
    let digest = Sha256::digest(canonical.as_bytes());
    digest.iter().map(|b| format!("{:02x}", b)).collect()
}

// ---------------------------------------------------------------------
// Store reading + verification (mirrors agenttrace/store.py)
// ---------------------------------------------------------------------

enum Failure {
    Broken { seq: u64, detail: String },
    Schema(String),
}

fn read_lines(path: &str) -> Result<Vec<String>, Failure> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| Failure::Schema(format!("cannot read {path}: {e}")))?;
    Ok(text.lines().filter(|l| !l.trim().is_empty()).map(str::to_string).collect())
}

fn parse_event(line: &str, position: usize) -> Result<Value, Failure> {
    let entry: Value = serde_json::from_str(line).map_err(|e| {
        Failure::Schema(format!("line {position}: invalid JSON: {e}"))
    })?;
    let version = entry.get("schema_version").and_then(Value::as_str);
    if version != Some(SUPPORTED_VERSION) {
        return Err(Failure::Schema(format!(
            "unsupported schema_version {:?} (supported: [\"1\"]); refusing \
             to verify an event this build does not understand",
            version
        )));
    }
    Ok(entry)
}

fn verify(path: &str) -> Result<usize, Failure> {
    let lines = read_lines(path)?;
    let mut prev = GENESIS.to_string();
    for (i, line) in lines.iter().enumerate() {
        let position = (i + 1) as u64;
        let entry = parse_event(line, i + 1)?;
        let seq = entry.get("seq").and_then(Value::as_u64).unwrap_or(position);
        if seq != position {
            return Err(Failure::Broken {
                seq,
                detail: format!(
                    "seq discontinuity: position {position} holds seq {seq}"
                ),
            });
        }
        if entry.get("prev_hash").and_then(Value::as_str) != Some(prev.as_str()) {
            return Err(Failure::Broken {
                seq,
                detail: "prev_hash does not match the previous event's hash"
                    .to_string(),
            });
        }
        let mut body = entry.clone();
        let stored_hash = body
            .as_object_mut()
            .and_then(|o| o.remove("hash"))
            .and_then(|h| h.as_str().map(str::to_string));
        match stored_hash {
            Some(h) if event_hash(&body) == h => prev = h,
            _ => {
                return Err(Failure::Broken {
                    seq,
                    detail: "hash mismatch: stored event content was altered"
                        .to_string(),
                })
            }
        }
    }
    Ok(lines.len())
}

fn count_by_producer(path: &str) -> Result<BTreeMap<String, usize>, Failure> {
    let lines = read_lines(path)?;
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for (i, line) in lines.iter().enumerate() {
        let entry = parse_event(line, i + 1)?;
        let source = entry
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("<unknown>")
            .to_string();
        *counts.entry(source).or_insert(0) += 1;
    }
    Ok(counts)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let (mode, path) = match (args.get(1).map(String::as_str),
                              args.get(2).map(String::as_str)) {
        (Some("verify"), Some(p)) => ("verify", p),
        (Some("count"), Some(p)) => ("count", p),
        (Some(p), None) if !p.starts_with('-') => ("verify", p),
        _ => {
            eprintln!("usage: agenttrace-verify <verify|count> <store.jsonl>");
            return ExitCode::from(2);
        }
    };
    let result = if mode == "verify" {
        verify(path).map(|n| format!("Chain OK — {n} events, every hash verified."))
    } else {
        count_by_producer(path).map(|counts| {
            let total: usize = counts.values().sum();
            let mut out = String::new();
            for (source, n) in &counts {
                out.push_str(&format!("{source}: {n}\n"));
            }
            out.push_str(&format!("total: {total}"));
            out
        })
    };
    match result {
        Ok(msg) => {
            println!("{msg}");
            ExitCode::SUCCESS
        }
        Err(Failure::Broken { seq, detail }) => {
            println!("Chain BROKEN at seq {seq}: {detail}");
            ExitCode::from(1)
        }
        Err(Failure::Schema(msg)) => {
            eprintln!("schema error: {msg}");
            ExitCode::from(2)
        }
    }
}
