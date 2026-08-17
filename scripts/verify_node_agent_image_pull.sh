#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo 'usage: verify_node_agent_image_pull.sh IMAGE@sha256:DIGEST' >&2
  exit 2
fi

image_reference=$1
if ! printf '%s\n' "$image_reference" \
  | grep -Eq '^ghcr\.io/andreykutenkikh-byte/restream-node@sha256:[0-9a-f]{64}$'; then
  echo 'Node Agent image reference must use the approved repository and an exact lowercase SHA-256 digest' >&2
  exit 2
fi

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
umask 077
docker_config=$(mktemp -d "$RUNNER_TEMP/adojapan-anonymous-docker.XXXXXX")
cleanup() {
  rm -rf -- "$docker_config"
}
trap cleanup 0 HUP INT TERM

DOCKER_CONFIG=$docker_config
export DOCKER_CONFIG

test -z "$(find "$DOCKER_CONFIG" -mindepth 1 -print -quit)"
docker pull "$image_reference"
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_reference" \
  | grep -Fqx "$image_reference"

printf 'Anonymous exact-digest pull verified: %s\n' "$image_reference"
