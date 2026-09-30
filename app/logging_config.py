"""Secret-aware application logging."""

from __future__ import annotations

import logging

from app.core.redaction import redact_text


class OAuthAccessFilter(logging.Filter):
    """Remove callback query secrets without breaking Uvicorn's structured formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if str(path).startswith("/api/broadcasts/youtube/callback"):
                record.args = (client, method, str(path).partition("?")[0], version, status)
        return True


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact_text(message)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the final rendered record, including exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx logs complete request URLs at INFO. MediaMTX control paths include
    # the ingest key, so transport access logs must never inherit INFO/DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, OAuthAccessFilter) for f in access.filters):
        access.addFilter(OAuthAccessFilter())
