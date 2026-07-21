"""Thin HTTP client the MCP process uses to drive the detached daemon.

Discovery order: the recorded daemon, then a scan of the default port range, then
spawn a new detached daemon. An existing Crosshair daemon is always reused — the
port fallback in daemon.py only steps around *unrelated* processes.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import httpx

from .daemon import DEFAULT_PORT, LOG, PORT_SCAN, RUNTIME_DIR, SERVICE_HOST, read_record

SPAWN_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 3.0


class DaemonError(RuntimeError):
    """Raised when the daemon cannot be reached or started."""


_base_url: str | None = None


async def _health(url: str, timeout: float = CONNECT_TIMEOUT_S) -> dict | None:
    """Return the health payload if a Crosshair daemon is listening at `url`."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(f"{url}/control/health")
            if resp.status_code != 200:
                return None
            body = resp.json()
            return body if body.get("service") == "crosshair" else None
    except Exception:
        return None


async def discover() -> str | None:
    """Find a live daemon without starting one."""
    record = read_record()
    if record and record.get("url") and await _health(record["url"]):
        return record["url"]
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SCAN):
        url = f"http://{SERVICE_HOST}:{port}"
        if await _health(url, timeout=0.4):
            return url
    return None


def _spawn(port: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "ab")
    subprocess.Popen(
        [sys.executable, "-m", "crosshair", "serve", "--port", str(port)],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: survives this MCP process exiting
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


async def ensure(port: int = DEFAULT_PORT, spawn: bool = True) -> str:
    """Return the base URL of a running daemon, starting one if needed."""
    global _base_url
    if _base_url and await _health(_base_url):
        return _base_url

    found = await discover()
    if found:
        _base_url = found
        return found
    if not spawn:
        raise DaemonError("No Crosshair daemon is running. Call ensure_server to start one.")

    _spawn(port)
    deadline = asyncio.get_running_loop().time() + SPAWN_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        found = await discover()
        if found:
            _base_url = found
            return found
    raise DaemonError(
        f"Started a daemon but it did not come up within {SPAWN_TIMEOUT_S:.0f}s. See {LOG}."
    )


def forget() -> None:
    """Drop the cached URL (after a shutdown, so the next call rediscovers)."""
    global _base_url
    _base_url = None


async def call(op: str, timeout: float = 30.0, **args) -> dict:
    """Invoke a workspace operation on the daemon."""
    url = await ensure()
    payload = {"op": op, "args": {k: v for k, v in args.items() if v is not None}}
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(f"{url}/rpc", json=payload)
    except httpx.HTTPError as exc:
        forget()
        raise DaemonError(f"Lost contact with the Crosshair daemon at {url}: {exc}") from exc

    body = resp.json()
    if not body.get("ok"):
        raise ValueError(body.get("error", "unknown error"))
    return body["result"]
