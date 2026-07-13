#!/usr/bin/env sh
set -eu

PROJECT_NAME="adojapan-restream"

if [ ! -f "compose.yml" ] || [ ! -f "compose.production.yml" ] || [ ! -d "app" ] || [ ! -d "mediamtx" ]; then
  echo "Run this command from the AdoJapan Restream repository root." >&2
  exit 2
fi

if ! grep -Eq '^name:[[:space:]]+adojapan-restream$' compose.yml; then
  echo "compose.yml does not declare the expected project name." >&2
  exit 2
fi

if [ "${COMPOSE_PROJECT_NAME:-$PROJECT_NAME}" != "$PROJECT_NAME" ]; then
  echo "COMPOSE_PROJECT_NAME must be $PROJECT_NAME." >&2
  exit 2
fi

bad_command='docker (system|volume|network) '
bad_command="${bad_command}prune"
if grep -R -E "$bad_command" scripts --include='*.sh' >/dev/null 2>&1; then
  echo "A destructive Docker command was detected in project scripts." >&2
  exit 2
fi

for script in scripts/*.sh; do
  if grep 'docker compose' "$script" | grep -v 'docker compose -p adojapan-restream' >/dev/null 2>&1; then
    echo "A Compose command without the fixed project name was detected in $script." >&2
    exit 2
  fi
done

for script in scripts/start.sh scripts/stop.sh scripts/rollback.sh; do
  if grep 'docker compose' "$script" | grep -v -- 'docker compose -p adojapan-restream -f compose.yml -f compose.production.yml' >/dev/null 2>&1; then
    echo "A production Compose command without the shared-host override was detected in $script." >&2
    exit 2
  fi
done

export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
