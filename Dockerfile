FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY scripts ./scripts

RUN pip install "uv==0.11.28" \
    && uv sync --locked --no-dev --no-editable --compile-bytecode \
    && groupadd --gid 10001 adojapan \
    && useradd --uid 10001 --gid adojapan --home-dir /srv/app --shell /usr/sbin/nologin adojapan \
    && install -d -o 10001 -g 10001 -m 0700 /run/adojapan-bootstrap \
    && mkdir -p /srv/app/data /srv/app/logs /srv/app/backups \
    && chown -R adojapan:adojapan /srv/app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
