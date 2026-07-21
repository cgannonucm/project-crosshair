"""In-memory workspace state, WebSocket fan-out, and the human-feedback event queue.

Everything (MCP stdio loop + uvicorn) runs on one asyncio event loop, so plain
asyncio primitives are enough — no cross-thread locking.
"""
from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_EVENTS = 1000
SNAPSHOT_TIMEOUT_S = 30.0


@dataclass
class Placement:
    panel_id: str
    row: int  # 1-indexed
    col: int  # 1-indexed
    row_span: int = 1
    col_span: int = 1


@dataclass
class View:
    name: str
    rows: int
    cols: int
    placements: list[Placement] = field(default_factory=list)


@dataclass
class Panel:
    id: str
    view: str
    title: str
    type: str  # "plotly" | "markdown" | "image"
    spec: dict


class Store:
    def __init__(self) -> None:
        self.views: dict[str, View] = {}
        self.panels: dict[str, Panel] = {}
        self.active_view: str | None = None
        self.url: str = ""  # set by the daemon once it knows its bound port
        self.clients: set[Any] = set()  # fastapi WebSocket connections
        self.events: list[dict] = []
        self.event_seq = 0
        self._event_cond = asyncio.Condition()
        self._snapshots: dict[str, asyncio.Future] = {}

    # ---------- state serialization ----------

    def state_dict(self) -> dict:
        return {
            "active_view": self.active_view,
            "views": [
                {
                    "name": v.name,
                    "rows": v.rows,
                    "cols": v.cols,
                    "placements": [asdict(p) for p in v.placements],
                }
                for v in self.views.values()
            ],
            "panels": {
                p.id: {"id": p.id, "view": p.view, "title": p.title, "type": p.type, "spec": p.spec}
                for p in self.panels.values()
            },
        }

    # ---------- websocket fan-out ----------

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def broadcast_state(self) -> None:
        await self.broadcast({"type": "state", "state": self.state_dict()})

    # ---------- layout helpers ----------

    def occupied_cells(self, view: View) -> set[tuple[int, int]]:
        cells: set[tuple[int, int]] = set()
        for p in view.placements:
            for r in range(p.row, p.row + p.row_span):
                for c in range(p.col, p.col + p.col_span):
                    cells.add((r, c))
        return cells

    def next_free_cell(self, view: View) -> tuple[int, int]:
        """First free cell scanning row-major; grows the grid by a row if full."""
        occupied = self.occupied_cells(view)
        for r in range(1, view.rows + 1):
            for c in range(1, view.cols + 1):
                if (r, c) not in occupied:
                    return r, c
        view.rows += 1
        return view.rows, 1

    def remove_placement(self, panel_id: str) -> None:
        for v in self.views.values():
            v.placements = [p for p in v.placements if p.panel_id != panel_id]

    # ---------- human feedback events ----------

    async def add_event(self, kind: str, data: dict, panel_id: str | None = None, view: str | None = None) -> dict:
        self.event_seq += 1
        event = {
            "seq": self.event_seq,
            "ts": time.time(),
            "kind": kind,
            "panel_id": panel_id,
            "view": view,
            "data": data,
        }
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]
        async with self._event_cond:
            self._event_cond.notify_all()
        return event

    def events_since(self, since_seq: int, limit: int = 100) -> list[dict]:
        return [e for e in self.events if e["seq"] > since_seq][:limit]

    async def wait_for_event(self, after_seq: int, timeout_s: float) -> list[dict]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            pending = self.events_since(after_seq)
            if pending:
                return pending
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return []
            async with self._event_cond:
                try:
                    await asyncio.wait_for(self._event_cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return []

    # ---------- snapshots ----------

    async def request_snapshot(self, target: dict) -> bytes:
        """Ask a connected browser to render a PNG. Returns raw PNG bytes."""
        if not self.clients:
            raise RuntimeError(
                "No browser is connected. Open the Crosshair UI (see server URL) and retry."
            )
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._snapshots[request_id] = fut
        try:
            await self.broadcast({"type": "snapshot_request", "request_id": request_id, "target": target})
            png_b64 = await asyncio.wait_for(fut, timeout=SNAPSHOT_TIMEOUT_S)
        finally:
            self._snapshots.pop(request_id, None)
        return base64.b64decode(png_b64)

    def resolve_snapshot(self, request_id: str, png_b64: str | None, error: str | None) -> None:
        fut = self._snapshots.get(request_id)
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(RuntimeError(f"Browser failed to render snapshot: {error}"))
        else:
            fut.set_result(png_b64)


STORE = Store()
