"""Durable, leased make-before-break controller. Never creates a YouTube resource."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.broadcast.media_control import MediaControl
from app.broadcast.models import BroadcastError, SwitchState
from app.broadcast.store import BroadcastStore
from app.db import utc_now

TERMINAL = {"COMPLETED", "EGRESS_SWITCH_COMPLETED", "FAILED", "CANCELLED"}
PRE_CUTOVER = {
    "REQUESTED",
    "VALIDATING_TARGET",
    "PREPARING_TARGET",
    "TARGET_MEDIA_READY",
    "TARGET_CREDENTIAL_LEASED",
    "TARGET_EGRESS_STARTING",
    "TARGET_YOUTUBE_CONNECTED",
    "CUTOVER_ARMED",
}


class SwitchController:
    def __init__(self, store: BroadcastStore, media: MediaControl) -> None:
        self.store, self.media = store, media
        self.owner = secrets.token_hex(16)

    def request(
        self, output_id: str, target_route_id: str, key: str, *, handoff_ingress: bool = True
    ) -> str:
        scope = f"switch:{output_id}"
        intent = [target_route_id, handoff_ingress]
        with self.store.transaction() as db:
            prior = self.store.replay(db, scope, key, intent)
            if prior:
                return prior
            output = self.store.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
            if not output["desired_enabled"]:
                raise BroadcastError("output_not_running")
            old = self.store.row(
                db,
                "SELECT * FROM broadcast_routes WHERE output_id=? AND role='current'",
                (output_id,),
            )
            target = self.store.row(
                db,
                "SELECT * FROM broadcast_routes WHERE output_id=? AND id=?",
                (output_id, target_route_id),
            )
            if old["id"] == target["id"]:
                raise BroadcastError("target_is_current")
            if db.execute(
                "SELECT 1 FROM broadcast_switches WHERE output_id=? AND active=1", (output_id,)
            ).fetchone():
                raise BroadcastError("switch_in_progress")
            if (
                handoff_ingress
                and db.execute(
                    "SELECT 1 FROM broadcast_switches w JOIN broadcast_outputs o "
                    "ON o.id=w.output_id "
                    "WHERE o.session_id=? AND w.active=1",
                    (output["session_id"],),
                ).fetchone()
            ):
                raise BroadcastError("source_handoff_in_progress")
            binding = self.store.row(
                db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,)
            )
            if not binding["has_backup"] or not binding["credentials_encrypted"]:
                raise BroadcastError("youtube_dual_ingest_required")
            self.media.admit(db, output_id, target_route_id)
            self.media.ready_node(db, old["node_id"], capability="route_switch_v1")
            self.media.ready_node(db, target["node_id"], capability="youtube_dual_ingest_v1")
            slot = "BACKUP" if old["youtube_slot"] == "PRIMARY" else "PRIMARY"
            if db.execute(
                "SELECT 1 FROM broadcast_routes WHERE output_id=? AND youtube_slot=?",
                (output_id, slot),
            ).fetchone():
                raise BroadcastError("youtube_slot_not_free")
            identifier, now = secrets.token_hex(16), utc_now()
            db.execute(
                "INSERT INTO broadcast_switches(id,output_id,old_route_id,target_route_id,"
                "state,created_at,updated_at,durations_json) VALUES (?,?,?,?,'REQUESTED',?,?,?)",
                (
                    identifier,
                    output_id,
                    old["id"],
                    target["id"],
                    now,
                    now,
                    json.dumps({"REQUESTED": now, "handoff_ingress": handoff_ingress}),
                ),
            )
            self.store.remember(db, scope, key, intent, identifier)
            self.store.event(
                db,
                output["session_id"],
                "switch.REQUESTED",
                output_id=output_id,
                switch_id=identifier,
            )
            return identifier

    def cancel(self, switch_id: str) -> None:
        with self.store.transaction() as db:
            switch = self._switch(db, switch_id)
            if switch["state"] == "CANCELLED":
                return
            if switch["cutover_at"] or switch["state"] not in PRE_CUTOVER | {"ROLLING_BACK"}:
                raise BroadcastError("cancel_after_cutover_forbidden")
            self._rollback(db, switch, "operator_cancelled")

    def _switch(self, db: Any, switch_id: str) -> dict[str, Any]:
        return dict(
            self.store.row(
                db,
                "SELECT w.*,o.session_id FROM broadcast_switches w "
                "JOIN broadcast_outputs o ON o.id=w.output_id WHERE w.id=?",
                (switch_id,),
            )
        )

    def _state(self, db: Any, switch: dict[str, Any], state: str) -> None:
        SwitchState(state)
        now = utc_now()
        history = json.loads(switch["durations_json"])
        history[state] = now
        elapsed = int(
            1000
            * (
                datetime.fromisoformat(now) - datetime.fromisoformat(switch["created_at"])
            ).total_seconds()
        )
        duration_names = {
            "PREPARING_TARGET": "prepare_ms",
            "TARGET_MEDIA_READY": "media_ready_ms",
            "TARGET_YOUTUBE_CONNECTED": "egress_ready_ms",
            "OLD_EGRESS_DRAINING": "cutover_ms",
            "DIRECT_SOURCE_CONFIRMED": "direct_source_ms",
            "COMPLETED": "total_ms",
            "EGRESS_SWITCH_COMPLETED": "total_ms",
            "FAILED": "total_ms",
            "CANCELLED": "total_ms",
        }
        if state in duration_names:
            history[duration_names[state]] = elapsed
        db.execute(
            "UPDATE broadcast_switches SET "
            "state=?,active=?,updated_at=?,durations_json=? WHERE id=?",
            (state, state not in TERMINAL, now, json.dumps(history), switch["id"]),
        )
        self.store.event(
            db,
            switch["session_id"],
            f"switch.{state}",
            output_id=switch["output_id"],
            switch_id=switch["id"],
        )
        switch["state"], switch["durations_json"] = state, json.dumps(history)

    def _rollback(self, db: Any, switch: dict[str, Any], code: str) -> None:
        db.execute(
            "UPDATE broadcast_switches SET safe_error_code=? WHERE id=?", (code, switch["id"])
        )
        switch["safe_error_code"] = code
        db.execute(
            "UPDATE broadcast_routes SET desired_enabled=0,media_warm=0,generation=generation+1 "
            "WHERE id=? AND (desired_enabled=1 OR media_warm=1)",
            (switch["target_route_id"],),
        )
        self.media.egress.sync(db, switch["output_id"])
        if switch["state"] != "ROLLING_BACK":
            self._state(db, switch, "ROLLING_BACK")

    @staticmethod
    def _observation(db: Any, route_id: str) -> Any:
        return db.execute(
            "SELECT * FROM broadcast_media_observations WHERE route_id=?", (route_id,)
        ).fetchone()

    def media_ready(
        self, db: Any, route_id: str, *, publisher: bool = False, direct: bool = False
    ) -> bool:
        obs = self._observation(db, route_id)
        if (
            not obs
            or (datetime.now(UTC) - datetime.fromisoformat(obs["observed_at"])).total_seconds() > 5
        ):
            return False
        node = db.execute(
            "SELECT generation FROM broadcast_media_nodes WHERE node_id=?", (obs["node_id"],)
        ).fetchone()
        return bool(
            node
            and obs["plan_generation"] == node["generation"]
            and obs["valid_samples"] >= 2
            and obs["source_identity"]
            and obs["video_frames"] >= 90
            and obs["audio_packets"] >= 90
            and obs["video_pts"] is not None
            and obs["audio_pts"] is not None
            and obs["bitrate_bps"]
            and not obs["safe_error_code"]
            and (
                not publisher
                or (
                    obs["publisher_connected"]
                    and obs["publisher_running"]
                    and obs["runtime_secret_present"]
                    and obs["publisher_frames"] >= 90
                    and obs["publisher_bytes"] > 0
                )
            )
            and (not direct or (obs["source_kind"] == "direct" and obs["direct_samples"] >= 2))
        )

    def stopped(self, db: Any, route_id: str, after: str) -> bool:
        obs = self._observation(db, route_id)
        if (
            obs
            and obs["observed_at"] >= after
            and not obs["publisher_running"]
            and not obs["runtime_secret_present"]
        ):
            node = db.execute(
                "SELECT generation FROM broadcast_media_nodes WHERE node_id=?", (obs["node_id"],)
            ).fetchone()
            authority = db.execute(
                "SELECT a.generation FROM broadcast_egress_authority a "
                "JOIN broadcast_routes r ON r.output_id=a.output_id WHERE r.id=?",
                (route_id,),
            ).fetchone()
            if (
                node
                and authority
                and obs["plan_generation"] == node["generation"]
                and obs["egress_generation"] == authority["generation"]
            ):
                return True
        # Missing or stale acknowledgments cannot free a slot before the last
        # possible grant expires. They must not prevent reuse after that deadline.
        latest = db.execute(
            "SELECT MAX(expires_at) FROM broadcast_egress_leases WHERE route_id=?", (route_id,)
        ).fetchone()[0]
        return bool(
            latest and datetime.now(UTC) > datetime.fromisoformat(latest) + timedelta(seconds=5)
        )

    def tick(self) -> None:
        with self.store.database.connect() as db:
            identifiers = [
                r[0]
                for r in db.execute("SELECT id FROM broadcast_switches WHERE active=1 LIMIT 32")
            ]
        for identifier in identifiers:
            self.step(identifier)

    def step(self, switch_id: str) -> None:
        with self.store.transaction() as db:
            switch = self._switch(db, switch_id)
            if not switch["active"]:
                return
            now = utc_now()
            if (
                switch["lease_owner"] != self.owner
                and switch["lease_until"]
                and switch["lease_until"] > now
            ):
                return
            generation = switch["generation"] + (switch["lease_owner"] != self.owner)
            db.execute(
                "UPDATE broadcast_switches SET lease_owner=?,lease_until=?,generation=? WHERE id=?",
                (
                    self.owner,
                    (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
                    generation,
                    switch_id,
                ),
            )
            old = self.store.row(
                db, "SELECT * FROM broadcast_routes WHERE id=?", (switch["old_route_id"],)
            )
            target = self.store.row(
                db, "SELECT * FROM broadcast_routes WHERE id=?", (switch["target_route_id"],)
            )
            state = switch["state"]
            if (
                state in PRE_CUTOVER
                and (
                    datetime.now(UTC) - datetime.fromisoformat(switch["created_at"])
                ).total_seconds()
                > 120
            ):
                self._rollback(db, switch, "target_preparation_timeout")
                return
            obs = self._observation(db, target["id"])
            if (
                state in PRE_CUTOVER - {"REQUESTED", "VALIDATING_TARGET"}
                and obs
                and obs["safe_error_code"]
            ):
                self._rollback(db, switch, "target_failed_before_cutover")
                return
            if state == "REQUESTED":
                self._state(db, switch, "VALIDATING_TARGET")
            elif state == "VALIDATING_TARGET":
                try:
                    self.media.admit(db, switch["output_id"], target["id"])
                except BroadcastError as exc:
                    self._rollback(db, switch, exc.code)
                    return
                slot = "BACKUP" if old["youtube_slot"] == "PRIMARY" else "PRIMARY"
                db.execute(
                    "UPDATE broadcast_routes SET "
                    "role='warm',youtube_slot=?,media_warm=1,desired_enabled=0,"
                    "generation=generation+1 WHERE id=?",
                    (slot, target["id"]),
                )
                self._state(db, switch, "PREPARING_TARGET")
            elif state == "PREPARING_TARGET":
                if self.media_ready(db, target["id"]):
                    history = json.loads(switch["durations_json"])
                    history["publisher_frames_at_media_ready"] = obs["publisher_frames"]
                    switch["durations_json"] = json.dumps(history)
                    self._state(db, switch, "TARGET_MEDIA_READY")
            elif state == "TARGET_MEDIA_READY":
                if not self.media_ready(db, target["id"]):
                    self._rollback(db, switch, "target_readiness_lost")
                    return
                db.execute(
                    "UPDATE broadcast_routes SET "
                    "desired_enabled=1,generation=generation+1 WHERE id=?",
                    (target["id"],),
                )
                self.media.egress.sync(db, switch["output_id"])
                self._state(db, switch, "TARGET_CREDENTIAL_LEASED")
            elif state == "TARGET_CREDENTIAL_LEASED":
                self._state(db, switch, "TARGET_EGRESS_STARTING")
            elif state == "TARGET_EGRESS_STARTING":
                history = json.loads(switch["durations_json"])
                if (
                    self.media_ready(db, target["id"], publisher=True)
                    and obs["publisher_frames"] > history["publisher_frames_at_media_ready"]
                ):
                    self._state(db, switch, "TARGET_YOUTUBE_CONNECTED")
            elif state == "TARGET_YOUTUBE_CONNECTED":
                if self.media_ready(db, target["id"], publisher=True):
                    self._state(db, switch, "CUTOVER_ARMED")
            elif state == "CUTOVER_ARMED":
                if not self.media_ready(db, target["id"], publisher=True):
                    self._rollback(db, switch, "target_readiness_lost")
                    return
                db.execute(
                    "UPDATE broadcast_routes SET role='standby',desired_enabled=0,"
                    "media_warm=0,generation=generation+1 WHERE id=?",
                    (old["id"],),
                )
                db.execute(
                    "UPDATE broadcast_routes SET role='current',media_warm=0 WHERE id=?",
                    (target["id"],),
                )
                db.execute(
                    "UPDATE broadcast_switches SET cutover_at=? WHERE id=?", (now, switch_id)
                )
                self.media.egress.sync(db, switch["output_id"])
                self._state(db, switch, "OLD_EGRESS_DRAINING")
            elif state == "OLD_EGRESS_DRAINING":
                if self.stopped(db, old["id"], switch["cutover_at"]):
                    db.execute(
                        "UPDATE broadcast_routes SET youtube_slot=NULL WHERE id=?", (old["id"],)
                    )
                    self._state(db, switch, "OLD_CREDENTIAL_REVOKED")
            elif state == "OLD_CREDENTIAL_REVOKED":
                if json.loads(switch["durations_json"])["handoff_ingress"]:
                    self._state(db, switch, "AWAITING_DIRECT_SOURCE")
                else:
                    self._state(db, switch, "EGRESS_SWITCH_COMPLETED")
            elif state == "AWAITING_DIRECT_SOURCE":
                # Forwarded input, however healthy, cannot complete a phone handoff.
                if self.media_ready(db, target["id"], publisher=True, direct=True):
                    self._state(db, switch, "DIRECT_SOURCE_SEEN")
            elif state == "DIRECT_SOURCE_SEEN":
                if self.media_ready(db, target["id"], publisher=True, direct=True):
                    db.execute(
                        "UPDATE broadcast_sources SET ingress_node_id=? WHERE id=("
                        "SELECT source_id FROM broadcast_sessions WHERE id=?)",
                        (target["node_id"], switch["session_id"]),
                    )
                    db.execute(
                        "UPDATE broadcast_forwarding SET enabled=0 WHERE route_id=?",
                        (target["id"],),
                    )
                    history = json.loads(switch["durations_json"])
                    history["source_switch_gap_ms"] = obs["source_switch_gap_ms"]
                    switch["durations_json"] = json.dumps(history)
                    self._state(db, switch, "DIRECT_SOURCE_CONFIRMED")
                else:
                    self._state(db, switch, "AWAITING_DIRECT_SOURCE")
            elif state == "DIRECT_SOURCE_CONFIRMED":
                self._state(db, switch, "REWARMING_OLD_ROUTE")
            elif state == "REWARMING_OLD_ROUTE":
                # Standby has a free slot; its phone path remains unmeasured.
                if self.media_ready(db, target["id"], publisher=True, direct=True):
                    self._state(db, switch, "COMPLETED")
            elif state == "ROLLING_BACK":
                if target["desired_enabled"]:
                    db.execute(
                        "UPDATE broadcast_routes SET "
                        "desired_enabled=0,media_warm=0,generation=generation+1 "
                        "WHERE id=?",
                        (target["id"],),
                    )
                    self.media.egress.sync(db, switch["output_id"])
                after = json.loads(switch["durations_json"])["ROLLING_BACK"]
                never_leased = not db.execute(
                    "SELECT 1 FROM broadcast_egress_leases WHERE route_id=?", (target["id"],)
                ).fetchone()
                if (
                    target["youtube_slot"] is None
                    or never_leased
                    or self.stopped(db, target["id"], after)
                ):
                    db.execute(
                        "UPDATE broadcast_routes SET "
                        "role='standby',youtube_slot=NULL,media_warm=0 WHERE id=?",
                        (target["id"],),
                    )
                    self._state(
                        db,
                        switch,
                        "CANCELLED"
                        if switch["safe_error_code"] == "operator_cancelled"
                        else "FAILED",
                    )
