#!/usr/bin/env bash
set -euo pipefail

# Deploy mainspring and update the scheduler machine to the new image.
# Usage: ./deploy.sh

APP=mainspring
SCHEDULER_MACHINE=7812d31cde1278

echo "==> Deploying app..."
fly deploy --app "$APP"

# Grab the image tag that fly deploy just pushed
IMAGE=$(fly machine list --app "$APP" --json \
  | python3 -c "
import json, sys
machines = json.load(sys.stdin)
# find the non-scheduled app machine
for m in machines:
    if not m.get('config', {}).get('schedule'):
        print(m['config']['image'])
        break
")

SCHEDULER_CMD='/bin/sh -c "curl -sf -X POST https://your-app.fly.dev/admin/import/garmin -H \"Authorization: Bearer $ADMIN_TOKEN\" && curl -sf -X POST https://your-app.fly.dev/admin/import/google_health -H \"Authorization: Bearer $ADMIN_TOKEN\" && echo \"imports done\""'

echo "==> New image: $IMAGE"
echo "==> Updating scheduler machine $SCHEDULER_MACHINE..."
fly machine update "$SCHEDULER_MACHINE" --image "$IMAGE" --command "$SCHEDULER_CMD" --app "$APP" --yes

echo "==> Done."
# Note: public URL (not .internal) is required so Fly proxy wakes the auto-stopped app machine.
