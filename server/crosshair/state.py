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
    # Bumped on every spec change. The browser re-plots on this rather than on
    # object identity, so unrelated state broadcasts (a new comment, another
    # panel's edit) don't tear down a figure and lose its zoom or current frame.
    rev: int = 1
    # The code the agent says produced this plot, if it supplied any. The spec
    # records what is on screen; this records how it was arrived at.
    code: str | None = None


@dataclass
class Anchor:
    """A rectangle a comment is pinned to, in one of two coordinate spaces.

    `space="data"` (the default) reads the corners as data coordinates, so the
    comment tracks the data under it as the human zooms and pans. Coordinates are
    whatever the axis speaks — numbers, but also date strings or category names —
    and are passed through to Plotly untouched.

    `space="panel"` reads them as fractions of the panel box, x rightward and y
    downward from the top-left corner. That is for comments on the chrome rather
    than the data — a tick label, an axis title, a legend — which should stay put
    when the view is zoomed, and which have no data coordinates to speak of.
    """

    x0: Any
    x1: Any
    y0: Any
    y1: Any
    space: str = "data"


@dataclass
class Comment:
    id: str
    panel_id: str
    view: str
    author: str  # "agent" | "human"
    text: str
    ts: float
    anchor: Anchor | None = None  # None = a comment on the panel as a whole
    resolved: bool = False
    edited_ts: float | None = None  # set when the text is revised after posting


class Store:
    def __init__(self) -> None:
        self.views: dict[str, View] = {}
        self.panels: dict[str, Panel] = {}
        self.comments: dict[str, Comment] = {}
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
                p.id: {"id": p.id, "view": p.view, "title": p.title, "type": p.type,
                       "spec": p.spec, "rev": p.rev, "code": p.code}
                for p in self.panels.values()
            },
            "comments": [self.comment_dict(c) for c in self.sorted_comments()],
        }

    def load_from_dict(self, state: dict) -> None:
        """Rebuild the workspace from a persisted `state_dict()` payload.

        Tolerant by design: a workspace saved by an older build is worth
        restoring partially, and a malformed entry should cost one panel rather
        than the whole display.
        """
        self.views.clear()
        self.panels.clear()
        self.comments.clear()

        for v in state.get("views") or []:
            try:
                self.views[v["name"]] = View(
                    name=v["name"],
                    rows=int(v.get("rows", 2)),
                    cols=int(v.get("cols", 2)),
                    placements=[
                        Placement(
                            panel_id=p["panel_id"],
                            row=int(p.get("row", 1)),
                            col=int(p.get("col", 1)),
                            row_span=int(p.get("row_span", 1)),
                            col_span=int(p.get("col_span", 1)),
                        )
                        for p in v.get("placements") or []
                    ],
                )
            except (KeyError, TypeError, ValueError):
                continue

        for p in (state.get("panels") or {}).values():
            try:
                self.panels[p["id"]] = Panel(
                    id=p["id"],
                    view=p["view"],
                    title=p.get("title") or p["id"],
                    type=p.get("type") or "plotly",
                    spec=p.get("spec") or {},
                    rev=int(p.get("rev", 1)),
                    code=p.get("code"),
                )
            except (KeyError, TypeError, ValueError):
                continue

        for c in state.get("comments") or []:
            try:
                a = c.get("anchor")
                self.comments[c["id"]] = Comment(
                    id=c["id"],
                    panel_id=c["panel_id"],
                    view=c["view"],
                    author=c.get("author") or "agent",
                    text=c.get("text") or "",
                    ts=float(c.get("ts") or time.time()),
                    anchor=Anchor(
                        x0=a["x0"], x1=a["x1"], y0=a["y0"], y1=a["y1"],
                        space=a.get("space") or "data",
                    ) if a else None,
                    resolved=bool(c.get("resolved")),
                    edited_ts=c.get("edited_ts"),
                )
            except (KeyError, TypeError, ValueError):
                continue

        active = state.get("active_view")
        self.active_view = active if active in self.views else next(iter(self.views), None)

    def comment_dict(self, c: Comment) -> dict:
        return {
            "id": c.id,
            "panel_id": c.panel_id,
            "view": c.view,
            "author": c.author,
            "text": c.text,
            "ts": c.ts,
            "anchor": asdict(c.anchor) if c.anchor else None,
            "resolved": c.resolved,
            "edited_ts": c.edited_ts,
        }

    def sorted_comments(self) -> list[Comment]:
        """Oldest first — the browser numbers pins in this order."""
        return sorted(self.comments.values(), key=lambda c: c.ts)

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

    async def broadcast_comments(self) -> None:
        """Push just the comment list.

        A workspace carrying inline figure data runs to many megabytes, and
        re-sending all of it to add one pin costs seconds of latency. Comments
        are tiny and change often, so they get their own channel.
        """
        await self.broadcast(
            {"type": "comments", "comments": [self.comment_dict(c) for c in self.sorted_comments()]}
        )

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

    # ---------- comments ----------

    def drop_comments(self, *, panel_id: str | None = None, view: str | None = None) -> int:
        """Discard comments whose anchor panel or view is going away."""
        doomed = [
            c.id
            for c in self.comments.values()
            if (panel_id is not None and c.panel_id == panel_id)
            or (view is not None and c.view == view)
        ]
        for cid in doomed:
            del self.comments[cid]
        return len(doomed)

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
