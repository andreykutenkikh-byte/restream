#!/usr/bin/env sh
set -eu
. "$(dirname "$0")/_project_guard.sh"

docker compose -p adojapan-restream -f compose.yml -f compose.production.yml stop
