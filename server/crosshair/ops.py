"""Workspace operations. These run inside the daemon and own all state mutation.

The MCP layer (tools.py) is a thin client that reaches these over HTTP, so an
agent session can come and go without the display losing its contents.

Every function raises ValueError with a message written for an agent to read;
web.py turns those into structured RPC errors.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from . import data as data_mod
from .data import DataRefError
from .state import STORE, Panel, Placement, View

PANEL_TYPES = ("plotly", "markdown", "image")


def _base(base_dir: str | None) -> Path:
    """Where relative $ref paths resolve — supplied per-call by the agent's client."""
    return Path(base_dir or os.environ.get("CROSSHAIR_DATA_DIR") or os.getcwd())


def _require_view(name: str) -> View:
    view = STORE.views.get(name)
    if view is None:
        known = ", ".join(STORE.views) or "(none yet — call create_view first)"
        raise ValueError(f"No view named {name!r}. Existing views: {known}")
    return view


def _require_panel(panel_id: str) -> Panel:
    panel = STORE.panels.get(panel_id)
    if panel is None:
        known = ", ".join(STORE.panels) or "(none yet)"
        raise ValueError(f"No panel {panel_id!r}. Existing panels: {known}")
    return panel


def _resolve(spec: Any, base_dir: str | None) -> Any:
    try:
        return data_mod.resolve_refs(spec, _base(base_dir))
    except DataRefError as exc:
        raise ValueError(str(exc)) from exc


def _merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch: null deletes, objects merge, everything else replaces."""
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


async def get_workspace() -> dict:
    state = STORE.state_dict()
    state["browser_connected"] = len(STORE.clients) > 0
    state["url"] = STORE.url
    return state


async def describe_data(file: str, base_dir: str | None = None) -> dict:
    try:
        return data_mod.describe_file(file, _base(base_dir))
    except DataRefError as exc:
        raise ValueError(str(exc)) from exc


# --------------------------------------------------------------------------
# Views and layout
# --------------------------------------------------------------------------


async def create_view(name: str, rows: int = 2, cols: int = 2) -> dict:
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must both be >= 1")
    view = STORE.views.get(name)
    if view is None:
        view = View(name=name, rows=rows, cols=cols)
        STORE.views[name] = view
    else:
        view.rows, view.cols = rows, cols
    if STORE.active_view is None:
        STORE.active_view = name
    await STORE.broadcast_state()
    return {"ok": True, "view": name, "rows": view.rows, "cols": view.cols}


async def delete_view(name: str) -> dict:
    _require_view(name)
    for panel_id in [p.id for p in STORE.panels.values() if p.view == name]:
        del STORE.panels[panel_id]
    del STORE.views[name]
    if STORE.active_view == name:
        STORE.active_view = next(iter(STORE.views), None)
    await STORE.broadcast_state()
    return {"ok": True, "deleted": name}


async def set_layout(view: str, rows: int, cols: int, placements: list[dict] | None = None) -> dict:
    v = _require_view(view)
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must both be >= 1")
    v.rows, v.cols = rows, cols
    for entry in placements or []:
        panel_id = entry.get("panel_id")
        if panel_id is None:
            raise ValueError('each placement needs a "panel_id"')
        panel = _require_panel(panel_id)
        if panel.view != view:
            raise ValueError(f"Panel {panel_id!r} belongs to view {panel.view!r}, not {view!r}")
        v.placements = [p for p in v.placements if p.panel_id != panel_id]
        v.placements.append(
            Placement(
                panel_id=panel_id,
                row=int(entry.get("row", 1)),
                col=int(entry.get("col", 1)),
                row_span=int(entry.get("row_span", 1)),
                col_span=int(entry.get("col_span", 1)),
            )
        )
    await STORE.broadcast_state()
    return {"ok": True, "view": view, "rows": rows, "cols": cols,
            "placements": [p.__dict__ for p in v.placements]}


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------


async def upsert_panel(
    view: str,
    panel_id: str,
    spec: dict,
    title: str = "",
    type: str = "plotly",
    row: int | None = None,
    col: int | None = None,
    row_span: int = 1,
    col_span: int = 1,
    base_dir: str | None = None,
) -> dict:
    v = _require_view(view)
    if type not in PANEL_TYPES:
        raise ValueError(f"type must be one of {PANEL_TYPES}, got {type!r}")
    resolved = _resolve(spec, base_dir)

    existing = STORE.panels.get(panel_id)
    if existing is not None and existing.view != view:
        STORE.remove_placement(panel_id)
    STORE.panels[panel_id] = Panel(
        id=panel_id,
        view=view,
        title=title or (existing.title if existing else panel_id),
        type=type,
        spec=resolved,
    )

    has_placement = any(p.panel_id == panel_id for p in v.placements)
    if row is not None and col is not None:
        v.placements = [p for p in v.placements if p.panel_id != panel_id]
        v.placements.append(Placement(panel_id, int(row), int(col), int(row_span), int(col_span)))
    elif not has_placement:
        r, c = STORE.next_free_cell(v)
        v.placements.append(Placement(panel_id, r, c, int(row_span), int(col_span)))

    await STORE.broadcast_state()
    placement = next(p for p in v.placements if p.panel_id == panel_id)
    return {"ok": True, "panel_id": panel_id, "view": view, "placement": placement.__dict__}


async def patch_panel(panel_id: str, spec_patch: dict, base_dir: str | None = None) -> dict:
    panel = _require_panel(panel_id)
    panel.spec = _merge_patch(panel.spec, _resolve(spec_patch, base_dir))
    await STORE.broadcast_state()
    return {"ok": True, "panel_id": panel_id, "spec": panel.spec}


async def append_data(panel_id: str, trace: int, x: list | None = None, y: list | None = None) -> dict:
    panel = _require_panel(panel_id)
    if panel.type != "plotly":
        raise ValueError(f"append_data only works on plotly panels; {panel_id!r} is {panel.type!r}")
    traces = panel.spec.get("data", [])
    if not 0 <= trace < len(traces):
        raise ValueError(f"trace {trace} out of range; panel {panel_id!r} has {len(traces)} trace(s)")

    target = traces[trace]
    for axis, values in (("x", x), ("y", y)):
        if values is None:
            continue
        current = target.get(axis)
        if isinstance(current, dict):
            raise ValueError(
                f"Trace {trace} axis {axis!r} is a file reference ($ref/$data) and cannot be appended to. "
                "Use upsert_panel with inline arrays for streaming plots."
            )
        target[axis] = list(current or []) + list(values)

    await STORE.broadcast(
        {"type": "append", "panel_id": panel_id, "trace": trace, "x": x or [], "y": y or []}
    )
    return {"ok": True, "panel_id": panel_id, "trace": trace, "points": len(x or y or [])}


async def remove_panel(panel_id: str) -> dict:
    _require_panel(panel_id)
    del STORE.panels[panel_id]
    STORE.remove_placement(panel_id)
    await STORE.broadcast_state()
    return {"ok": True, "removed": panel_id}


async def reset_workspace() -> dict:
    """Clear every view and panel without restarting the daemon."""
    views, panels = len(STORE.views), len(STORE.panels)
    STORE.views.clear()
    STORE.panels.clear()
    STORE.active_view = None
    await STORE.broadcast_state()
    return {"ok": True, "cleared_views": views, "cleared_panels": panels}


# --------------------------------------------------------------------------
# Snapshot and feedback
# --------------------------------------------------------------------------


async def snapshot(view: str | None = None, panel_id: str | None = None) -> dict:
    if panel_id is not None:
        _require_panel(panel_id)
        target = {"panel_id": panel_id}
    else:
        name = view or STORE.active_view
        if name is None:
            raise ValueError("No views exist yet — call create_view first.")
        _require_view(name)
        target = {"view": name}
    png = await STORE.request_snapshot(target)
    return {"png_base64": base64.b64encode(png).decode()}


async def get_events(since_seq: int = 0, limit: int = 100) -> dict:
    return {"events": STORE.events_since(since_seq, limit), "latest_seq": STORE.event_seq}


async def wait_for_feedback(timeout_s: float = 300.0, since_seq: int = 0) -> dict:
    timeout_s = max(1.0, min(float(timeout_s), 3600.0))
    events = await STORE.wait_for_event(since_seq, timeout_s)
    return {"events": events, "latest_seq": STORE.event_seq, "timed_out": not events}


async def add_note(text: str, view: str | None = None) -> dict:
    await STORE.broadcast({"type": "agent_note", "text": text, "view": view or STORE.active_view})
    return {"ok": True}


# Dispatch table used by the daemon's /rpc endpoint.
OPS = {
    "get_workspace": get_workspace,
    "describe_data": describe_data,
    "create_view": create_view,
    "delete_view": delete_view,
    "set_layout": set_layout,
    "upsert_panel": upsert_panel,
    "patch_panel": patch_panel,
    "append_data": append_data,
    "remove_panel": remove_panel,
    "reset_workspace": reset_workspace,
    "snapshot": snapshot,
    "get_events": get_events,
    "wait_for_feedback": wait_for_feedback,
    "add_note": add_note,
}
