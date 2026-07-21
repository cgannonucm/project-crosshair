"""The daemon's HTTP surface: the browser app, the /ws state channel, /data arrays,
and the /rpc control plane the MCP client drives."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import data as data_mod
from .ops import OPS
from .state import STORE

STATIC_DIR = Path(__file__).parent / "static"
SERVICE = "crosshair"


def create_app() -> FastAPI:
    app = FastAPI(title="Crosshair")

    # ---- control plane (used by the MCP client) ----

    @app.get("/control/health")
    async def health():
        """Identity probe — lets a client tell our daemon from an unrelated process."""
        return {
            "service": SERVICE,
            "pid": os.getpid(),
            "url": STORE.url,
            "browser_connected": len(STORE.clients) > 0,
            "views": len(STORE.views),
            "panels": len(STORE.panels),
        }

    @app.post("/control/shutdown")
    async def shutdown():
        async def _stop():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop())
        return {"ok": True, "stopping": True}

    @app.post("/rpc")
    async def rpc(payload: dict):
        """Single entry point for every workspace operation."""
        op = str(payload.get("op") or "")
        args = payload.get("args") or {}
        fn = OPS.get(op)
        if fn is None:
            return JSONResponse(
                {"ok": False, "error": f"unknown op {op!r}", "kind": "unknown_op"},
                status_code=400,
            )
        try:
            return {"ok": True, "result": await fn(**args)}
        except ValueError as exc:
            # Agent-facing validation errors — expected, not a server fault.
            return JSONResponse({"ok": False, "error": str(exc), "kind": "invalid"}, status_code=400)
        except TypeError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "kind": "invalid"}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent verbatim
            return JSONResponse({"ok": False, "error": str(exc), "kind": "error"}, status_code=500)

    # ---- data plane ----

    @app.get("/api/state")
    async def get_state():
        return STORE.state_dict()

    @app.get("/data/{ref_id}")
    async def get_data(ref_id: str):
        arr = data_mod.get_array(ref_id)
        if arr is None:
            return JSONResponse({"error": "unknown ref"}, status_code=404)
        return JSONResponse({"values": arr})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        STORE.clients.add(websocket)
        try:
            await websocket.send_json({"type": "state", "state": STORE.state_dict()})
            while True:
                await _handle_client_message(await websocket.receive_json())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            STORE.clients.discard(websocket)

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


async def _handle_client_message(msg: dict) -> None:
    kind = msg.get("type")
    if kind == "snapshot_result":
        STORE.resolve_snapshot(msg.get("request_id", ""), msg.get("png"), msg.get("error"))
    elif kind == "event":
        await STORE.add_event(
            kind=msg.get("kind", "unknown"),
            data=msg.get("data", {}),
            panel_id=msg.get("panel_id"),
            view=msg.get("view"),
        )
    elif kind == "set_active_view":
        name = msg.get("view")
        if name in STORE.views:
            STORE.active_view = name
