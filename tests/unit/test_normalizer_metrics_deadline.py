"""Real loopback HTTP must not turn a socket timeout into an unbounded scrape."""

from __future__ import annotations

import contextlib
import http.server
import io
import runpy
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"


@pytest.fixture
def namespace():
    return runpy.run_path(str(NORMALIZER), run_name="_metrics_deadline_test")


@pytest.fixture
def server():
    stop = threading.Event()
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(1)

        def log_message(self, *_args):
            pass

        def do_GET(self):
            received.append((self.path, self.client_address))
            try:
                self.respond()
            except OSError:
                self.close_connection = True  # Expected when a timed-out client closes.

        def drip(self, chunks):
            for index, chunk in enumerate(chunks):
                if index and stop.wait(0.075):
                    return
                self.wfile.write(chunk)
                self.wfile.flush()

        def respond(self):
            body = b"12345"
            if self.path == "/drip-status":
                self.drip(
                    [
                        b"HTTP/1.",
                        b"1 ",
                        b"200 ",
                        b"OK",
                        b"\r\nContent-Length: 5\r\n\r\n12345",
                    ]
                )
                return
            if self.path == "/drip-headers":
                self.drip(
                    [
                        b"HTTP/1.1 200 OK\r\n",
                        b"X-A: a\r\n",
                        b"X-B: b\r\n",
                        b"X-C: c\r\n",
                        b"Content-Length: 5\r\n\r\n12345",
                    ]
                )
                return
            self.send_response(200)
            if self.path in {"/chunked", "/drip-chunked"}:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            if self.path in {"/close", "/drip-close"}:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.path == "/drip-chunked":
                self.drip([b"1\r\n" + bytes([value]) + b"\r\n" for value in body] + [b"0\r\n\r\n"])
            elif self.path == "/chunked":
                self.wfile.write(b"5\r\n12345\r\n0\r\n\r\n")
            elif self.path in {"/drip-body", "/drip-close"}:
                self.drip([bytes([value]) for value in body])
            else:
                self.wfile.write(body)
            self.wfile.flush()

    listener = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    listener.daemon_threads = False
    thread = threading.Thread(target=lambda: listener.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield listener, received
    finally:
        stop.set()
        listener.shutdown()
        listener.server_close()  # Joins every request thread, including interrupted drips.
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("route", ["/fast", "/chunked", "/close"])
def test_fast_complete_response_and_connection_close_body_are_read(namespace, server, route):
    listener, _received = server
    reader = namespace["MetricsReader"](listener.server_port, route, lambda value: value)
    try:
        assert reader.sample() == (True, "12345")
        assert reader.deadline.expires is None
    finally:
        reader.close()


@pytest.mark.parametrize(
    "route",
    ["/drip-status", "/drip-headers", "/drip-body", "/drip-chunked", "/drip-close"],
)
def test_actual_socket_drip_cannot_extend_complete_request_deadline(namespace, server, route):
    listener, _received = server
    parsed = []
    reader = namespace["MetricsReader"](listener.server_port, route, parsed.append)
    started = time.monotonic()
    try:
        assert reader.sample() == (False, None)
        elapsed = time.monotonic() - started
        assert namespace["METRICS_REQUEST_TIMEOUT_SECONDS"] == 0.2
        # OS scheduling tolerance only; success/cleanup assertions independently
        # reject the old implementation, which accepts these >0.3s responses.
        assert elapsed < 0.6
        assert parsed == []
        assert reader.connection is None
        assert reader.deadline.expires is None
    finally:
        reader.close()


def test_keepalive_reuses_connection_but_starts_a_new_deadline(namespace, server):
    listener, received = server
    reader = namespace["MetricsReader"](listener.server_port, "/fast", len)
    try:
        assert reader.sample() == (True, 5)
        connection = reader.connection
        time.sleep(0.21)  # Idle keepalive time is not part of the next request budget.
        assert reader.sample() == (True, 5)
        assert reader.connection is connection
        assert received[0][1] == received[1][1]
    finally:
        reader.close()


def test_timeout_discards_connection_and_next_request_recovers(namespace, server):
    listener, received = server
    reader = namespace["MetricsReader"](listener.server_port, "/drip-body", len)
    try:
        assert reader.sample() == (False, None)
        assert reader.connection is None
        reader.path = "/fast"
        assert reader.sample() == (True, 5)
        assert received[0][1] != received[1][1]
    finally:
        reader.close()


def test_socket_file_reference_survives_owner_close_then_releases_fd(namespace):
    left, right = socket.socketpair()
    deadline = namespace["_MetricsDeadline"]()
    deadline.begin()
    wrapped = namespace["_DeadlineSocket"](left, deadline)
    response_file = wrapped.makefile("rb")
    try:
        wrapped.close()  # Same ownership sequence as HTTP Connection: close.
        assert left.fileno() >= 0
        right.sendall(b"ok")
        assert response_file.read(2) == b"ok"
        response_file.close()
        assert left.fileno() == -1
    finally:
        response_file.close()
        wrapped.close()
        right.close()


def install_clock(namespace, monkeypatch):
    clock = [10.0]
    monkeypatch.setitem(
        namespace["_MetricsDeadline"].remaining.__globals__,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    return clock


def test_absolute_deadline_expires_at_boundary_and_rearms(namespace, monkeypatch):
    clock = install_clock(namespace, monkeypatch)
    deadline = namespace["_MetricsDeadline"]()
    with pytest.raises(TimeoutError):
        deadline.remaining()
    deadline.begin()
    assert deadline.remaining() == pytest.approx(0.2)
    clock[0] += 0.19
    assert deadline.remaining() == pytest.approx(0.01)
    clock[0] = 10.2
    with pytest.raises(TimeoutError):
        deadline.remaining()
    deadline.end()
    deadline.begin()
    assert deadline.remaining() == pytest.approx(0.2)


def test_recv_uses_remaining_budget_and_rejects_late_success(namespace, monkeypatch):
    clock = install_clock(namespace, monkeypatch)
    deadline = namespace["_MetricsDeadline"]()
    deadline.begin()
    timeouts = []

    class LateBytes(io.BytesIO):
        def readinto(self, buffer):
            count = super().readinto(buffer)
            clock[0] += 0.02
            return count

    wrapped = namespace["_DeadlineSocketReader"](
        LateBytes(b"x"),
        SimpleNamespace(settimeout=timeouts.append),
        deadline,
    )
    try:
        clock[0] += 0.19
        with pytest.raises(TimeoutError):
            wrapped.readinto(bytearray(1))
        assert timeouts == [pytest.approx(0.01)]
    finally:
        wrapped.close()


def test_send_uses_remaining_budget_and_rejects_late_success(namespace, monkeypatch):
    clock = install_clock(namespace, monkeypatch)
    deadline = namespace["_MetricsDeadline"]()
    deadline.begin()
    timeouts, sent = [], []

    def send(data):
        sent.append(data)
        clock[0] += 0.02

    wrapped = namespace["_DeadlineSocket"](
        SimpleNamespace(settimeout=timeouts.append, sendall=send),
        deadline,
    )
    clock[0] += 0.19
    with pytest.raises(TimeoutError):
        wrapped.sendall(b"GET")
    assert sent == [b"GET"]
    assert timeouts == [pytest.approx(0.01)]


def test_connect_uses_remaining_budget_and_rejects_late_success(namespace, monkeypatch):
    clock = install_clock(namespace, monkeypatch)
    deadline = namespace["_MetricsDeadline"]()
    deadline.begin()
    seen = []

    def connect(connection):
        seen.append(connection.timeout)
        clock[0] += 0.02

    base_class = namespace["_MetricsHTTPConnection"].__bases__[0]
    monkeypatch.setattr(base_class, "connect", connect)
    connection = namespace["_MetricsHTTPConnection"](1234, deadline)
    clock[0] += 0.19
    with pytest.raises(TimeoutError):
        connection.connect()
    assert seen == [pytest.approx(0.01)]


@pytest.mark.parametrize("late_phase", ["request", "headers", "body", "parse"])
def test_all_phases_share_one_budget_and_late_parse_cannot_succeed(
    namespace,
    monkeypatch,
    late_phase,
):
    clock = install_clock(namespace, monkeypatch)
    closed = []

    def phase(name):
        clock[0] += 0.21 if name == late_phase else 0.001

    class Response:
        status = 200

        def read(self, _size):
            phase("body")
            return b"1"

        def close(self):
            closed.append("response")

    class Connection:
        def __init__(self, _port, _deadline):
            pass

        def request(self, *_args, **_kwargs):
            phase("request")

        def getresponse(self):
            phase("headers")
            return Response()

        def close(self):
            closed.append("connection")

    def parser(_payload):
        phase("parse")
        return 1

    monkeypatch.setitem(
        namespace["MetricsReader"].sample.__globals__,
        "_MetricsHTTPConnection",
        Connection,
    )
    reader = namespace["MetricsReader"](1234, "/metrics", parser)
    assert reader.sample() == (False, None)
    assert reader.connection is None
    assert reader.deadline.expires is None
    assert "connection" in closed
    if late_phase != "request":
        assert "response" in closed


def test_parse_exception_still_closes_response_and_disarms_deadline(namespace, server):
    listener, _received = server

    def parser(_payload):
        raise ValueError("parser failure")

    reader = namespace["MetricsReader"](listener.server_port, "/fast", parser)
    try:
        with pytest.raises(ValueError, match="parser failure"):
            reader.sample()
        assert reader.deadline.expires is None
    finally:
        with contextlib.suppress(OSError):
            reader.close()
