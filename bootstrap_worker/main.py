"""Default ASGI application used by the worker container."""

from bootstrap_worker.api import create_app

app = create_app()

__all__ = ["app"]
