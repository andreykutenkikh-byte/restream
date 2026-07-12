import logging
import sys

from app.logging_config import RedactingFormatter


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
