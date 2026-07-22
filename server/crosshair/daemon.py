"""The detached display server: owns workspace state and outlives any agent session."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import uvicorn

DEFAULT_PORT = 8137
PORT_SCAN = 10  # 8137..8146
SERVICE_HOST = "localhost"  # what clients and the browser dial
RUNTIME_DIR = Path(os.environ.get("CROSSHAIR_HOME") or (Path.home() / ".crosshair"))
RECORD = RUNTIME_DIR / "daemon.json"
LOG = RUNTIME_DIR / "daemon.log"


def port_is_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int) -> int:
    """First free port at or after `preferred`.

    A Crosshair daemon already holding `preferred` is handled earlier by the
    client (it attaches instead of spawning); reaching here means the port is
    held by something unrelated, so we step aside rather than fail.
    """
    for candidate in range(preferred, preferred + PORT_SCAN):
        if port_is_free(host, candidate):
            return candidate
    raise RuntimeError(
        f"No free port in {preferred}..{preferred + PORT_SCAN - 1}. "
        "Pass --port to choose a different range."
    )


def write_record(host: str, port: int, url: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port, "url": url}))


def clear_record() -> None:
    try:
        RECORD.unlink()
    except FileNotFoundError:
        pass


def read_record() -> dict | None:
    try:
        return json.loads(RECORD.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    from . import persist
    from .state import STORE
    from .web import create_app

    bound = pick_port(host, port)
    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{display_host}:{bound}"
    STORE.url = url

    # Bring back whatever the last daemon had on screen. The browser gets full
    # state on connect, so a restored workspace needs nothing further.
    saved = persist.load_state()
    if saved:
        STORE.load_from_dict(saved)
        print(
            f"Restored {len(STORE.views)} view(s) and {len(STORE.panels)} panel(s) "
            f"from {persist.STATE_FILE}",
            file=sys.stderr,
            flush=True,
        )

    config = uvicorn.Config(create_app(), host=host, port=bound, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    write_record(host, bound, url)
    print(f"Crosshair daemon on {url} (pid {os.getpid()})", file=sys.stderr, flush=True)
    try:
        await server.serve()
    finally:
        clear_record()
