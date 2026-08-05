#!/usr/bin/env bash
# Backfill Garmin and/or Google Health in weekly chunks.
# Each chunk completes before Fly auto-stops the machine; the next request wakes it again.
#
# Usage:
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14 garmin
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14 google_health

set -euo pipefail

BASE_URL="${APP_BASE_URL:?APP_BASE_URL env var required (e.g. https://your-app.fly.dev or http://localhost:8080)}"
START="${1:?Usage: backfill.sh START_DATE END_DATE [source]}"
END="${2:?Usage: backfill.sh START_DATE END_DATE [source]}"
SOURCE_FILTER="${3:-both}"  # garmin | google_health | both
TOKEN="${ADMIN_TOKEN:?ADMIN_TOKEN env var required}"

POLL_INTERVAL=5
CHUNK_DAYS=7
# Each chunk fires ~10 endpoint calls per day (rollups + daily-* + sleep/exercise
# lists) against the Google Health API, so a 7-day chunk is ~70 requests already.
# Pause between successful chunks so a long backfill doesn't run afoul of Google's
# per-user rate limits — failed chunks still use the exponential backoff below.
THROTTLE_SECONDS=20

date_add() {
    date -d "$1 + $2 days" +%Y-%m-%d 2>/dev/null \
        || python3 -c "from datetime import date,timedelta; d=date.fromisoformat('$1'); print((d+timedelta(days=$2)).isoformat())"
}

date_le() {
    [ "$(python3 -c "print('1' if '$1' <= '$2' else '0')")" = "1" ]
}

run_chunk() {
    local source="$1" chunk_start="$2" chunk_end="$3"
    echo "→ $source  $chunk_start → $chunk_end"

    local response
    response=$(curl -sf -X POST \
        "${BASE_URL}/admin/import/${source}?start_date=${chunk_start}&end_date=${chunk_end}" \
        -H "Authorization: Bearer ${TOKEN}")

    local run_id
    run_id=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

    # Poll until done, keeping the machine awake
    while true; do
        sleep "$POLL_INTERVAL"
        local status_resp
        status_resp=$(curl -sf "${BASE_URL}/admin/import/status/${run_id}" \
            -H "Authorization: Bearer ${TOKEN}")
        local status rows err
        status=$(echo "$status_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'])")
        rows=$(echo "$status_resp"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('rows_upserted') or '')")
        err=$(echo "$status_resp"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error') or '')")

        if [ "$status" = "ok" ] || [ "$status" = "skipped" ]; then
            echo "  ✓ $rows rows"
            return 0
        elif [ "$status" = "error" ]; then
            echo "  ✗ error: $err"
            return 1
        fi
    done
}

# A failing chunk (e.g. a revoked OAuth token) fails fast, and every later chunk hits
# the same broken credential — without a backoff, that turns into a tight retry loop
# hammering the import endpoint every few seconds. Back off exponentially between
# failures and give up after a run of consecutive ones instead of grinding through
# every remaining chunk against a credential that isn't coming back on its own.
MAX_CONSECUTIVE_FAILURES=3
BACKOFF_BASE=30
BACKOFF_MAX=600
consecutive_failures=0

run_chunk_with_backoff() {
    if run_chunk "$@"; then
        consecutive_failures=0
        return 0
    fi

    consecutive_failures=$((consecutive_failures + 1))
    if [ "$consecutive_failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
        echo "  ✗ $consecutive_failures consecutive failures — aborting backfill (likely a persistent auth/config issue, not a transient one)."
        exit 1
    fi

    local backoff=$((BACKOFF_BASE * (2 ** (consecutive_failures - 1))))
    [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
    echo "  … backing off ${backoff}s before the next chunk (failure $consecutive_failures/$MAX_CONSECUTIVE_FAILURES)"
    sleep "$backoff"
}

chunk_start="$START"
while date_le "$chunk_start" "$END"; do
    chunk_end=$(date_add "$chunk_start" "$((CHUNK_DAYS - 1))")
    [ "$(python3 -c "print('1' if '$chunk_end' > '$END' else '0')")" = "1" ] && chunk_end="$END"

    if [ "$SOURCE_FILTER" = "both" ] || [ "$SOURCE_FILTER" = "garmin" ]; then
        run_chunk_with_backoff garmin "$chunk_start" "$chunk_end"
    fi
    if [ "$SOURCE_FILTER" = "both" ] || [ "$SOURCE_FILTER" = "google_health" ]; then
        run_chunk_with_backoff google_health "$chunk_start" "$chunk_end"
    fi

    chunk_start=$(date_add "$chunk_start" "$CHUNK_DAYS")
    date_le "$chunk_start" "$END" && sleep "$THROTTLE_SECONDS"
done

echo "Done."
