#!/bin/bash
# Seeker Agent — sweep all gonzo channels in one tick (single Python process).
#
# Run by cron every 30 min. One process handles all 6 channels so the
# Mistral free-tier pacing state (1.2s/req min_gap) persists across them.
#
# Safety:
#   - kill-switch file at $KILL_FILE blocks everything (exit 0, silent)
#   - overall timeout caps wall-clock at SEEKER_SWEEP_TIMEOUT_S (default 600s)
#   - lock file (handled in run_tick) prevents concurrent sweeps

set -u

KILL_FILE="${SEEKER_KILL_FILE:-/tmp/seeker.kill}"
PKG_DIR="${SEEKER_PKG_DIR:-/opt/kitso-handshake-agents/packages/seeker-agent}"
SWEEP_TIMEOUT_S="${SEEKER_SWEEP_TIMEOUT_S:-600}"
BATCH_SIZE="${SEEKER_BATCH_SIZE:-8}"
LOG_TAG="seeker-tick-all"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] [$LOG_TAG] $*"; }

if [ -f "$KILL_FILE" ]; then
    log "kill switch engaged ($KILL_FILE present); skipping sweep"
    exit 0
fi

CREDS_FILE="${SEEKER_CREDENTIALS_FILE:-/root/.config/seeker/credentials.json}"
if [ ! -f "$CREDS_FILE" ]; then
    log "credentials file missing: $CREDS_FILE; aborting"
    exit 1
fi

MISTRAL_API_KEY=$(python3 -c "
import json
with open('$CREDS_FILE') as f:
    d = json.load(f)
print(d.get('mistral_api_key', ''))
")

if [ -z "$MISTRAL_API_KEY" ]; then
    log "no mistral_api_key in $CREDS_FILE; aborting"
    exit 1
fi
export MISTRAL_API_KEY
export SEEKER_TICK_SOURCE="${SEEKER_TICK_SOURCE:-cron}"

log "sweep start (batch_size=$BATCH_SIZE timeout=${SWEEP_TIMEOUT_S}s)"

cd "$PKG_DIR" || { log "cannot cd to $PKG_DIR"; exit 1; }

timeout "$SWEEP_TIMEOUT_S" python3 -m seeker_agent.main sweep \
    --batch-size "$BATCH_SIZE" \
    --dry-run \
    2>&1 | sed -u "s/^/    /"
RC=${PIPESTATUS[0]}

log "sweep end rc=$RC"
exit $RC
