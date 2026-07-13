#!/usr/bin/env sh
set -eu
. "$(dirname "$0")/_project_guard.sh"

# Persistent project volumes are intentionally preserved.
docker compose -p adojapan-restream -f compose.yml -f compose.production.yml down --remove-orphans
