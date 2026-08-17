#!/usr/bin/env sh
set -eu

check_limits() {
  service="$1"
  expected="$2 $3 $4 no running healthy 0 false"
  container_id="$(docker compose -p adojapan-restream --env-file .env.ci -f compose.yml -f compose.production.yml -f compose.ci.yml ps -q "$service")"
  if [ -z "$container_id" ]; then
    echo "$service container was not found" >&2
    exit 1
  fi

  actual="$(docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}} {{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.State.Health.Status}} {{.RestartCount}} {{.State.OOMKilled}}' "$container_id")"
  if [ "$actual" != "$expected" ]; then
    echo "$service runtime restart policy, limits, health, restart count, or OOM state did not match policy" >&2
    exit 1
  fi
  echo "$service runtime restart policy/limits/status/health/restarts/OOM: $actual"
}

check_limits backend 400000000 402653184 96
check_limits mediamtx 200000000 201326592 64
