#!/usr/bin/env sh
set -eu

check_limits() {
  service="$1"
  expected="$2 $3 $4 running healthy"
  container_id="$(docker compose -p adojapan-restream --env-file .env.ci -f compose.yml -f compose.production.yml -f compose.ci.yml ps -q "$service")"
  if [ -z "$container_id" ]; then
    echo "$service container was not found" >&2
    exit 1
  fi

  actual="$(docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}} {{.State.Status}} {{.State.Health.Status}}' "$container_id")"
  if [ "$actual" != "$expected" ]; then
    echo "$service runtime limits or health did not match policy" >&2
    exit 1
  fi
  echo "$service runtime limits/status/health: $actual"
}

check_limits backend 400000000 402653184 96
check_limits mediamtx 200000000 201326592 64
