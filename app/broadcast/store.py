"""Transactional broadcast intent, secret storage and safe read projections."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from app.broadcast.models import BroadcastError, OutputCreate, ResourceLimits, SessionCreate
from app.core.security import decrypt_destination_key, encrypt_destination_key
from app.db import Database, utc_now


class BroadcastStore:
    def __init__(self, database: Database, master_key: str) -> None:
        self.database = database
        self.master_key = master_key
        self.limits = ResourceLimits()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def seal(self, value: dict[str, Any]) -> str:
        return encrypt_destination_key(json.dumps(value), self.master_key)

    def unseal(self, value: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(decrypt_destination_key(value, self.master_key))
        return result

    def fingerprint(self, value: Any) -> str:
        return hmac.new(
            self.master_key.encode(), json.dumps(value, sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()

    def replay(self, db: sqlite3.Connection, scope: str, key: str, value: Any) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise BroadcastError("idempotency_key_required", 422)
        row = db.execute(
            "SELECT fingerprint,result_id FROM broadcast_requests "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row["fingerprint"], self.fingerprint(value)):
            raise BroadcastError("idempotency_conflict")
        return str(row["result_id"])

    def remember(
        self, db: sqlite3.Connection, scope: str, key: str, value: Any, result_id: str
    ) -> None:
        db.execute(
            "INSERT INTO broadcast_requests VALUES (?,?,?,?,?)",
            (scope, key, self.fingerprint(value), result_id, utc_now()),
        )

    @staticmethod
    def row(db: sqlite3.Connection, sql: str, values: tuple[Any, ...]) -> sqlite3.Row:
        row = db.execute(sql, values).fetchone()
        if row is None:
            raise BroadcastError("broadcast_resource_not_found", 404)
        return cast(sqlite3.Row, row)

    @staticmethod
    def node(db: sqlite3.Connection, node_id: str) -> None:
        row = db.execute("SELECT id FROM restream_nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise BroadcastError("node_not_found", 404)

    @staticmethod
    def event(
        db: sqlite3.Connection,
        session_id: str,
        event_type: str,
        *,
        output_id: str | None = None,
        switch_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO broadcast_events(session_id,output_id,switch_id,event_type,"
            "safe_detail_json,created_at) VALUES (?,?,?,?,?,?)",
            (session_id, output_id, switch_id, event_type, json.dumps(detail or {}), utc_now()),
        )

    def create_session(self, data: SessionCreate, key: str) -> str:
        value = data.model_dump()
        with self.transaction() as db:
            prior = self.replay(db, "session", key, value)
            if prior:
                return prior
            self.node(db, data.ingress_node_id)
            if db.execute("SELECT COUNT(*) FROM broadcast_sessions").fetchone()[0] >= 100:
                raise BroadcastError("session_limit")
            source_id, session_id = secrets.token_hex(16), secrets.token_hex(16)
            now = utc_now()
            db.execute(
                "INSERT INTO broadcast_sources VALUES (?,'moblin',?,?,?)",
                (source_id, data.ingress_node_id, data.profile.model_dump_json(), now),
            )
            db.execute(
                "INSERT INTO broadcast_sessions(id,source_id,name,policy,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, source_id, data.name, data.policy, now, now),
            )
            self.event(db, session_id, "session.created")
            self.remember(db, "session", key, value, session_id)
            return session_id

    def create_output(self, session_id: str, data: OutputCreate, key: str) -> str:
        value = data.model_dump(mode="json")
        value["stream_key"] = data.stream_key.get_secret_value() if data.stream_key else None
        scope = f"output:{session_id}"
        with self.transaction() as db:
            prior = self.replay(db, scope, key, value)
            if prior:
                return prior
            self.row(db, "SELECT id FROM broadcast_sessions WHERE id=?", (session_id,))
            self.node(db, data.node_id)
            count = db.execute(
                "SELECT COUNT(*) FROM broadcast_outputs WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            if count >= self.limits.max_outputs_per_source:
                raise BroadcastError("output_limit")
            credentials: dict[str, Any] | None = None
            if data.mode == "manual":
                if not data.primary_url or not data.stream_key:
                    raise BroadcastError("manual_credentials_required", 422)
                if data.backup_url == data.primary_url:
                    raise BroadcastError("distinct_backup_endpoint_required", 422)
                credentials = {
                    "primary": data.primary_url,
                    "backup": data.backup_url,
                    "stream_key": value["stream_key"],
                }
                fingerprint = self.fingerprint(value["stream_key"])
                if db.execute(
                    "SELECT 1 FROM youtube_bindings WHERE credential_fingerprint=?", (fingerprint,)
                ).fetchone():
                    raise BroadcastError("independent_output_requires_unique_stream")
            else:
                if data.primary_url or data.backup_url or data.stream_key or not data.channel_id:
                    raise BroadcastError("api_channel_required", 422)
                account = db.execute(
                    "SELECT status FROM youtube_accounts WHERE channel_id=?", (data.channel_id,)
                ).fetchone()
                if not account or account["status"] != "connected":
                    raise BroadcastError("youtube_account_unavailable")
                fingerprint = None
            output_id, route_id = secrets.token_hex(16), secrets.token_hex(16)
            now = utc_now()
            db.execute(
                "INSERT INTO broadcast_outputs(id,session_id,name,mode,visibility,scheduled_start,"
                "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    output_id,
                    session_id,
                    data.name,
                    data.mode,
                    data.visibility,
                    data.scheduled_start,
                    "READY" if credentials else "DRAFT",
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO broadcast_routes(id,output_id,node_id,role,youtube_slot,created_at,"
                "updated_at) VALUES (?,?,?,'current','PRIMARY',?,?)",
                (route_id, output_id, data.node_id, now, now),
            )
            db.execute(
                "INSERT INTO youtube_bindings(output_id,channel_id,credentials_encrypted,"
                "credential_fingerprint,has_backup,updated_at) VALUES (?,?,?,?,?,?)",
                (
                    output_id,
                    data.channel_id,
                    self.seal(credentials) if credentials else None,
                    fingerprint,
                    bool(data.backup_url),
                    now,
                ),
            )
            if data.mode == "youtube_api":
                db.execute(
                    "INSERT INTO youtube_operations(output_id,marker,updated_at) VALUES (?,?,?)",
                    (output_id, f"adojapan:{output_id}", now),
                )
            self.event(db, session_id, "output.created", output_id=output_id)
            self.remember(db, scope, key, value, output_id)
            return output_id

    def add_route(self, output_id: str, node_id: str, key: str) -> str:
        scope = f"route:{output_id}"
        with self.transaction() as db:
            prior = self.replay(db, scope, key, node_id)
            if prior:
                return prior
            output = self.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
            self.node(db, node_id)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM broadcast_routes WHERE output_id=?", (output_id,)
                ).fetchone()[0]
                >= 8
            ):
                raise BroadcastError("route_limit")
            if db.execute(
                "SELECT id FROM broadcast_routes WHERE output_id=? AND node_id=?",
                (output_id, node_id),
            ).fetchone():
                raise BroadcastError("route_exists")
            route_id = secrets.token_hex(16)
            now = utc_now()
            db.execute(
                "INSERT INTO broadcast_routes(id,output_id,node_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (route_id, output_id, node_id, now, now),
            )
            self.event(db, output["session_id"], "route.created", output_id=output_id)
            self.remember(db, scope, key, node_id, route_id)
            return route_id

    def intent(self, output_id: str, enabled: bool, key: str) -> str:
        scope = f"intent:{output_id}"
        with self.transaction() as db:
            prior = self.replay(db, scope, key, enabled)
            if prior:
                return prior
            output = self.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
            if db.execute(
                "SELECT 1 FROM broadcast_switches WHERE output_id=? AND active=1", (output_id,)
            ).fetchone():
                raise BroadcastError("switch_in_progress")
            binding = self.row(db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,))
            if enabled and not binding["credentials_encrypted"]:
                raise BroadcastError("output_not_provisioned")
            db.execute(
                "UPDATE broadcast_outputs SET desired_enabled=?,generation=generation+1,"
                "state=?,updated_at=? WHERE id=?",
                (enabled, "START_REQUESTED" if enabled else "STOP_REQUESTED", utc_now(), output_id),
            )
            db.execute(
                "UPDATE broadcast_routes SET desired_enabled=?,generation=generation+1 "
                "WHERE output_id=? AND role='current'",
                (enabled, output_id),
            )
            self.event(
                db,
                output["session_id"],
                "output.start_requested" if enabled else "output.stop_requested",
                output_id=output_id,
            )
            self.remember(db, scope, key, enabled, output_id)
            return output_id

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        with self.database.connect() as db:
            sessions = db.execute(
                "SELECT s.*,src.ingress_node_id,src.profile_json FROM broadcast_sessions s "
                "JOIN broadcast_sources src ON src.id=s.source_id "
                "WHERE (? IS NULL OR s.id=?) ORDER BY s.created_at DESC LIMIT 100",
                (session_id, session_id),
            ).fetchall()
            result = []
            for session in sessions:
                item = dict(session)
                item["profile"] = json.loads(item.pop("profile_json"))
                item["outputs"] = []
                for output in db.execute(
                    "SELECT * FROM broadcast_outputs WHERE session_id=?", (session["id"],)
                ):
                    out = dict(output)
                    binding = self.row(
                        db,
                        "SELECT channel_id,broadcast_id,stream_id,"
                        "lifecycle_status,stream_status,health_status,has_backup "
                        "FROM youtube_bindings WHERE output_id=?",
                        (output["id"],),
                    )
                    out["youtube"] = dict(binding)
                    out["viewer_playback"] = "UNKNOWN"
                    out["routes"] = [
                        dict(r)
                        for r in db.execute(
                            "SELECT id,node_id,role,youtube_slot,source_kind,"
                            "desired_enabled,generation "
                            "FROM broadcast_routes WHERE output_id=?",
                            (output["id"],),
                        )
                    ]
                    out["switch"] = next(
                        (
                            dict(r)
                            for r in db.execute(
                                "SELECT id,state,old_route_id,target_route_id,cutover_at,"
                                "safe_error_code FROM broadcast_switches "
                                "WHERE output_id=? ORDER BY created_at DESC LIMIT 1",
                                (output["id"],),
                            )
                        ),
                        None,
                    )
                    item["outputs"].append(out)
                item["events"] = [
                    dict(e)
                    for e in db.execute(
                        "SELECT id,output_id,switch_id,event_type,created_at FROM broadcast_events "
                        "WHERE session_id=? ORDER BY id DESC LIMIT 100",
                        (session["id"],),
                    )
                ]
                result.append(item)
            nodes = [
                dict(n)
                for n in db.execute(
                    "SELECT id,display_name,status,last_seen_at,"
                    "capabilities_json FROM restream_nodes LIMIT 100"
                )
            ]
            accounts = [
                dict(a)
                for a in db.execute("SELECT channel_id,display_name,status FROM youtube_accounts")
            ]
            return {
                "sessions": result,
                "nodes": nodes,
                "accounts": accounts,
                "limits": self.limits.model_dump(),
                "auto_failover_enabled": False,
            }
