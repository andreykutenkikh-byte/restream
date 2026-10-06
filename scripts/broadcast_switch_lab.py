"""Real four-node acceptance: multicast relocation, leased A→B→C and phone reconnect."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.broadcast.envelope import public_key, seal
from app.broadcast.media_control import MediaNodeEnable
from app.broadcast.media_runtime import MediaPorts, MediaRuntime, PacketProbe, stop
from app.broadcast.models import ResourceLimits
from app.broadcast.switching import SwitchController
from app.db import utc_now
from scripts.broadcast_media_lab import Lab, unused_ports


class SwitchingLab(Lab):
    def __init__(self, mediamtx: str, ffmpeg: str, ffprobe: str, directory: Path) -> None:
        super().__init__(mediamtx, ffmpeg, ffprobe, directory)
        self.switcher = SwitchController(self.store, self.control)
        self.receivers: dict[int, list[PacketProbe]] = {}
        self.offline: set[str] = set()
        self.sink_receipts: dict[int, list[dict[str, Any]]] = {0: [], 3: []}
        self.end_receipts = threading.Event()
        self.receipt_thread = threading.Thread(target=self.sample_receipts, daemon=True)
        self.receipt_thread.start()

    def sample_receipts(self) -> None:
        # Separate actual receiver progress from ffprobe startup/output buffering.
        with httpx.Client(timeout=1, trust_env=False) as client:
            while not self.end_receipts.wait(0.05):
                try:
                    response = client.get(f"http://127.0.0.1:{self.sink_api}/v3/paths/list")
                    if not response.is_success:
                        continue
                    for item in response.json()["items"]:
                        if item["name"] not in {"out/0", "out/3"} or not item.get("ready"):
                            continue
                        index = int(item["name"].split("/")[1])
                        identity = item["source"]["id"]
                        received = item["bytesReceived"]
                        entries = self.sink_receipts[index]
                        if not entries or entries[-1]["id"] != identity:
                            entries.append(
                                {"id": identity, "bytes": received, "first": 0.0, "last": 0.0}
                            )
                        entry = entries[-1]
                        if received > entry["bytes"]:
                            stamp = time.monotonic()
                            entry["first"] = entry["first"] or stamp
                            entry["last"], entry["bytes"] = stamp, received
                    self.sample_sources(client)
                except (httpx.HTTPError, ValueError, KeyError):
                    continue

    def sample_sources(self, client: httpx.Client) -> None:
        """Optional additional boundary sampling in the extended handoff lab."""

    def step(self) -> None:
        for node, runtime in self.runtimes.items():
            observations = runtime.tick()
            if node not in self.offline:
                self.exchange(node, observations)
        self.observe_sinks()
        self.switcher.tick()
        time.sleep(0.4)

    def observe_sinks(self) -> None:
        with httpx.Client(timeout=2, trust_env=False) as client:
            for index, probes in self.receivers.items():
                response = client.get(f"http://127.0.0.1:{self.sink_api}/v3/paths/get/out/{index}")
                if not response.is_success or not response.json().get("ready"):
                    continue
                identity = str(response.json()["source"]["id"])
                if probes and probes[-1].identity == identity and probes[-1].process.poll() is None:
                    continue
                if probes:
                    probes[-1].close()
                probes.append(
                    PacketProbe(
                        self.ffprobe,
                        f"rtsp://reader:{self.reader_secret}@127.0.0.1:{self.sink_rtsp}/out/{index}",
                        identity,
                    )
                )

    def state(self, identifier: str) -> str:
        with self.database.connect() as db:
            return str(
                db.execute(
                    "SELECT state FROM broadcast_switches WHERE id=?", (identifier,)
                ).fetchone()[0]
            )

    def until(self, identifier: str, state: str) -> None:
        self.wait(lambda: self.state(identifier) == state, seconds=150)

    def add_fourth_node(self) -> None:
        node = "relay-d"
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO restream_nodes(id,display_name,address,resolved_ip,ssh_port,"
                "ssh_username,status,created_at,updated_at) VALUES (?,?,?,'127.0.0.1',22,"
                "'synthetic','ready',?,?)",
                (node, node, node + ".example", utc_now(), utc_now()),
            )
        key = X25519PrivateKey.generate()
        ports = MediaPorts(*unused_ports(4))
        self.keys[node], self.sequence[node], self.boot_ids[node] = key, 0, secrets.token_hex(16)
        self.control.enable(
            node,
            MediaNodeEnable(
                public_key=public_key(key),
                srt_host="127.0.0.1",
                srt_port=ports.srt,
                limits=ResourceLimits(),
            ),
        )
        self.runtimes[node] = MediaRuntime(
            node_id=node,
            private_key=key,
            mediamtx=self.mediamtx,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            directory=self.directory / node,
            ports=ports,
            srt_bind_host="127.0.0.1",
            test_destinations={self.outputs[1] + ":BACKUP": self.destination("4")},
        )
        self.exchange(node, [])

    def relocate_independent_output(self) -> None:
        self.add_fourth_node()
        output = self.outputs[1]
        target = self.store.add_route(output, "relay-d", secrets.token_hex(16))
        self.step()
        assert not any(r["destination"] for r in self.runtimes["relay-d"].plan["routes"])
        a = self.runtimes["relay-a"].publishers[self.routes[0]][1]
        c = self.runtimes["relay-c"].publishers[self.routes[2]][1]
        assert a.process and c.process
        pids = (a.process.pid, c.process.pid)
        identifier = self.switcher.request(
            output, target, secrets.token_hex(16), handoff_ingress=False
        )
        self.until(identifier, "EGRESS_SWITCH_COMPLETED")
        plan = json.dumps(self.runtimes["relay-d"].plan)
        assert "synthetic-independent-1" in plan
        assert "synthetic-independent-0" not in plan and "synthetic-independent-2" not in plan
        assert (a.process.pid, c.process.pid) == pids
        self.report["stages"]["multicast_b_to_d"] = {
            "status": "PASS",
            "credential_reentry": False,
            "other_publisher_pids_preserved": True,
            "media": self.record([4], "moved-b"),
        }
        for output in self.outputs[1:]:
            self.store.intent(output, False, secrets.token_hex(16))
        self.wait(
            lambda: (
                not self.runtimes["relay-d"].publishers and not self.runtimes["relay-c"].publishers
            )
        )

    def switching(self) -> None:
        output = self.outputs[0]
        with self.database.connect() as db:
            db.execute(
                "UPDATE youtube_bindings SET broadcast_id='synthetic-broadcast-identity',"
                "stream_id='synthetic-stream-identity' WHERE output_id=?",
                (output,),
            )
        b = self.store.add_route(output, "relay-b", secrets.token_hex(16))
        c = self.store.add_route(output, "relay-c", secrets.token_hex(16))
        for runtime in self.runtimes.values():
            runtime.test_destinations[output + ":PRIMARY"] = self.destination("0")
            runtime.test_destinations[output + ":BACKUP"] = self.destination("3")
        self.step()
        # Real target publisher rejects authentication before cutover. A must survive.
        current = self.runtimes["relay-a"].publishers[self.routes[0]][1]
        assert current.process
        old_pid = current.process.pid
        self.runtimes["relay-b"].test_destinations[output + ":BACKUP"] = (
            f"rtmp://127.0.0.1:{self.sink_rtmp}/out/forbidden"
        )
        failed = self.switcher.request(output, b, secrets.token_hex(16))
        self.until(failed, "FAILED")
        assert current.connected and current.process.pid == old_pid
        assert b not in self.runtimes["relay-b"].publishers
        assert all(not r["destination"] for r in self.runtimes["relay-b"].plan["routes"])
        self.report["stages"]["rollback_before_cutover"] = {
            "status": "PASS",
            "old_pid_preserved": True,
        }
        self.runtimes["relay-b"].test_destinations[output + ":BACKUP"] = self.destination("3")
        self.receivers = {0: [], 3: []}
        self.wait(lambda: bool(self.receivers[0] and self.receivers[0][-1].video_frames >= 90))
        with self.database.connect() as db:
            identity = tuple(
                db.execute(
                    "SELECT output_id,broadcast_id,stream_id,credential_fingerprint "
                    "FROM youtube_bindings WHERE output_id=?",
                    (output,),
                ).fetchone()
            )
        for index, (old_node, node, target, slot, old_sink, sink) in enumerate(
            [
                ("relay-a", "relay-b", b, "BACKUP", 0, 3),
                ("relay-b", "relay-c", c, "PRIMARY", 3, 0),
            ]
        ):
            # All standby destinations are absent before the request.
            target_plan = next(r for r in self.runtimes[node].plan["routes"] if r["id"] == target)
            assert target_plan["destination"] is None
            old_envelope = self.control.desired(old_node)
            request_key = secrets.token_hex(16)
            identifier = self.switcher.request(output, target, request_key)
            assert self.switcher.request(output, target, request_key) == identifier
            self.until(identifier, "TARGET_MEDIA_READY")
            assert (
                next(r for r in self.runtimes[node].plan["routes"] if r["id"] == target)[
                    "destination"
                ]
                is None
            )
            self.until(identifier, "TARGET_CREDENTIAL_LEASED")
            if index == 0:
                # Discard the controller object after committing the target lease. Durable
                # state, a different owner and the actual ten-second lease govern restart.
                self.switcher = SwitchController(self.store, self.control)
                self.report["stages"]["crash_reconciliation"] = {"restart_after_target_lease": True}
            self.until(identifier, "CUTOVER_ARMED")
            with self.database.connect() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM broadcast_egress_leases WHERE "
                        "output_id=? AND state='ACTIVE'",
                        (output,),
                    ).fetchone()[0]
                    == 2
                )
                old_route = db.execute(
                    "SELECT old_route_id FROM broadcast_switches WHERE id=?", (identifier,)
                ).fetchone()[0]
            assert self.runtimes[old_node].publishers[old_route][1].connected
            assert self.runtimes[node].publishers[target][1].connected
            self.until(identifier, "AWAITING_DIRECT_SOURCE")
            assert old_route not in self.runtimes[old_node].publishers
            assert (
                next(r for r in self.runtimes[old_node].plan["routes"] if r["id"] == old_route)[
                    "destination"
                ]
                is None
            )
            try:
                self.runtimes[old_node].accept(old_envelope)
                raise AssertionError("stale_generation_resurrected")
            except ValueError:
                pass
            self.wait(
                lambda sink=sink: bool(
                    self.receivers[sink] and self.receivers[sink][-1].video_frames >= 90
                )
            )
            old_receiver = self.receivers[old_sink][-1]
            target_receiver = self.receivers[sink][-1]
            packet_observer_gap = max(
                0.0, 1000 * (target_receiver.first_video_packet - old_receiver.last_video_packet)
            )
            old_receipt, target_receipt = (
                self.sink_receipts[old_sink][-1],
                self.sink_receipts[sink][-1],
            )
            assert old_receipt["first"] and target_receipt["first"]
            egress_gap = max(0.0, 1000 * (target_receipt["first"] - old_receipt["last"]))
            assert egress_gap == 0  # Both mock RTMP receivers overlap; viewer playback is untested.
            forwarded_record = self.record([sink], f"switch-{index}-forwarded")
            target_worker = self.runtimes[node].publishers[target][1]
            assert target_worker.process
            target_pid = target_worker.process.pid
            before_outage = target_worker.frames
            self.offline.add(node)
            self.wait(
                lambda worker=target_worker, frames=before_outage: worker.frames >= frames + 90
            )
            assert target_worker.process.pid == target_pid and target_worker.connected
            self.offline.remove(node)
            # Exactly one synthetic phone sender: stop old, then connect directly to target.
            assert self.source_process
            phone_stopped = time.monotonic()
            before_direct_frames = target_receiver.video_frames
            stop(self.source_process)
            self.source_process = self.phone(node)
            self.processes.append(self.source_process)
            self.until(identifier, "COMPLETED")
            self.wait(
                lambda sink=sink, before=before_direct_frames: (
                    self.receivers[sink][-1].video_frames >= before + 90
                )
            )
            new_receiver = self.receivers[sink][-1]
            assert new_receiver is target_receiver
            source_gap = self.runtimes[node].source_switches[target]["gap_ms"]
            assert 0 < source_gap < 30000
            with self.database.connect() as db:
                assert (
                    tuple(
                        db.execute(
                            "SELECT output_id,broadcast_id,stream_id,credential_fingerprint "
                            "FROM youtube_bindings WHERE output_id=?",
                            (output,),
                        ).fetchone()
                    )
                    == identity
                )
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM broadcast_egress_leases WHERE "
                        "output_id=? AND state='ACTIVE'",
                        (output,),
                    ).fetchone()[0]
                    == 1
                )
                states = [
                    r[0].removeprefix("switch.")
                    for r in db.execute(
                        "SELECT event_type FROM broadcast_events WHERE "
                        "switch_id=? AND event_type LIKE 'switch.%' ORDER BY id",
                        (identifier,),
                    )
                ]
            final_process = self.runtimes[node].publishers[target][1].process
            assert final_process is not None
            assert final_process.pid == target_pid, "direct_handoff_replaced_publisher"
            self.report["stages"][f"switch_{old_node}_to_{node}"] = {
                "status": "PASS",
                "slot": slot,
                "same_output_credential_and_binding": True,
                "make_before_break": True,
                "old_runtime_secret_removed": True,
                "stale_generation_resurrection": "REJECTED",
                "controller_outage": "SURVIVES_WITHIN_LEASE",
                "youtube_egress_switch_gap_ms": round(egress_gap, 1),
                "ffprobe_startup_observer_gap_ms": round(packet_observer_gap, 1),
                "egress_measurement": "mock RTMP receiver byte progress at 50ms plus decoded media",
                "phone_direct_takeover_gap_ms": "MEASURED_IN_CONTINUOUS_HANDOFF_LAB",
                "source_switch_gap_ms": round(source_gap, 1),
                "phone_stop_to_completion_ms": round(1000 * (time.monotonic() - phone_stopped), 1),
                "publisher_replaced_for_direct_input": final_process.pid != target_pid,
                "states": states,
                "forwarded_media": forwarded_record,
                "direct_media": self.record([sink], f"switch-{index}-direct"),
            }
        self.report["stages"]["crash_reconciliation"]["status"] = "PASS"

    def expiry_and_cold_restart(self) -> None:
        node = "relay-c"
        runtime = self.runtimes[node]
        route = next(r for r in runtime.plan["routes"] if r["output_id"] == self.outputs[0])
        route_id = route["id"]
        worker = runtime.publishers[route_id][1]
        process = worker.process
        assert process is not None and process.poll() is None
        # A short synthetic grant exercises the real watchdog without changing the
        # production lease TTL or exposing a test-only TTL option on any API/CLI.
        plan = deepcopy(runtime.plan)
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=3)
        for item in plan["routes"]:
            if item["egress_lease"]:
                item["egress_lease"]["expires_at"] = expiry.isoformat()
        envelope = seal(
            public_key(self.keys[node]),
            plan,
            {
                "node_id": node,
                "purpose": "broadcast-desired-v2",
                "generation": runtime.generation + 1,
                "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=120)).isoformat(),
            },
        )
        runtime.accept(envelope)
        self.offline.add(node)
        started = time.monotonic()
        while (
            process.poll() is None or route_id in runtime.publishers or worker.argv
        ) and time.monotonic() - started < 7:
            time.sleep(0.05)  # Deliberately no tick/heartbeat: independent watchdog only.
        assert process.poll() is not None
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        assert route_id not in runtime.publishers and worker.argv == []
        assert all(r["destination"] is None for r in runtime.plan["routes"])
        assert "synthetic-independent" not in runtime.fence_file.read_text(encoding="utf-8")
        try:
            runtime.accept(envelope)
        except ValueError:
            pass
        else:
            raise AssertionError("Expired grant resurrected publisher")
        directory, ports, destinations = (
            runtime.fence_file.parent,
            runtime.ports,
            runtime.test_destinations,
        )
        runtime.close()
        replacement = MediaRuntime(
            node_id=node,
            private_key=self.keys[node],
            mediamtx=self.mediamtx,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            directory=directory,
            ports=ports,
            srt_bind_host="127.0.0.1",
            test_destinations=destinations,
        )
        self.runtimes[node] = replacement
        assert replacement.plan["routes"] == [] and replacement.publishers == {}
        try:
            replacement.accept(envelope)
        except ValueError:
            pass
        else:
            raise AssertionError("Cold restart accepted stale cache")
        assert replacement.publishers == {}
        self.report["stages"]["expiry_and_cold_restart"] = {
            "status": "PASS",
            "synthetic_lease_ttl_ms": 3000,
            "publisher_stop_after_grant_ms": elapsed_ms,
            "watchdog_without_tick_or_heartbeat": "PASS",
            "runtime_secret_removed": True,
            "expired_grant_replay": "REJECTED",
            "cold_restart_stale_cache": "REJECTED",
            "fence_file_has_no_credential": True,
        }

    def close(self) -> None:
        self.end_receipts.set()
        self.receipt_thread.join(timeout=3)
        for probes in self.receivers.values():
            for probe in probes:
                probe.close()
        super().close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    lab = SwitchingLab(args.mediamtx, args.ffmpeg, args.ffprobe, args.directory)
    try:
        lab.setup()
        lab.multicast()
        lab.relocate_independent_output()
        lab.switching()
        lab.expiry_and_cold_restart()
        lab.report["status"] = "PASS"
    except BaseException as exc:
        lab.report["status"] = "FAIL"
        lab.report["failure"] = type(exc).__name__
        lab.report["observations"] = {
            node: runtime.last_observations for node, runtime in lab.runtimes.items()
        }
        with lab.database.connect() as db:
            lab.report["switches"] = [
                dict(row)
                for row in db.execute(
                    "SELECT state,safe_error_code,durations_json FROM broadcast_switches"
                )
            ]
        raise
    finally:
        lab.close()
        (args.directory / "report.json").write_text(
            json.dumps(lab.report, indent=2), encoding="utf-8"
        )
        print(json.dumps(lab.report))


if __name__ == "__main__":
    main()
