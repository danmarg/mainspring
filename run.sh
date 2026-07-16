#!/bin/sh
set -e

if [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  echo "Litestream enabled — replica: $LITESTREAM_REPLICA_URL"
  exec litestream replicate \
    -exec "uvicorn app.main:app --host 0.0.0.0 --port 8080" \
    "${DB_PATH:-/data/health.db}" "$LITESTREAM_REPLICA_URL"
else
  exec uvicorn app.main:app --host 0.0.0.0 --port 8080
fi
