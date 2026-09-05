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
