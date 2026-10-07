"""Disposable loopback panel + existing real media lab; no production settings."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.broadcast.api import mutation
from app.broadcast.media_runtime import stop
from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from scripts.broadcast_handoff_lab import HandoffLab
from scripts.hud_beta import NoWorkers, tls_files

PASSWORD = "local-handoff-synthetic-only"  # noqa: S105 - public local fixture, never production


class LabBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD"}:
            path = scope["path"]
            allowed = path in {"/api/auth/login", "/api/auth/logout", "/lab/source"} or (
                path.startswith(
                    (
                        "/api/broadcasts/",
                        "/stream-operator/api/",
                        "/moblin-hud/",
                        "/api/moblin-hud/",
                    )
                )
                and path.rsplit("/", 1)[-1]
                in {
                    "switch",
                    "cancel",
                    "operators",
                    "revoke",
                    "pair",
                    "logout",
                    "pairings",
                    "moblin-profiles",
                }
            )
            if not allowed:
                await JSONResponse({"error": "synthetic_lab_boundary"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def settings(lab: HandoffLab, port: int) -> Settings:
    return Settings(
        environment="test",
        public_domain="127.0.0.1",
        public_control_url=f"https://127.0.0.1:{port}",
        public_rtmp_host="127.0.0.1",
        public_rtmp_port=1,
        session_secret=secrets.token_urlsafe(48),
        master_encryption_key=lab.master_key,
        admin_login="lab",
        admin_password_hash=hash_password(PASSWORD),
        database_path=lab.directory / "control.sqlite",
        mediamtx_api_url="http://127.0.0.1:1",
        mediamtx_hls_url="http://127.0.0.1:1",
        mediamtx_internal_rtmp_url="rtmp://127.0.0.1:1",
        max_destinations=1,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=30,
        reconnect_stable_seconds=60,
        reconnect_max_fast_failures=3,
        log_level="WARNING",
        trusted_proxies=(),
        cookie_secure=True,
        session_ttl_seconds=3600,
        ffmpeg_binary="disabled-legacy-lab-worker",
        ffprobe_binary="disabled-legacy-lab-worker",
        worker_auth_user="disabled",
        worker_auth_password=secrets.token_urlsafe(48),
        bootstrap_socket_path=lab.directory / "disabled-bootstrap.sock",
        bootstrap_worker_secret="",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if (args.directory / "control.sqlite").exists() or not 1 <= args.seconds <= 3600:
        raise ValueError("Fresh disposable directory and bounded duration required")
    lab = HandoffLab(args.mediamtx, args.ffmpeg, args.ffprobe, args.directory)
    ending = threading.Event()
    lock = threading.Lock()
    server = None
    thread = None
    try:
        lab.setup()
        lab.wait(lambda: lab.ready("relay-a", lab.routes[0]))
        for output in lab.outputs[1:]:
            lab.store.intent(output, False, secrets.token_hex(16))
        for node in ("relay-b", "relay-c"):
            lab.store.add_route(lab.outputs[0], node, secrets.token_hex(16))
        for runtime in lab.runtimes.values():
            runtime.test_destinations[lab.outputs[0] + ":PRIMARY"] = lab.destination("0")
            runtime.test_destinations[lab.outputs[0] + ":BACKUP"] = lab.destination("3")
        app = create_app(settings(lab, args.port), worker_launcher=NoWorkers())
        app.add_middleware(LabBoundary)

        @app.get("/lab", response_class=HTMLResponse)
        def laboratory() -> str:
            return """<!doctype html><meta charset="utf-8"><title>Local handoff lab</title>
<h1>Синтетическая медиалаборатория</h1><p>
<a href="/login">Вход: lab / local-handoff-synthetic-only</a> ·
<a href="/broadcasts">Панель Orchestrator</a></p>
<p>В панели выберите target и режим. После AWAITING_DIRECT_SOURCE вернитесь сюда
и переподключите единственный синтетический источник.
YouTube здесь заменён локальным приёмником.</p>
<select id="node"><option>relay-a</option><option>relay-b</option>
<option>relay-c</option></select><button id="move">Переподключить источник</button>
<p id="status"></p><script src="/lab/source.js" defer></script>"""

        @app.get("/lab/source.js")
        def source_script() -> Response:
            # The application's CSP deliberately disallows inline scripts.
            return Response(
                """
document.querySelector('#move').onclick=async()=>{
const s=await fetch('/api/auth/session');if(!s.ok){location.href='/login';return;}
const auth=await s.json();const r=await fetch('/lab/source',{method:'POST',headers:{
'Content-Type':'application/json','X-CSRF-Token':auth.csrf_token},
body:JSON.stringify({node:document.querySelector('#node').value})});
document.querySelector('#status').textContent=r.ok?
'Источник переподключён. Проверяйте фактическое завершение в панели.':'Запрос отклонён';
};""",
                media_type="application/javascript",
            )

        @app.post("/lab/source", dependencies=[Depends(mutation)])
        async def move_source(request: Request) -> dict[str, bool]:
            node = (await request.json()).get("node")
            if node not in lab.runtimes:
                return {"accepted": False}

            def move() -> None:
                with lock:
                    assert lab.source_process
                    stop(lab.source_process)
                    time.sleep(1)
                    lab.source_process = lab.phone(node)
                    lab.processes.append(lab.source_process)

            await asyncio.to_thread(move)
            return {"accepted": True}

        key, cert = tls_files(args.directory)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="error",
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        print(
            f"LOCAL SYNTHETIC PANEL https://127.0.0.1:{args.port}/lab | lab / {PASSWORD}",
            flush=True,
        )
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and thread.is_alive() and not ending.wait(0.1):
            with lock:
                lab.step()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        lab.close()


if __name__ == "__main__":
    main()
