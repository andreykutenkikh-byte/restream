"""Add the CLI-only Compose plugin without a package transaction or daemon restart."""

from bootstrap_worker.errors import safe_failure

# Official docker/compose release assets; pinned independently of the target's apt/rpm state.
# https://github.com/docker/compose/releases/tag/v5.6.0
VERSION = "v5.6.0"
DIGESTS = {
    "x86_64": "40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a",
    "aarch64": "733ec76717ceb59052a9609b9dadfb523b2df8eab57a54212872d10a58078ea2",
}
PLUGIN_DIR = "/usr/local/lib/docker/cli-plugins"


def install_command(architecture: str) -> str:
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    if architecture not in DIGESTS:
        raise safe_failure("unsupported_operating_system")
    url = f"https://github.com/docker/compose/releases/download/{VERSION}/docker-compose-linux-{architecture}"
    return f"""set -eu
test -z "${{DOCKER_CONFIG-}}"
if [ -f "$HOME/.docker/config.json" ]; then
  test -r "$HOME/.docker/config.json"
  if grep -q 'cliPluginsExtraDirs' "$HOME/.docker/config.json"; then exit 1; fi
fi
for path in "$HOME/.docker/cli-plugins/docker-compose" \
  /usr/lib/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-compose \
  /usr/local/libexec/docker/cli-plugins/docker-compose {PLUGIN_DIR}/docker-compose; do
  if [ -e "$path" ] || [ -L "$path" ]; then exit 1; fi
done
daemon_pid=$(systemctl show -p MainPID --value docker)
test "$daemon_pid" -gt 0
for path in /usr /usr/local /usr/local/lib /usr/local/lib/docker {PLUGIN_DIR}; do
  test ! -L "$path"
  if [ ! -e "$path" ]; then mkdir -m 755 -- "$path"; fi
  test -d "$path"
  test "$(stat -c %u "$path")" = 0
  test -z "$(find "$path" -maxdepth 0 -perm /022 -print)"
done
stage=$(mktemp -d {PLUGIN_DIR}/.adojapan-compose.XXXXXX)
trap 'rm -f -- "$stage/docker-compose"; rmdir -- "$stage"' EXIT HUP INT TERM
if command -v curl >/dev/null 2>&1; then
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 180 {url} -o "$stage/docker-compose"
elif command -v wget >/dev/null 2>&1; then
  wget --https-only --timeout=30 --tries=1 -q {url} -O "$stage/docker-compose"
else
  exit 1
fi
printf '%s  %s\\n' {DIGESTS[architecture]} "$stage/docker-compose" | sha256sum -c - >/dev/null
chmod 755 "$stage/docker-compose"
test "$("$stage/docker-compose" version --short)" = {VERSION.removeprefix("v")}
# Atomic no-clobber publication, including a concurrent installation or a dangling symlink.
ln -- "$stage/docker-compose" {PLUGIN_DIR}/docker-compose
test "$(docker compose -p adojapan-restream-node version --short)" = {VERSION.removeprefix("v")}
test "$(systemctl show -p MainPID --value docker)" = "$daemon_pid"
"""
