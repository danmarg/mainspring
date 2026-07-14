#!/usr/bin/env bash
# Backfill Garmin and/or Google Health in weekly chunks.
# Each chunk completes before Fly auto-stops the machine; the next request wakes it again.
#
# Usage:
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14 garmin
#   ADMIN_TOKEN=xxx ./scripts/backfill.sh 2026-04-01 2026-07-14 google_health

set -euo pipefail

BASE_URL="${BASE_URL:-https://your-app.fly.dev}"
START="${1:?Usage: backfill.sh START_DATE END_DATE [source]}"
END="${2:?Usage: backfill.sh START_DATE END_DATE [source]}"
SOURCE_FILTER="${3:-both}"  # garmin | google_health | both
TOKEN="${ADMIN_TOKEN:?ADMIN_TOKEN env var required}"

POLL_INTERVAL=5
CHUNK_DAYS=7

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
            break
        elif [ "$status" = "error" ]; then
            echo "  ✗ error: $err"
            break
        fi
    done
}

chunk_start="$START"
while date_le "$chunk_start" "$END"; do
    chunk_end=$(date_add "$chunk_start" "$((CHUNK_DAYS - 1))")
    [ "$(python3 -c "print('1' if '$chunk_end' > '$END' else '0')")" = "1" ] && chunk_end="$END"

    if [ "$SOURCE_FILTER" = "both" ] || [ "$SOURCE_FILTER" = "garmin" ]; then
        run_chunk garmin "$chunk_start" "$chunk_end"
    fi
    if [ "$SOURCE_FILTER" = "both" ] || [ "$SOURCE_FILTER" = "google_health" ]; then
        run_chunk google_health "$chunk_start" "$chunk_end"
    fi

    chunk_start=$(date_add "$chunk_start" "$CHUNK_DAYS")
done

echo "Done."
