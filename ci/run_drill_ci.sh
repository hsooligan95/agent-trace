#!/usr/bin/env bash
# Nightly platform drill -- CI glue.
#
# Reproduces, in a fresh checkout layout, exactly what the local drill
# expects: the seven repos side by side in one "portfolio" directory
# (agent-trace plus its six producers), one .venv per Python repo,
# agent-gate's node_modules + dist built, then drill/run_drill.py run
# UNMODIFIED, then ci/make_status.py re-verifying the produced store
# cold and writing ci/last-drill.json + ci/last-drill-timeline.md.
#
# The workflow (.github/workflows/nightly-drill.yml) only checks the
# repos out and calls this script; the same script is what the local
# simulation runs, so what is tested locally is what CI executes.
#
# Layout contract (identical locally and in CI):
#   <portfolio>/
#     agent-trace/        <- this repo (REPO); PORTFOLIO is its parent
#     agent-canary/  agent-gate/  blast-radius/
#     factory-floor-agent/  agent-ledger/  agent-chaos/
#
# Idempotent: a repo whose .venv carries the .ci-deps stamp is not
# reinstalled, so local re-runs are cheap. CI runners start empty, so
# every install runs there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PORTFOLIO="$(cd "$REPO/.." && pwd)"
PY="${PYTHON:-python3}"
STAMP=".ci-deps-stamp"

log() { printf '\n=== %s ===\n' "$*"; }

# venv_for <repo-dir> <install-command...>
# Creates the repo's .venv if absent and runs the install command
# (with the venv's pip) unless a previous run stamped it done.
venv_for() {
  local dir="$1"; shift
  if [ ! -x "$dir/.venv/bin/python" ]; then
    "$PY" -m venv "$dir/.venv"
  fi
  if [ -f "$dir/.venv/$STAMP" ]; then
    echo "deps already installed for ${dir##*/} (stamp present, skipping)"
    return 0
  fi
  "$dir/.venv/bin/pip" install --quiet --upgrade pip
  ( cd "$dir" && "$@" )
  touch "$dir/.venv/$STAMP"
}

log "layout"
echo "agent-trace repo : $REPO"
echo "portfolio dir    : $PORTFOLIO"
for r in agent-canary agent-gate blast-radius factory-floor-agent agent-ledger agent-chaos; do
  [ -d "$PORTFOLIO/$r" ] || { echo "MISSING sibling repo: $PORTFOLIO/$r" >&2; exit 2; }
done
"$PY" --version
node --version

log "agent-trace venv (+ blast-radius frozen requirements: boto3, moto[server])"
# The drill runs blast-radius and the moto server with THIS venv
# (PYTHONPATH points at the blast-radius checkout), so its frozen
# requirements are installed here -- same as the local setup.
venv_for "$REPO" "$REPO/.venv/bin/pip" install --quiet \
  -r "$PORTFOLIO/blast-radius/requirements.txt"

log "agent-canary venv (editable install; pulls langgraph)"
venv_for "$PORTFOLIO/agent-canary" "$PORTFOLIO/agent-canary/.venv/bin/pip" \
  install --quiet -e .

log "factory-floor-agent venv"
venv_for "$PORTFOLIO/factory-floor-agent" \
  "$PORTFOLIO/factory-floor-agent/.venv/bin/pip" install --quiet \
  -r "$PORTFOLIO/factory-floor-agent/requirements.txt"

log "agent-ledger venv (pinned requirements)"
venv_for "$PORTFOLIO/agent-ledger" "$PORTFOLIO/agent-ledger/.venv/bin/pip" \
  install --quiet -r "$PORTFOLIO/agent-ledger/requirements.txt"

log "agent-chaos venv (editable install; stdlib-only runtime)"
venv_for "$PORTFOLIO/agent-chaos" "$PORTFOLIO/agent-chaos/.venv/bin/pip" \
  install --quiet -e .

log "agent-gate (npm ci + tsc build; dist/ is not committed)"
GATE="$PORTFOLIO/agent-gate"
if [ ! -d "$GATE/node_modules" ]; then
  ( cd "$GATE" && npm ci --no-audit --no-fund )
fi
if [ ! -f "$GATE/dist/src/index.js" ]; then
  ( cd "$GATE" && npm run build )
fi

log "platform drill (drill/run_drill.py, unmodified)"
mkdir -p "$REPO/drill/output"
"$REPO/.venv/bin/python" "$REPO/drill/run_drill.py" 2>&1 | tee "$REPO/drill/output/drill-run.log"

OUT_DIR="$(ls -dt "$REPO"/drill/output/trace-drill-* | head -1)"
echo "drill output: $OUT_DIR"

log "status (cold re-verify of the produced store)"
ENVIRONMENT="local-simulation"
[ "${GITHUB_ACTIONS:-false}" = "true" ] && ENVIRONMENT="github-actions"
"$REPO/.venv/bin/python" "$REPO/ci/make_status.py" \
  --out-dir "$OUT_DIR" --ci-dir "$REPO/ci" --environment "$ENVIRONMENT"

log "done"
