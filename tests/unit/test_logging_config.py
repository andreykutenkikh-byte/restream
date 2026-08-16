import logging
import sys

from app.logging_config import RedactingFormatter, configure_logging


def test_formatter_redacts_exception_text() -> None:
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("token=do-not-log-this")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "operation failed",
            (),
            exc_info=sys.exc_info(),
        )
    rendered = formatter.format(record)
    assert "do-not-log-this" not in rendered
    assert "[REDACTED]" in rendered


def test_http_transport_access_logs_are_never_verbose() -> None:
    configure_logging("DEBUG")

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("asyncssh").getEffectiveLevel() == logging.WARNING
