FROM debian:12.11-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        ffmpeg \
        fonts-dejavu-core \
        iproute2 \
        openssh-server \
        procps \
        python3-yaml \
        sudo \
        systemd \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10002 --shell /bin/sh ci-node \
    && usermod --password '$6$adojapanci$pOxXaB5ilo3ZfsWGlIO22ruMkY5TwOyHBgi7o5tmRFnEb9/9e3pmKBGOYLD9x3EWnsEFP72dpP4kJ9cSynF3W0' ci-node \
    && printf '%s\n' 'ci-node ALL=(root) ALL' > /etc/sudoers.d/ci-node \
    && chmod 0440 /etc/sudoers.d/ci-node \
    && mkdir -p /run/sshd

# Independent media evidence must survive native install rollback. These pins
# are checked against RelayInstaller by unit tests; no service is installed.
RUN set -eu; \
    reader_version='v1.20.1'; \
    reader_archive='mediamtx_v1.20.1_linux_amd64.tar.gz'; \
    reader_url='https://github.com/bluenviron/mediamtx/releases/download/v1.20.1/mediamtx_v1.20.1_linux_amd64.tar.gz'; \
    reader_archive_sha256='81b143f55a5d23d4a8c028d52869c14ea4a59919900528698fcc97a747fd69c6'; \
    install -d -m 0700 /tmp/adojapan-ci-reader-download /usr/local/lib/adojapan-ci/reader; \
    curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 180 --max-filesize 67108864 \
        "$reader_url" --output /tmp/adojapan-ci-reader-download/archive.tar.gz; \
    printf '%s  %s\n' "$reader_archive_sha256" /tmp/adojapan-ci-reader-download/archive.tar.gz \
        | sha256sum --check --status; \
    tar -xzf /tmp/adojapan-ci-reader-download/archive.tar.gz \
        -C /tmp/adojapan-ci-reader-download mediamtx LICENSE; \
    test -f /tmp/adojapan-ci-reader-download/mediamtx; \
    test ! -L /tmp/adojapan-ci-reader-download/mediamtx; \
    test -f /tmp/adojapan-ci-reader-download/LICENSE; \
    test ! -L /tmp/adojapan-ci-reader-download/LICENSE; \
    test "$(stat -c '%s' /tmp/adojapan-ci-reader-download/mediamtx)" -le 134217728; \
    install -o root -g root -m 0555 /tmp/adojapan-ci-reader-download/mediamtx \
        /usr/local/lib/adojapan-ci/reader/mediamtx; \
    install -o root -g root -m 0444 /tmp/adojapan-ci-reader-download/LICENSE \
        /usr/local/lib/adojapan-ci/reader/LICENSE; \
    reader_binary_sha256="$(sha256sum /usr/local/lib/adojapan-ci/reader/mediamtx | cut -d ' ' -f 1)"; \
    printf '{"version":"%s","archive":"%s","archive_sha256":"%s","binary_sha256":"%s"}\n' \
        "$reader_version" "$reader_archive" "$reader_archive_sha256" "$reader_binary_sha256" \
        > /usr/local/lib/adojapan-ci/reader/manifest.json; \
    chmod 0444 /usr/local/lib/adojapan-ci/reader/manifest.json; \
    reader_actual_version="$(/usr/local/lib/adojapan-ci/reader/mediamtx --version)"; \
    test "$reader_actual_version" = "$reader_version"; \
    chmod 0555 /usr/local/lib/adojapan-ci/reader; \
    rm -rf /tmp/adojapan-ci-reader-download

COPY ci/ssh-target/sshd_config /etc/ssh/sshd_config
COPY ci/ssh-target/fake-docker /usr/local/bin/docker
COPY ci/ssh-target/fake-apt-get /usr/local/bin/apt-get
COPY ci/ssh-target/fake-systemctl /usr/local/bin/systemctl
COPY ci/ssh-target/fake-dpkg-query /usr/local/bin/dpkg-query
COPY ci/ssh-target/validate-node-compose.py /usr/local/lib/adojapan-ci/validate-node-compose.py
COPY ci/ssh-target/relay-heartbeat.py /usr/local/lib/adojapan-ci/relay-heartbeat.py

ENV PATH="/usr/local/bin:${PATH}"

RUN rm -f /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub \
    && ssh-keygen -q -t ed25519 -N '' -C ci-ephemeral-host-key \
        -f /etc/ssh/ssh_host_ed25519_key \
    && chmod 0600 /etc/ssh/ssh_host_ed25519_key \
    && chmod 0644 /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/sshd_config \
    && chmod 0644 /usr/local/lib/adojapan-ci/validate-node-compose.py \
    && chmod 0755 /usr/local/lib/adojapan-ci/relay-heartbeat.py \
    && chmod 0755 /usr/local/bin/apt-get /usr/local/bin/docker /usr/local/bin/systemctl \
        /usr/local/bin/dpkg-query \
    && cp /usr/local/bin/systemctl /usr/bin/systemctl \
    && /usr/sbin/sshd -t

EXPOSE 22

STOPSIGNAL SIGTERM

CMD ["sh", "-c", "install -d -m 0755 /run/sshd && exec /usr/sbin/sshd -D -e"]
