"""Explicit, non-logging initialization helpers for deployment secrets."""

from __future__ import annotations

import argparse
import getpass
import secrets

from app.core.security import generate_master_key, hash_password


def _hash_password_interactively() -> str:
    password = getpass.getpass("New administrator password: ")
    confirmation = getpass.getpass("Repeat administrator password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Use an administrator password with at least 12 characters")
    return hash_password(password)


def main() -> None:
    parser = argparse.ArgumentParser(description="AdoJapan Restream initialization helpers")
    parser.add_argument(
        "command",
        choices=(
            "generate-master-key",
            "generate-session-secret",
            "generate-worker-auth-password",
            "generate-bootstrap-worker-secret",
            "hash-password",
        ),
    )
    args = parser.parse_args()
    if args.command == "generate-master-key":
        print(generate_master_key())
    elif args.command in {
        "generate-session-secret",
        "generate-worker-auth-password",
        "generate-bootstrap-worker-secret",
    }:
        print(secrets.token_urlsafe(48))
    else:
        print(_hash_password_interactively())


if __name__ == "__main__":
    main()
