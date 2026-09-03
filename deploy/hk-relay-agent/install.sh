#!/bin/sh
set -eu
umask 077

if [ "$#" -ne 0 ] || [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root without arguments.' >&2
    exit 2
fi

relay_agent_stage_file=${ADOJAPAN_RELAY_INSTALL_STAGE_FILE:-}
relay_agent_mark_stage() {
    [ -n "$relay_agent_stage_file" ] || return 0
    case "$relay_agent_stage_file" in
        /run/adojapan-relay-install.*.stage) ;;
        *) exit 2 ;;
    esac
    if [ -L "$relay_agent_stage_file" ]; then
        exit 1
    fi
    (umask 077; printf '%s\n' "$1" >"$relay_agent_stage_file")
}

relay_agent_ensure_config_dir() {
    relay_agent_config_dir=$1
    if [ ! -e "$relay_agent_config_dir" ] && [ ! -L "$relay_agent_config_dir" ]; then
        install -d -o root -g root -m 0755 -- "$relay_agent_config_dir"
    fi
    relay_agent_config_permissions=$(stat -c '%A' -- "$relay_agent_config_dir") || exit 1
    if [ -L "$relay_agent_config_dir" ] || [ ! -d "$relay_agent_config_dir" ] || \
       [ "$(stat -c '%u:%g' -- "$relay_agent_config_dir")" != '0:0' ]; then
        printf '%s\n' 'Safety check failed: system configuration directory is unsafe.' >&2
        exit 1
    fi
    case "$relay_agent_config_permissions" in
        ?????w*|????????w*)
            printf '%s\n' \
                'Safety check failed: system configuration directory is writable.' >&2
            exit 1
            ;;
    esac
}

relay_agent_mark_stage preflight
relay_agent_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
relay_agent_repo_root=$(CDPATH= cd -- "$relay_agent_script_dir/../.." && pwd)
relay_agent_source="$relay_agent_repo_root/relay_agent"
relay_agent_journal_helper="$relay_agent_script_dir/journal-rollback.py"

test -d "$relay_agent_source"
if [ ! -f "$relay_agent_journal_helper" ] || [ -L "$relay_agent_journal_helper" ]; then
    printf '%s\n' 'Safety check failed: relay journal helper is unavailable.' >&2
    exit 1
fi
test -f /usr/local/sbin/relayctl
test -x /usr/bin/python3
/usr/bin/python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'

if systemctl is-active --quiet adojapan-relay-agent.service 2>/dev/null; then
    printf '%s\n' 'Stop adojapan-relay-agent.service before install or update.' >&2
    exit 1
fi
if systemctl is-active --quiet adojapan-relay-broker.service 2>/dev/null; then
    printf '%s\n' 'Stop adojapan-relay-broker.service before install or update.' >&2
    exit 1
fi

if find "$relay_agent_source" -type l -print -quit | grep -q .; then
    printf '%s\n' 'Safety check failed: relay_agent source contains a symbolic link.' >&2
    exit 1
fi
if find "$relay_agent_source" ! -type d ! -type f -print -quit | grep -q .; then
    printf '%s\n' 'Safety check failed: relay_agent source contains a special file.' >&2
    exit 1
fi

relay_agent_manifest_dir=$(mktemp -d /run/adojapan-relay-install.XXXXXXXX)
relay_agent_source_manifest="$relay_agent_manifest_dir/source.sha256"
relay_agent_copy_manifest="$relay_agent_manifest_dir/copy.sha256"
relay_agent_cleanup_manifest() {
    rm -f -- "$relay_agent_source_manifest" "$relay_agent_copy_manifest"
    rmdir -- "$relay_agent_manifest_dir" 2>/dev/null || true
}
trap relay_agent_cleanup_manifest EXIT HUP INT TERM
(
    cd "$relay_agent_source"
    find . -type f -name '*.py' -exec sha256sum {} + | LC_ALL=C sort
) >"$relay_agent_source_manifest"
if [ ! -s "$relay_agent_source_manifest" ] || \
   [ ! -f "$relay_agent_source/__init__.py" ] || \
   [ ! -f "$relay_agent_source/broker.py" ]; then
    printf '%s\n' 'Safety check failed: relay_agent source is incomplete.' >&2
    exit 1
fi

relay_before_active=0
relay_before_enabled=0
systemctl is-active --quiet moblin-relay.service && relay_before_active=1 || true
systemctl is-enabled --quiet moblin-relay.service && relay_before_enabled=1 || true

relay_agent_mark_stage accounts
relay_agent_ensure_config_dir /etc/sysusers.d
install -o root -g root -m 0644 "$relay_agent_script_dir/adojapan-relay-agent.sysusers" \
    /etc/sysusers.d/adojapan-relay-agent.conf
relay_agent_mark_stage sysusers
systemd-sysusers /etc/sysusers.d/adojapan-relay-agent.conf
relay_agent_mark_stage accounts
relay_agent_ensure_config_dir /etc/tmpfiles.d
install -o root -g root -m 0644 "$relay_agent_script_dir/adojapan-relay-agent.tmpfiles" \
    /etc/tmpfiles.d/adojapan-relay-agent.conf
relay_agent_mark_stage tmpfiles
systemd-tmpfiles --create /etc/tmpfiles.d/adojapan-relay-agent.conf

# Preserve a strict legacy-v1 journal before installing code that writes v2.
# The root-only helper validates both storage trees without following links and
# never replaces an already valid rollback point.
relay_agent_mark_stage journal
/usr/bin/python3 -Es "$relay_agent_journal_helper" --prepare
install -o root -g root -m 0700 "$relay_agent_journal_helper" \
    /usr/local/sbin/adojapan-relay-restore-v1-journal

relay_agent_mark_stage copy
install -d -o root -g root -m 0755 /usr/local/lib/adojapan-relay-agent
rm -rf /usr/local/lib/adojapan-relay-agent/relay_agent.new
install -d -o root -g root -m 0700 /usr/local/lib/adojapan-relay-agent/relay_agent.new
cp -R --no-preserve=ownership,mode,timestamps "$relay_agent_source/." \
    /usr/local/lib/adojapan-relay-agent/relay_agent.new/
if find /usr/local/lib/adojapan-relay-agent/relay_agent.new -type l -print -quit | grep -q .; then
    printf '%s\n' 'Safety check failed: staged relay_agent contains a symbolic link.' >&2
    exit 1
fi
if find /usr/local/lib/adojapan-relay-agent/relay_agent.new \
    ! -type d ! -type f -print -quit | grep -q .; then
    printf '%s\n' 'Safety check failed: staged relay_agent contains a special file.' >&2
    exit 1
fi
find /usr/local/lib/adojapan-relay-agent/relay_agent.new -type f -name '*.pyc' -delete
find /usr/local/lib/adojapan-relay-agent/relay_agent.new -depth -type d -name __pycache__ \
    -exec rmdir {} +
if find /usr/local/lib/adojapan-relay-agent/relay_agent.new \
    -type f ! -name '*.py' -print -quit | grep -q .; then
    printf '%s\n' 'Safety check failed: staged relay_agent contains an unexpected file.' >&2
    exit 1
fi
(
    cd /usr/local/lib/adojapan-relay-agent/relay_agent.new
    find . -type f -name '*.py' -exec sha256sum {} + | LC_ALL=C sort
) >"$relay_agent_copy_manifest"
if ! cmp -s "$relay_agent_source_manifest" "$relay_agent_copy_manifest"; then
    printf '%s\n' 'Safety check failed: relay_agent source changed while copying.' >&2
    exit 1
fi
chown -R root:root /usr/local/lib/adojapan-relay-agent/relay_agent.new
find /usr/local/lib/adojapan-relay-agent/relay_agent.new -type d -exec chmod 0755 {} +
find /usr/local/lib/adojapan-relay-agent/relay_agent.new -type f -exec chmod 0644 {} +
rm -rf /usr/local/lib/adojapan-relay-agent/relay_agent.old
if [ -d /usr/local/lib/adojapan-relay-agent/relay_agent ]; then
    mv /usr/local/lib/adojapan-relay-agent/relay_agent \
        /usr/local/lib/adojapan-relay-agent/relay_agent.old
fi
mv /usr/local/lib/adojapan-relay-agent/relay_agent.new \
    /usr/local/lib/adojapan-relay-agent/relay_agent

relay_agent_mark_stage units
install -o root -g root -m 0755 "$relay_agent_script_dir/agent-entry.py" \
    /usr/local/lib/adojapan-relay-agent/agent-entry.py
install -o root -g root -m 0755 "$relay_agent_script_dir/broker-entry.py" \
    /usr/local/lib/adojapan-relay-agent/broker-entry.py
install -o root -g root -m 0755 "$relay_agent_script_dir/install-token.py" \
    /usr/local/sbin/adojapan-relay-install-token
install -o root -g root -m 0755 "$relay_agent_script_dir/install-preview-token.py" \
    /usr/local/sbin/adojapan-relay-install-preview-token
install -o root -g root -m 0644 "$relay_agent_script_dir/adojapan-relay-agent.service" \
    /etc/systemd/system/adojapan-relay-agent.service
install -o root -g root -m 0644 "$relay_agent_script_dir/adojapan-relay-broker.service" \
    /etc/systemd/system/adojapan-relay-broker.service
install -o root -g root -m 0644 "$relay_agent_script_dir/adojapan-relay-broker.socket" \
    /etc/systemd/system/adojapan-relay-broker.socket

relay_agent_mark_stage broker
systemctl daemon-reload
systemctl enable --now adojapan-relay-broker.socket

relay_after_active=0
relay_after_enabled=0
systemctl is-active --quiet moblin-relay.service && relay_after_active=1 || true
systemctl is-enabled --quiet moblin-relay.service && relay_after_enabled=1 || true
if [ "$relay_before_active" -ne "$relay_after_active" ] || \
   [ "$relay_before_enabled" -ne "$relay_after_enabled" ]; then
    printf '%s\n' 'Safety check failed: moblin-relay state changed unexpectedly.' >&2
    exit 1
fi

if [ ! -f /etc/adojapan-relay-agent/node.token ]; then
    printf '%s\n' 'Install the node token: sudo adojapan-relay-install-token'
fi
if [ ! -f /etc/adojapan-relay-agent/preview-reader.token ]; then
    printf '%s\n' \
        'Generate the preview reader token: sudo adojapan-relay-install-preview-token --generate'
fi
if [ -n "$relay_agent_stage_file" ]; then
    rm -f -- "$relay_agent_stage_file"
fi
printf '%s\n' 'Relay agent files installed; agent remains inactive.'
