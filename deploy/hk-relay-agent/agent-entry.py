#!/usr/bin/python3
"""Fixed-path launcher used by the hardened systemd unit."""

from __future__ import annotations

import sys

sys.path.insert(0, "/usr/local/lib/adojapan-relay-agent")

from relay_agent.__main__ import main  # noqa: E402

raise SystemExit(main())
