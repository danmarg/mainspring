#!/usr/bin/env bash
set -euo pipefail

# Deploy mainspring to Fly.io.
# On first run, creates a scheduler machine that triggers imports hourly.
# On subsequent runs, updates the scheduler machine to the new image automatically.
# Usage: ./fly-deploy.sh

# Read app name from fly.toml so there's one place to change it
APP=$(grep '^app = ' fly.toml | sed 's/app = "\([^"]*\)".*/\1/')
APP_URL="https://${APP}.fly.dev"

echo "==> Deploying app..."
fly deploy --app "$APP"

# Parse machine list once
MACHINES_JSON=$(fly machine list --app "$APP" --json)

# Find the image the app machine is now running
IMAGE=$(python3 -c "
import json, sys
machines = json.load(sys.stdin)
for m in machines:
    if not m.get('config', {}).get('schedule'):
        print(m['config']['image'])
        break
" <<< "$MACHINES_JSON")

# Find the scheduler machine (has config.schedule set)
SCHEDULER_ID=$(python3 -c "
import json, sys
machines = json.load(sys.stdin)
for m in machines:
    if m.get('config', {}).get('schedule'):
        print(m['id'])
        break
" <<< "$MACHINES_JSON")

SCHEDULER_CMD="/bin/sh -c \"curl -sf -X POST ${APP_URL}/admin/import/garmin -H 'Authorization: Bearer \$ADMIN_TOKEN' && curl -sf -X POST ${APP_URL}/admin/import/google_health -H 'Authorization: Bearer \$ADMIN_TOKEN' && echo imports done\""

if [ -n "$SCHEDULER_ID" ]; then
  echo "==> Updating scheduler machine $SCHEDULER_ID to image $IMAGE..."
  fly machine update "$SCHEDULER_ID" --image "$IMAGE" --command "$SCHEDULER_CMD" --app "$APP" --yes
else
  echo "==> No scheduler machine found — creating one..."
  fly machine run "$IMAGE" \
    --app "$APP" \
    --schedule hourly \
    --command "$SCHEDULER_CMD"
  echo "==> Scheduler machine created."
fi

echo "==> Done."
# Note: public URL (not .internal) is required so Fly proxy wakes the auto-stopped app machine.
