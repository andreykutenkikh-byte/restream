"""Continuous decoded video AND audio observation of two full handoffs per mode."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from app.broadcast.media_runtime import stop
from app.broadcast.selector import FLV_HEADER, Selector, Tag
from scripts.broadcast_media_lab import run_command
from scripts.broadcast_switch_lab import SwitchingLab


class DecodeObserver:
    """One decoder connection spanning the seam; no restarted ffprobe timing."""

    def __init__(self, ffmpeg: str, url: str) -> None:
        self.errors: set[str] = set()
        self.error_at = 0.0
        self.samples: dict[str, deque[tuple[float, float, str]]] = {
            "video": deque(maxlen=18000),
            "audio": deque(maxlen=30000),
        }
        self.process = subprocess.Popen(  # noqa: S603 - loopback synthetic sink only
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-xerror",
                "-rw_timeout",
                "30000000",
                "-rtmp_live",
                "live",
                "-rtmp_buffer",
                "0",
                "-threads",
                "2",
                "-probesize",
                "4194304",
                "-analyzeduration",
                "3000000",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                "showinfo",
                "-af",
                "ashowinfo",
                "-threads",
                "2",
                "-f",
                "null",
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            # Raw lines/URLs/credentials never leave this reader or enter evidence.
            for signature in (
                "Invalid",
                "corrupt",
                "non monoton",
                "timed out",
                "Error",
                "End of file",
                "Broken pipe",
                "Connection",
                "Invalid NAL unit size",
                "corrupt decoded frame",
                "non-existing PPS",
                "Missing reference",
                "concealing",
                "error while decoding",
            ):
                if signature in line:
                    self.errors.add(signature)
                    self.error_at = self.error_at or time.monotonic()
            if "pts_time:" not in line or "checksum:" not in line:
                continue
            kind = "audio" if "ashowinfo" in line else "video"
            pts = re.search(r"pts_time:([-\d.]+)", line)
            checksum = re.search(r"checksum:([A-F0-9]+)", line)
            if pts and checksum:
                self.samples[kind].append((time.monotonic(), float(pts[1]), checksum[1]))

    def ready(self) -> bool:
        return self.process.poll() is None and all(len(v) >= 90 for v in self.samples.values())

    def result(self, began: float, ended: float) -> dict[str, Any]:
        assert self.process.poll() is None, "continuous_decoder_failed:" + ",".join(
            sorted(self.errors)
        )
        assert not self.errors, "continuous_decode_errors:" + ",".join(sorted(self.errors))
        result: dict[str, Any] = {"decoder_pid_preserved": True, "decode": "PASS"}
        for kind, values in self.samples.items():
            all_rows = list(values)
            rows = [r for r in all_rows if began - 2 <= r[0] <= ended + 2]
            assert len(rows) >= 90, "insufficient_continuous_decoded_segment"
            assert all(b[1] > a[1] for a, b in zip(rows, rows[1:], strict=False)), (
                "decoded_pts_regression"
            )
            gaps = [1000 * (b[0] - a[0]) for a, b in zip(rows, rows[1:], strict=False)]
            media_gaps = [1000 * (b[1] - a[1]) for a, b in zip(rows, rows[1:], strict=False)]
            result[kind] = {
                "decoded_samples": len(rows),
                "max_arrival_gap_ms": round(max(gaps), 1),
                "max_pts_gap_ms": round(max(media_gaps), 1),
                "adjacent_repeated_checksums": sum(
                    a[2] == b[2] for a, b in zip(rows, rows[1:], strict=False)
                ),
                "first_after_decision_ms": round(
                    1000 * (next(r[0] for r in all_rows if r[0] >= ended) - ended), 1
                ),
            }
        return result

    def close(self) -> None:
        stop(self.process)
        self.thread.join(timeout=3)
        if self.process.stderr:
            self.process.stderr.close()


def decoded_boundary(ffmpeg: str, selector: Selector) -> tuple[str, float]:
    """Decode the selected IDR once, only in the synthetic lab, for receiver correlation."""
    assert selector.boundary
    video, audio = selector.boundary
    data = FLV_HEADER + b"".join(Tag(k, 0, selector.config[k].data).encode() for k in (9, 8))
    data += video.encode(-video.dts) + audio.encode(-video.dts)
    result = subprocess.run(  # noqa: S603 - only in-memory synthetic IDR, no transport credentials
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-threads",
            "2",
            "-f",
            "flv",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-vf",
            "showinfo",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        input=data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    lines = result.stderr.decode(errors="replace").splitlines()
    checksums = [
        m[1]
        for line in lines
        if "Parsed_showinfo_" in line
        if (m := re.search(r"checksum:([A-F0-9]+)", line))
    ]
    assert result.returncode == 0 and checksums, "selected_idr_decode_failed"
    return checksums[0], (audio.pts - video.pts) / 1000


class HandoffLab(SwitchingLab):
    def __init__(self, mediamtx: str, ffmpeg: str, ffprobe: str, directory: Path) -> None:
        self.source_receipts: dict[str, list[dict[str, Any]]] = {}
        super().__init__(mediamtx, ffmpeg, ffprobe, directory)

    def sample_sources(self, client: httpx.Client) -> None:
        for node, runtime in list(self.runtimes.items()):
            try:
                response = client.get(
                    f"http://127.0.0.1:{runtime.ports.api}/v3/paths/get/source/{self.source_id}/direct"
                )
                if not response.is_success or not response.json().get("ready"):
                    continue
                item = response.json()
                rows = self.source_receipts.setdefault(node, [])
                identity = item["source"]["id"]
                if not rows or rows[-1]["id"] != identity:
                    rows.append({"id": identity, "bytes": 0, "first": 0.0, "last": 0.0})
                    rows[:] = rows[-16:]
                row = rows[-1]
                if item["bytesReceived"] > row["bytes"]:
                    stamp = time.monotonic()
                    row["first"] = row["first"] or stamp
                    row["last"], row["bytes"] = stamp, item["bytesReceived"]
            except (httpx.HTTPError, ValueError, KeyError):
                continue

    def process_failures(self) -> None:
        runtime = self.runtimes["relay-c"]
        route = next(r for r in runtime.plan["routes"] if r["output_id"] == self.outputs[0])
        worker = runtime.publishers[route["id"]][1]
        assert worker.process and worker.selector
        pid, frames = worker.process.pid, worker.frames
        selected = worker.selector.inputs[worker.selector.selected]
        stop(selected.process)
        self.wait(lambda: worker.connected and worker.frames >= frames + 90)
        assert worker.process.pid == pid and worker.selector.input_restarts
        self.report["stages"]["selector_reader_crash"] = {
            "status": "PASS",
            "publisher_preserved": True,
            "bounded_input_restart": True,
            "credential_reentry": False,
        }
        stop(worker.process)
        self.wait(
            lambda: bool(
                worker.process
                and worker.process.pid != pid
                and worker.connected
                and worker.frames >= 90
            )
        )
        self.report["stages"]["publisher_crash"] = {
            "status": "PASS",
            "bounded_publisher_restart": True,
            "output_binding_preserved": True,
            "continuous_connection_claimed": False,
        }

    def incompatible_direct(self, node: str, target: str, operation: str) -> None:
        """Actual authenticated 44.1 kHz source must not displace healthy 48 kHz forward."""
        fixture = self.directory / "incompatible-audio.mp4"
        run_command(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(self.directory / "source.mp4"),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-t",
                "6",
                str(fixture),
            ]
        )
        runtime = self.runtimes[node]
        worker = runtime.publishers[target][1]
        assert worker.process and worker.selector
        pid, frames = worker.process.pid, worker.frames
        incompatible = self.phone(node, fixture=fixture)
        self.processes.append(incompatible)
        direct = f"source/{self.source_id}/direct"
        try:
            self.wait(
                lambda: (
                    direct in worker.selector.inputs
                    and min(worker.selector.inputs[direct].counts.values()) >= 90
                    and worker.frames >= frames + 90
                )
            )
            assert incompatible.poll() is None
            assert worker.process.pid == pid and worker.connected
            assert worker.selector.selected.startswith("forward/")
            assert self.state(operation) == "AWAITING_DIRECT_SOURCE"
            self.report["stages"]["incompatible_direct"] = {
                "status": "PASS",
                "actual_input_audio_hz": 44100,
                "required_audio_hz": 48000,
                "forwarded_retained": True,
                "publisher_preserved": True,
                "direct_not_confirmed": True,
            }
        finally:
            stop(incompatible)

    def source_returns_old(self) -> None:
        """A reconnect to a previous node is not proof of the current direct path."""
        runtime = self.runtimes["relay-c"]
        route = next(r for r in runtime.plan["routes"] if r["output_id"] == self.outputs[0])
        worker = runtime.publishers[route["id"]][1]
        assert worker.process and self.source_process
        pid = worker.process.pid
        stop(self.source_process)
        self.source_process = self.phone("relay-b")
        self.processes.append(self.source_process)
        direct_path = f"source/{self.source_id}/direct"

        def path_ready(node: str) -> bool:
            response = self.runtimes[node].http.get(f"/v3/paths/get/{direct_path}")
            return bool(response.is_success and response.json().get("ready"))

        self.wait(
            lambda: (
                any(
                    o["route_id"] == route["id"] and o["source_kind"] == "unknown"
                    for o in runtime.last_observations
                )
                and not worker.connected
                and path_ready("relay-b")
            )
        )
        assert worker.process.pid == pid
        with self.database.connect() as db:
            assert (
                db.execute(
                    "SELECT ingress_node_id FROM broadcast_sources WHERE id=?", (self.source_id,)
                ).fetchone()[0]
                == "relay-c"
            )
        # A killed SRT sender can remain registered until transport timeout.
        # Replacement is disabled: observe actual release before restoration,
        # rather than hiding a rejected second publisher with repeated attempts.
        self.wait(lambda: not path_ready("relay-c"))
        stop(self.source_process)
        self.source_process = self.phone("relay-c")
        self.processes.append(self.source_process)
        frames = worker.frames
        self.wait(
            lambda: (
                worker.connected
                and worker.frames >= 90
                and (worker.process.pid != pid or worker.frames >= frames + 90)
            )
        )
        self.report["stages"]["source_returns_old"] = {
            "status": "PASS",
            "current_route_reported_unknown": True,
            "old_node_received_source": True,
            "no_implicit_ingress_move": True,
            "restored_direct_without_credential_reentry": True,
            "publisher_preserved": worker.process.pid == pid,
            "prolonged_absence_may_exceed_receiver_idle_timeout": True,
        }

    def handoffs(self, modes: list[str]) -> None:
        output = self.outputs[0]
        targets = {
            node: self.store.add_route(output, node, secrets.token_hex(16))
            for node in ("relay-b", "relay-c")
        }
        for runtime in self.runtimes.values():
            runtime.test_destinations[output + ":PRIMARY"] = self.destination("0")
            runtime.test_destinations[output + ":BACKUP"] = self.destination("3")
        with self.database.connect() as db:
            binding = tuple(
                db.execute(
                    "SELECT output_id,credential_fingerprint FROM youtube_bindings "
                    "WHERE output_id=?",
                    (output,),
                ).fetchone()
            )
        old_node, old_sink = "relay-a", 0
        for mode in modes:
            for node in ("relay-b", "relay-c"):
                target = targets[node]
                identifier = self.switcher.request(output, target, secrets.token_hex(16))
                self.until(identifier, "AWAITING_DIRECT_SOURCE")
                sink = 3 if old_sink == 0 else 0
                worker = self.runtimes[node].publishers[target][1]
                assert worker.process and worker.selector
                publisher_pid = worker.process.pid
                selector = worker.selector
                if "incompatible_direct" not in self.report["stages"]:
                    self.incompatible_direct(node, target, identifier)
                before = self.record([sink], f"{mode}-{node}-forwarded")
                observer = DecodeObserver(
                    self.ffmpeg,
                    f"rtmp://127.0.0.1:{self.sink_rtmp}/out/{sink}?user=reader&pass={self.reader_secret}",
                )
                try:
                    self.wait(observer.ready)
                    assert self.source_process
                    old_source = self.source_process
                    old_receipt = self.source_receipts[old_node][-1]
                    began = time.monotonic()
                    if mode == "reconnect":
                        stop(old_source)
                        time.sleep(1)  # Explicit real absence, not an overlap target violation.
                    self.source_process = self.phone(node)
                    self.processes.append(self.source_process)
                    self.until(identifier, "COMPLETED")
                    assert worker.selector is selector and worker.process.pid == publisher_pid
                    assert selector.events and selector.error is None
                    event = dict(selector.events[-1])
                    decision = float(event["decision_at"])
                    checksum, av_offset = decoded_boundary(self.ffmpeg, selector)
                    if mode == "overlap":
                        assert old_source.poll() is None
                        stop(old_source)
                    deadline = time.monotonic() + 4
                    self.wait(lambda deadline=deadline: time.monotonic() >= deadline)
                    self.report["latest_observer"] = {
                        "errors": sorted(observer.errors),
                        "error_after_source_stop_ms": round((observer.error_at - began) * 1000, 1)
                        if observer.error_at
                        else None,
                        "decision_after_source_stop_ms": round((decision - began) * 1000, 1),
                        "process_exit": observer.process.poll(),
                        "selector_input_faults": selector.input_faults,
                        "timestamp_faults": selector.timestamp_faults,
                    }
                    continuous = observer.result(began, decision)
                    direct_video = next(
                        r
                        for r in observer.samples["video"]
                        if r[0] >= decision - 0.01 and r[2] == checksum
                    )
                    direct_audio = next(
                        r
                        for r in observer.samples["audio"]
                        if abs(r[1] - (direct_video[1] + av_offset)) < 0.002
                    )
                    continuous["first_direct_video_ms"] = round(
                        1000 * (direct_video[0] - decision), 1
                    )
                    continuous["first_direct_audio_ms"] = round(
                        1000 * (direct_audio[0] - decision), 1
                    )
                    continuous["direct_video_proof"] = "decoded checksum of selected IDR"
                    continuous["direct_audio_proof"] = (
                        "decoded audio PTS at mapped IDR + preserved AV offset"
                    )
                    continuous["transport"] = (
                        "persistent RTMP reader; no RTSP observer clock conversion"
                    )
                    new_receipt = self.source_receipts[node][-1]
                    source_absence = max(0, (new_receipt["first"] - old_receipt["last"]) * 1000)
                    selected_input = selector.inputs[selector.selected]
                    if mode == "overlap":
                        assert continuous["video"]["max_arrival_gap_ms"] <= 1000, (
                            "overlap_video_gap"
                        )
                        assert continuous["audio"]["max_arrival_gap_ms"] <= 1000, (
                            "overlap_audio_gap"
                        )
                    assert selector.old_tail_packets == 0, "active_packets_discarded"
                    after = self.record([sink], f"{mode}-{node}-direct")
                    with self.database.connect() as db:
                        assert (
                            tuple(
                                db.execute(
                                    "SELECT output_id,credential_fingerprint FROM youtube_bindings "
                                    "WHERE output_id=?",
                                    (output,),
                                ).fetchone()
                            )
                            == binding
                        )
                    self.report["stages"][f"{mode}_{old_node}_to_{node}"] = {
                        "status": "PASS",
                        "same_output_and_credential": True,
                        "publisher_preserved": True,
                        "selector": event,
                        "source_sender_mode": "simultaneous_synthetic_senders"
                        if mode == "overlap"
                        else "one_sender_stop_then_reconnect",
                        "source_absence_ms": round(source_absence, 1),
                        "source_absence_boundary": "MediaMTX ingress bytes; nominal 50ms poll",
                        "first_direct_av_reader_ms": round(
                            1000 * (max(selected_input.first_arrivals.values()) - began), 1
                        ),
                        "deliberate_reconnect_wait_ms": 1000 if mode == "reconnect" else 0,
                        "preparation_until_decision_ms": round((decision - began) * 1000, 1),
                        "continuous_receiver": continuous,
                        "active_packet_drops": selector.old_tail_packets,
                        "input_timestamp_rounding_repairs": {
                            p.split("/")[0]: i.rounding_repairs for p, i in selector.inputs.items()
                        },
                        "preselection_packets": {
                            p.split("/")[0]: i.preselection_packets
                            for p, i in selector.inputs.items()
                        },
                        "before_media": before,
                        "after_media": after,
                    }
                finally:
                    observer.close()
                old_node, old_sink = node, sink


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=["overlap", "reconnect", "both"], default="both")
    parser.add_argument("--focused", action="store_true")
    args = parser.parse_args()
    lab = HandoffLab(args.mediamtx, args.ffmpeg, args.ffprobe, args.directory)
    try:
        lab.setup()
        if args.focused:
            lab.wait(lambda: lab.ready("relay-a", lab.routes[0]))
            for output in lab.outputs[1:]:
                lab.store.intent(output, False, secrets.token_hex(16))
            lab.step()
        else:
            lab.multicast()
            lab.relocate_independent_output()
        lab.handoffs(["reconnect", "overlap"] if args.mode == "both" else [args.mode])
        lab.process_failures()
        lab.source_returns_old()
        lab.expiry_and_cold_restart()
        lab.report["status"] = "PASS"
    except BaseException as exc:
        lab.report["status"] = "FAIL"
        lab.report["failure"] = type(exc).__name__
        lab.report["failure_code"] = str(exc) if isinstance(exc, AssertionError) else "lab_failed"
        lab.report["runtime"] = {
            node: {
                rid: {
                    "selected_kind": w.selector.selected.split("/")[0],
                    "error": w.selector.error,
                    "rejection": w.selector.rejection,
                    "frames": w.selector.frames,
                    "input_errors": [i.error for i in w.selector.inputs.values()],
                }
                for rid, (_, w) in r.publishers.items()
                if w.selector
            }
            for node, r in lab.runtimes.items()
        }
        raise
    finally:
        (args.directory / "report.json").write_text(
            json.dumps(lab.report, indent=2), encoding="utf-8"
        )
        lab.close()
        print(json.dumps({"status": lab.report["status"], "stages": list(lab.report["stages"])}))


if __name__ == "__main__":
    main()
