#!/bin/sh
set -eu
umask 077

if [ "$#" -ne 0 ] || [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root without arguments.' >&2
    exit 2
fi

relay_before_active=0
relay_before_enabled=0
systemctl is-active --quiet moblin-relay.service && relay_before_active=1 || true
systemctl is-enabled --quiet moblin-relay.service && relay_before_enabled=1 || true

systemctl disable --now adojapan-relay-agent.service 2>/dev/null || true
systemctl disable --now adojapan-relay-broker.socket 2>/dev/null || true
systemctl stop adojapan-relay-broker.service 2>/dev/null || true
rm -f /etc/systemd/system/adojapan-relay-agent.service
rm -f /etc/systemd/system/adojapan-relay-broker.service
rm -f /etc/systemd/system/adojapan-relay-broker.socket
rm -f /etc/sysusers.d/adojapan-relay-agent.conf
rm -f /etc/tmpfiles.d/adojapan-relay-agent.conf
rm -f /usr/local/sbin/adojapan-relay-install-token
rm -rf /usr/local/lib/adojapan-relay-agent
systemctl daemon-reload
systemctl reset-failed adojapan-relay-agent.service adojapan-relay-broker.service 2>/dev/null || true

relay_after_active=0
relay_after_enabled=0
systemctl is-active --quiet moblin-relay.service && relay_after_active=1 || true
systemctl is-enabled --quiet moblin-relay.service && relay_after_enabled=1 || true
if [ "$relay_before_active" -ne "$relay_after_active" ] || \
   [ "$relay_before_enabled" -ne "$relay_after_enabled" ]; then
    printf '%s\n' 'Safety check failed: moblin-relay state changed unexpectedly.' >&2
    exit 1
fi

printf '%s\n' 'Agent removed; credential, journal and restream-agent account were retained.'
