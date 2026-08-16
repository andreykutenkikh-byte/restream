"""Node agent process entrypoint."""

from __future__ import annotations

import logging
import signal
import threading

from node_agent import __version__
from node_agent.client import NodeAPIClient
from node_agent.commands import CommandJournal, CommandProcessor, LocalSelfTestProbe
from node_agent.credentials import CredentialStore
from node_agent.errors import AgentError, ConfigurationError, CredentialError, ProtocolError
from node_agent.metrics import MetricsCollector
from node_agent.service import AgentService
from node_agent.settings import AgentSettings


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy_logger in ("httpx", "httpcore"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def _run_agent(stop_event: threading.Event) -> int:
    logger = logging.getLogger("node_agent")
    try:
        settings = AgentSettings.from_env()
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        metrics = MetricsCollector(
            settings.data_dir,
            host_hostname=settings.host_hostname,
            host_os_name=settings.host_os_name,
            host_os_version=settings.host_os_version,
            host_architecture=settings.host_architecture,
        )
        credentials = CredentialStore(settings.enrollment_token_path, settings.node_token_path)
        journal = CommandJournal(settings.command_journal_path)
        self_test = LocalSelfTestProbe(
            settings.control_url,
            settings.data_dir,
            allow_insecure_http=settings.allow_insecure_http,
        )
        commands = CommandProcessor(
            agent_version=__version__,
            journal=journal,
            snapshot_supplier=metrics.collect,
            self_test_probe=self_test,
        )
        with NodeAPIClient(settings, agent_version=__version__) as client:
            service = AgentService(
                settings=settings,
                client=client,
                credentials=credentials,
                metrics=metrics,
                commands=commands,
                stop_event=stop_event,
            )
            service.run()
    except (ConfigurationError, CredentialError, ProtocolError) as exc:
        logger.error("Agent entered quiescent state (%s)", exc.code)
        stop_event.wait()
        return 0
    except AgentError as exc:
        logger.error("Node agent stopped safely (%s)", exc.code)
        return 1
    except Exception:
        logger.error("Node agent stopped safely (internal_error)")
        return 1
    logger.info("Node agent stopped")
    return 0


def main() -> int:
    _configure_logging()
    stop_event = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return _run_agent(stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
