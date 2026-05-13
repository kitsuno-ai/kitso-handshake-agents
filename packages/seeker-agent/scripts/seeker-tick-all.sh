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

# Load all seeker secrets in one python pass — keeps the shell simple and
# leaves the credentials file as the single source of truth.
eval "$(python3 -c "
import json, shlex
with open('$CREDS_FILE') as f:
    d = json.load(f)
# Use shlex.quote so values with shell metacharacters survive eval cleanly.
print('MISTRAL_API_KEY=' + shlex.quote(d.get('mistral_api_key', '')))
print('CLOUDFLARE_API_TOKEN=' + shlex.quote(d.get('cloudflare_api_token', '')))
print('CLOUDFLARE_ACCOUNT_ID=' + shlex.quote(d.get('cloudflare_account_id', '')))
print('MOLTBOOK_API_KEY=' + shlex.quote(d.get('moltbook_api_key', '')))
")"

if [ -z "$MISTRAL_API_KEY" ]; then
    log "no mistral_api_key in $CREDS_FILE; aborting"
    exit 1
fi

export MISTRAL_API_KEY CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID MOLTBOOK_API_KEY
export SEEKER_TICK_SOURCE="${SEEKER_TICK_SOURCE:-cron}"

# S303: enable Mistral -> Cloudflare Workers AI failover for the cron sweep.
# Falls back to single-provider Mistral if Cloudflare creds end up empty
# (the _build_provider factory handles that gracefully with a warning).
if [ -n "$CLOUDFLARE_API_TOKEN" ] && [ -n "$CLOUDFLARE_ACCOUNT_ID" ]; then
    export SEEKER_LLM_FAILOVER_ENABLED="true"
fi

# S303: enable the Moltbook arm with the "jobs" submolt (the agent job
# board). Empty by default; setting it here turns on the arm in prod. To
# include other submolts later, set MOLTBOOK_ALLOWED_SUBMOLTS in the cron
# env directly (it takes precedence over this default).
export MOLTBOOK_ALLOWED_SUBMOLTS="${MOLTBOOK_ALLOWED_SUBMOLTS:-jobs}"

log "sweep start (batch_size=$BATCH_SIZE timeout=${SWEEP_TIMEOUT_S}s)"

cd "$PKG_DIR" || { log "cannot cd to $PKG_DIR"; exit 1; }

timeout "$SWEEP_TIMEOUT_S" python3 -m seeker_agent.main sweep \
    --batch-size "$BATCH_SIZE" \
    --dry-run \
    2>&1 | sed -u "s/^/    /"
RC=${PIPESTATUS[0]}

log "sweep end rc=$RC"
exit $RC
