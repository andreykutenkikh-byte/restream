"""Entry point for the unprivileged native relay agent."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from relay_agent.broker_client import BrokerClient
from relay_agent.client import ControlClient
from relay_agent.errors import RelayAgentError
from relay_agent.journal import CommandJournal
from relay_agent.metrics import HostMetricsCollector
from relay_agent.processor import CommandProcessor
from relay_agent.security import effective_uid, read_private_token
from relay_agent.service import AgentService

TOKEN_PATH = Path("/etc/adojapan-relay-agent/node.token")
JOURNAL_PATH = Path("/var/lib/adojapan-relay-agent/commands.json")


def main() -> int:
    if sys.argv[1:] or effective_uid() == 0:
        return 2
    os.umask(0o077)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s relay-agent %(message)s",
    )
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        token = read_private_token(TOKEN_PATH)
        control = ControlClient(token)
        broker = BrokerClient()
        journal = CommandJournal(JOURNAL_PATH)
        processor = CommandProcessor(broker, journal)
        service = AgentService(
            control=control,
            processor=processor,
            metrics=HostMetricsCollector(),
            stop_event=stop_event,
        )
        service.run()
    except RelayAgentError as exc:
        logging.getLogger("relay_agent").error("Agent stopped safely (%s)", exc.code)
        return 1
    except Exception:
        logging.getLogger("relay_agent").error("Agent stopped safely (internal_error)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
