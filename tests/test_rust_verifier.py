"""Rust/Python verifier agreement: the Rust accent must agree with
the primary Python verifier on every store these tests build --
intact stores verify, and a tampered store fails at exactly the same
seq in both. The crate is built and verified where a Rust toolchain
exists (2026-11-21, rustc/cargo 1.100.0); when no toolchain is
available the agreement run skips rather than claiming a pass it did
not run. The scaffold itself is still checked so it cannot silently
rot.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agenttrace import ingest, store

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "rust" / "agenttrace-verify"
SAMPLES = ROOT / "samples"

cargo = shutil.which("cargo")
needs_cargo = pytest.mark.skipif(
    cargo is None,
    reason="cargo not installed on this machine: the Rust agreement "
           "test cannot run here (the crate is verified on machines "
           "with a toolchain — see rust/agenttrace-verify/README.md)")


def test_rust_crate_scaffold_present():
    assert (CRATE / "Cargo.toml").exists()
    main_rs = CRATE / "src" / "main.rs"
    assert main_rs.exists()
    text = main_rs.read_text(encoding="utf-8")
    # The chain contract's load-bearing pieces are in the source.
    for token in ("schema_version", "prev_hash", "Sha256", "GENESIS"):
        assert token in text


def _sample_store(tmp_path):
    path = tmp_path / "store.jsonl"
    store.append_events(path, ingest.ingest_canary_verdict(SAMPLES / "canary-verdict.json"))
    store.append_events(path, ingest.ingest_gate_audit(SAMPLES / "gate-audit.jsonl"))
    store.append_events(path, ingest.ingest_blastradius_audit(SAMPLES / "blastradius-audit.jsonl"))
    return path


def _tampered_copy(path, tmp_path):
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["payload"]["tampered"] = True  # alter history, keep the hash
    lines[1] = json.dumps(entry)
    out = tmp_path / "tampered.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


@needs_cargo
def test_rust_verifier_agrees_with_python(tmp_path):
    build = subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(CRATE / "Cargo.toml")],
        capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, build.stderr
    binary = CRATE / "target" / "release" / "agenttrace-verify"

    path = _sample_store(tmp_path)
    py = store.verify(path)
    assert py.ok
    rs = subprocess.run([str(binary), "verify", str(path)],
                        capture_output=True, text=True)
    assert rs.returncode == 0, rs.stderr
    assert "Chain OK" in rs.stdout

    tampered = _tampered_copy(path, tmp_path)
    py_bad = store.verify(tampered)
    assert not py_bad.ok and py_bad.broken_seq == 2
    rs_bad = subprocess.run([str(binary), "verify", str(tampered)],
                            capture_output=True, text=True)
    assert rs_bad.returncode == 1
    assert f"seq {py_bad.broken_seq}" in rs_bad.stdout

    counts = subprocess.run([str(binary), "count", str(path)],
                            capture_output=True, text=True)
    assert counts.returncode == 0
    assert f"total: {py.count}" in counts.stdout
