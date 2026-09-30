from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from test_hud_release_migrations import assert_integrity, columns, pinned_database, rows, versions

from app.broadcast.models import BroadcastError, OutputCreate, SessionCreate
from app.broadcast.oauth import YouTubeOAuth
from app.broadcast.store import BroadcastStore
from app.broadcast.youtube import SAFE_REASONS, YouTubeError, YouTubeHTTP, YouTubeProvisioner
from app.core.config import Settings
from app.db import Database, utc_now


def seed_nodes(database: Database) -> None:
    with database.connect() as db:
        for node_id in ("relay-a", "relay-b", "relay-c"):
            db.execute(
                "INSERT INTO restream_nodes(id,display_name,address,resolved_ip,ssh_port,"
                "ssh_username,status,created_at,updated_at) VALUES (?,?,?,'192.0.2.1',22,"
                "'synthetic','ready',?,?)",
                (node_id, node_id, f"{node_id}.example", utc_now(), utc_now()),
            )


@pytest.fixture
def store(settings: Settings) -> BroadcastStore:
    db = Database(settings.database_path)
    db.migrate()
    seed_nodes(db)
    return BroadcastStore(db, settings.master_encryption_key)


def session(store: BroadcastStore) -> str:
    return store.create_session(
        SessionCreate(name="Portrait walk", ingress_node_id="relay-a"), "synthetic-session-001"
    )


def manual_output(node: str = "relay-a", key: str = "synthetic-key-one") -> OutputCreate:
    return OutputCreate(
        name="Independent event",
        node_id=node,
        primary_url="rtmps://a.rtmps.youtube.com/live2",
        backup_url="rtmps://b.rtmps.youtube.com/live2?backup=1",
        stream_key=SecretStr(key),
    )


def api_output(store: BroadcastStore) -> str:
    now = utc_now()
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO youtube_accounts VALUES "
            "('channel','Synthetic',?,'connected',?,?)",
            (store.seal({"refresh_token": "synthetic-refresh"}), now, now),
        )
    return store.create_output(
        session(store),
        OutputCreate(name="API event", node_id="relay-a", mode="youtube_api", channel_id="channel"),
        "synthetic-api-out-001",
    )


def test_independent_outputs_restart_encryption_idempotency_and_isolated_intent(
    store: BroadcastStore,
) -> None:
    sid = session(store)
    one = store.create_output(sid, manual_output(), "synthetic-output-001")
    two = store.create_output(
        sid, manual_output("relay-b", "synthetic-key-two"), "synthetic-output-002"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            list(
                pool.map(
                    lambda _: store.create_output(sid, manual_output(), "synthetic-output-001"),
                    range(4),
                )
            )
            == [one] * 4
        )
    with pytest.raises(BroadcastError, match="idempotency_conflict"):
        store.create_output(sid, manual_output(key="another-key"), "synthetic-output-001")
    with pytest.raises(BroadcastError, match="unique_stream"):
        store.create_output(sid, manual_output("relay-c"), "synthetic-output-003")
    store.intent(one, True, "synthetic-start-001")
    store.intent(two, True, "synthetic-start-002")
    store.intent(two, False, "synthetic-stop-002")
    store.database.migrate()
    snapshot = BroadcastStore(store.database, store.master_key).snapshot()
    outputs = {o["id"]: o for o in snapshot["sessions"][0]["outputs"]}
    assert outputs[one]["desired_enabled"] == 1
    assert outputs[two]["desired_enabled"] == 0
    assert outputs[one]["state"] == "START_REQUESTED"  # Intent is not media evidence.
    assert outputs[one]["viewer_playback"] == "UNKNOWN"
    assert not snapshot["auto_failover_enabled"]
    assert "synthetic-key" not in json.dumps(snapshot)
    with store.database.connect() as db:
        dump = "\n".join(db.iterdump())
        assert "synthetic-key" not in dump
        assert len(db.execute("SELECT * FROM broadcast_outputs").fetchall()) == 2
    assert_integrity(store.database)


@pytest.mark.parametrize(
    "url",
    [
        "rtmp://a.rtmp.youtube.com/live2",
        "rtmps://127.0.0.1/live2",
        "rtmps://a.rtmps.youtube.com.evil.example/live2",
        "rtmps://a.rtmps.youtube.com:444/live2",
        "rtmps://user@a.rtmps.youtube.com/live2",
        "rtmps://a.rtmps.youtube.com/live2/secret",
        "rtmps://a.rtmps.youtube.com/live2?redirect=elsewhere",
        "rtmps://a.rtmps.youtube.com/live2#key",
        "rtmps://a.rtmps.youtube.com/live2\n",
        "rtmps://%61.rtmps.youtube.com/live2",
    ],
)
def test_destination_ssrf_allowlist(url: str) -> None:
    with pytest.raises(ValidationError):
        OutputCreate(name="Invalid", node_id="a", primary_url=url)


class FakeYouTube:
    def __init__(self) -> None:
        self.streams: list[dict[str, Any]] = []
        self.broadcasts: list[dict[str, Any]] = []
        self.bindings: list[tuple[str, str]] = []
        self.transitions: list[str] = []
        self.timeout_at: str | None = None

    def insert_stream(self, output: dict[str, Any], marker: str) -> dict[str, Any]:
        resource = {
            "id": f"stream-{len(self.streams)}",
            "snippet": {"description": marker},
            "cdn": {
                "ingestionInfo": {
                    "rtmpsIngestionAddress": "rtmps://a.rtmps.youtube.com/live2",
                    "rtmpsBackupIngestionAddress": "rtmps://b.rtmps.youtube.com/live2?backup=1",
                    "streamName": f"synthetic-remote-key-{len(self.streams)}",
                }
            },
        }
        self.streams.append(resource)
        if self.timeout_at == "stream":
            raise YouTubeError("youtube_response_uncertain")
        return resource

    def insert_broadcast(self, output: dict[str, Any], marker: str) -> dict[str, Any]:
        resource = {"id": f"broadcast-{len(self.broadcasts)}", "snippet": {"description": marker}}
        self.broadcasts.append(resource)
        if self.timeout_at == "broadcast":
            raise YouTubeError("youtube_response_uncertain")
        return resource

    def find(self, resource: str, marker: str) -> dict[str, Any] | None:
        values = self.streams if resource == "liveStreams" else self.broadcasts
        return next((x for x in values if x["snippet"]["description"] == marker), None)

    def bind(self, broadcast_id: str, stream_id: str) -> None:
        if (broadcast_id, stream_id) not in self.bindings:
            self.bindings.append((broadcast_id, stream_id))
        if self.timeout_at == "bind":
            raise YouTubeError("youtube_response_uncertain")

    def status(self, broadcast_id: str, stream_id: str) -> dict[str, str]:
        return {"lifecycle_status": "ready", "stream_status": "active", "health_status": "good"}

    def transition(self, broadcast_id: str, state: str) -> None:
        self.transitions.append(state)


@pytest.mark.parametrize("timeout_at", [None, "stream", "broadcast", "bind"])
def test_provision_timeout_reconciliation_restart_never_duplicates(
    store: BroadcastStore,
    timeout_at: str | None,
) -> None:
    output = api_output(store)
    fake = FakeYouTube()
    fake.timeout_at = timeout_at
    if timeout_at:
        with pytest.raises(YouTubeError, match="uncertain"):
            YouTubeProvisioner(store).provision(output, fake)
    fake.timeout_at = None
    for _ in range(2):
        reopened = BroadcastStore(store.database, store.master_key)
        YouTubeProvisioner(reopened).provision(output, fake)
    assert len(fake.streams) == len(fake.broadcasts) == len(fake.bindings) == 1
    out = store.snapshot()["sessions"][0]["outputs"][0]
    assert out["state"] == "READY"
    assert out["youtube"]["broadcast_id"] == "broadcast-0"
    assert "synthetic-remote-key" not in json.dumps(out)
    assert fake.transitions == []


def test_unknown_insert_result_is_not_retried_blindly(store: BroadcastStore) -> None:
    output = api_output(store)
    with store.transaction() as db:
        db.execute(
            "UPDATE youtube_operations SET phase='STREAM_PENDING' WHERE output_id=?", (output,)
        )
    fake = FakeYouTube()
    for _ in range(2):
        with pytest.raises(YouTubeError, match="reconciliation_required"):
            YouTubeProvisioner(store).provision(output, fake)
    assert fake.streams == fake.broadcasts == []


@pytest.mark.parametrize("reason", sorted(SAFE_REASONS) + ["secret-containing-error"])
def test_youtube_error_mapping_never_exposes_remote_body(reason: str) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            403, json={"error": {"message": "synthetic-secret", "errors": [{"reason": reason}]}}
        )
    )
    provider = YouTubeHTTP(lambda: "synthetic-token", transport=transport)
    with pytest.raises(YouTubeError) as error:
        provider.insert_stream({"name": "Output"}, "marker")
    assert error.value.code == (reason if reason in SAFE_REASONS else "youtube_api_error")
    assert "synthetic-secret" not in str(error.value)


def test_youtube_http_contract_no_autostop_redirects_or_shared_stream_defaults() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "synthetic"})

    remote = YouTubeHTTP(lambda: "synthetic-token", transport=httpx.MockTransport(handle))
    remote.insert_broadcast(
        {"name": "event", "visibility": "unlisted", "scheduled_start": None}, "m"
    )
    body = json.loads(requests[0].content)
    assert body["contentDetails"]["enableAutoStop"] is False
    assert body["contentDetails"]["enableAutoStart"] is False
    assert requests[0].url.host == "www.googleapis.com"
    remote.bind("event", "stream")
    remote.transition("event", "live")
    assert dict(requests[1].url.params)["streamId"] == "stream"


def test_oauth_bound_state_encrypted_refresh_refresh_and_revoke(store: BroadcastStore) -> None:
    mode = {"revoked": False}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            if mode["revoked"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
            )
        if request.url.path == "/revoke":
            return httpx.Response(200, json={})
        return httpx.Response(
            200, json={"items": [{"id": "channel", "snippet": {"title": "Test"}}]}
        )

    oauth = YouTubeOAuth(
        store,
        "synthetic-client",
        "synthetic-secret",
        "https://testserver/api/broadcasts/youtube/callback",
        transport=httpx.MockTransport(handle),
    )
    url = oauth.begin("admin-session")
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"]
    state = params["state"][0]
    with pytest.raises(BroadcastError, match="state_invalid"):
        oauth.callback("different-session", state, "synthetic-code")
    assert oauth.callback("admin-session", state, "synthetic-code") == "channel"
    with pytest.raises(BroadcastError, match="state_invalid"):
        oauth.callback("admin-session", state, "synthetic-code")
    assert oauth.access_token("channel") == "synthetic-access"
    with store.database.connect() as db:
        assert "synthetic-refresh" not in "\n".join(db.iterdump())
    mode["revoked"] = True
    with pytest.raises(BroadcastError, match="token_revoked"):
        oauth.access_token("channel")
    assert store.snapshot()["accounts"][0]["status"] == "revoked"


def test_new_broadcast_rows_survive_later_native_v6_and_restart(store: BroadcastStore) -> None:
    output = store.create_output(session(store), manual_output(), "synthetic-output-migrate")
    original_columns = columns(store.database)
    original_rows = rows(store.database, original_columns)
    for _ in range(2):
        pinned_database("combined", store.database.path).migrate()
        reopened = Database(store.database.path)
        reopened.migrate()
        assert versions(reopened) == [1, 2, 3, 4, 5, 6, 7, 8]
        assert rows(reopened, original_columns, exclude_v6=True) == original_rows
        assert_integrity(reopened)
    assert store.snapshot()["sessions"][0]["outputs"][0]["id"] == output
