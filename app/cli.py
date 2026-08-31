"""Explicit, non-logging initialization helpers for deployment secrets."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from app.core.config import Settings
from app.core.security import generate_master_key, hash_password
from app.db import Database
from app.services.relays import RelayProvisionConflictError, RelayService


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
            "provision-relay-node",
        ),
    )
    parser.add_argument("--name", help="Non-secret relay display name")
    parser.add_argument("--address", help="Non-secret relay address")
    parser.add_argument(
        "--rotate-existing",
        action="store_true",
        help="Explicitly rotate the credential of an existing stopped relay",
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
    elif args.command == "hash-password":
        print(_hash_password_interactively())
    else:
        if not args.name or not args.address:
            parser.error("provision-relay-node requires --name and --address")
        settings = Settings.from_env()
        database = Database(settings.database_path)
        database.migrate()
        service = RelayService(database, settings.master_encryption_key)
        try:
            grant = service.provision_node(
                display_name=args.name,
                address=args.address,
                rotate_existing=bool(args.rotate_existing),
            )
        except RelayProvisionConflictError as exc:
            raise SystemExit(str(exc)) from None
        print(
            "WARNING: the relay token below is shown once and is not persisted in plaintext.",
            file=sys.stderr,
        )
        print(f"Node ID: {grant.node_id}", file=sys.stderr)
        print(grant.node_token)


if __name__ == "__main__":
    main()
