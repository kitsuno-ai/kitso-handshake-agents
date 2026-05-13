#!/bin/bash
# S305 — Vacancy retry tick.
#
# Posts any vacancy card in $VACANCY_DIR that hasn't yet been successfully
# posted. Idempotent — uses audit_events.event_type='vacancy_posted' as the
# completion marker.
#
# Cron pattern: */15 * * * *  (every 15 min)
# Moltbook rate limit is 1 post / 30 min / agent, so consecutive cards land
# every other cycle. Successful posts skip immediately; we stop after the
# first success in a cycle to avoid hitting the defensive rate limit.
#
# Designed for the Moltbook-outage retry case but useful as the steady-state
# "new card lands → cron picks it up automatically" mechanism.

set -uo pipefail
# NOT set -e — we want to continue past a failed post to the next card

VACANCY_DIR=${VACANCY_DIR:-/opt/kitsuno-vacancies/v0.1}
SUBMOLT=${SUBMOLT:-jobs}
AUDIT_DB_URL=${AUDIT_DB_URL:-postgresql://seeker_writer:5bN6woa4QTbvBA13PTFk7tonGHNCBrHd8IPz7D7oprI@127.0.0.1:5435/kitso_handshake_experiment}
PG_CONTAINER=${PG_CONTAINER:-experiment-db-postgres}

# Generate a fresh kill token for this run (dead-man switch)
export AGENT_KILL_TOKEN="$(openssl rand -hex 16)"
export AUDIT_DB_URL

ts() { date -Iseconds; }

# Quick health check: is Moltbook itself up? If GET /agents/me 500s, every
# card will fail — no point spamming. Probe once and bail early.
API_KEY=$(python3 -c "import json; print(json.load(open('/root/.config/moltbook/credentials.json'))['kitsuno_jobs']['api_key'])" 2>/dev/null || echo "")
if [ -z "$API_KEY" ]; then
  echo "[$(ts)] FATAL: could not read kitsuno_jobs api_key from credentials"
  exit 1
fi

MOLTBOOK_STATUS=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 \
  -H "Authorization: Bearer $API_KEY" \
  https://www.moltbook.com/api/v1/agents/me || echo "000")

if [ "$MOLTBOOK_STATUS" != "200" ]; then
  echo "[$(ts)] SKIP cycle — Moltbook GET /agents/me returned $MOLTBOOK_STATUS"
  exit 0
fi

# For each .json card, check if already posted; if not, try to post.
for card in "$VACANCY_DIR"/*.json; do
  [ -f "$card" ] || continue

  card_basename=$(basename "$card")

  # Check audit_events for a successful post of this card
  POSTED=$(docker exec "$PG_CONTAINER" psql -U postgres -d kitso_handshake_experiment -t -A -c \
    "SELECT COUNT(*) FROM audit_events WHERE event_type = 'vacancy_posted' AND payload->>'card_path' = '$card'" \
    2>/dev/null | tr -d '[:space:]')

  if [ "${POSTED:-0}" -gt "0" ]; then
    echo "[$(ts)] SKIP $card_basename — already posted"
    continue
  fi

  echo "[$(ts)] ATTEMPT $card_basename"
  if vacancy-agent --card "$card" --submolt "$SUBMOLT" >/tmp/vacancy-retry-last.log 2>&1; then
    echo "[$(ts)] OK $card_basename — stopping cycle (respect rate limit)"
    tail -2 /tmp/vacancy-retry-last.log
    exit 0
  else
    echo "[$(ts)] FAIL $card_basename — see /tmp/vacancy-retry-last.log"
    tail -3 /tmp/vacancy-retry-last.log
  fi
done

echo "[$(ts)] cycle complete — no unposted cards"
