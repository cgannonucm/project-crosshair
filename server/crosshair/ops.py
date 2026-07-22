"""Workspace operations. These run inside the daemon and own all state mutation.

The MCP layer (tools.py) is a thin client that reaches these over HTTP, so an
agent session can come and go without the display losing its contents.

Every function raises ValueError with a message written for an agent to read;
web.py turns those into structured RPC errors.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import data as data_mod
from . import persist
from .data import DataRefError
from .state import STORE, Anchor, Comment, Panel, Placement, View

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


async def get_history(
    view: str | None = None,
    panel_id: str | None = None,
    limit: int = 50,
    include_args: bool = False,
) -> dict:
    limit = max(1, min(int(limit), 500))
    entries = persist.read_history(
        view=view, panel_id=panel_id, limit=limit, include_args=bool(include_args)
    )
    return {"history": entries, "count": len(entries), **persist.workspace_info()}


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
    STORE.drop_comments(view=name)
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
    code: str | None = None,
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
        rev=(existing.rev + 1) if existing else 1,
        # An upsert without code keeps whatever the panel already had, so a
        # later positional tweak doesn't erase the provenance of the figure.
        code=code if code is not None else (existing.code if existing else None),
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


async def patch_panel(panel_id: str, spec_patch: dict, base_dir: str | None = None,
                      code: str | None = None) -> dict:
    panel = _require_panel(panel_id)
    panel.spec = _merge_patch(panel.spec, _resolve(spec_patch, base_dir))
    panel.rev += 1
    if code is not None:
        panel.code = code
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
    dropped = STORE.drop_comments(panel_id=panel_id)
    await STORE.broadcast_state()
    return {"ok": True, "removed": panel_id, "dropped_comments": dropped}


async def restore_panel(panel_id: str, seq: int) -> dict:
    """Roll a panel back to the version recorded at a history entry.

    `seq` names an entry in the mutation log that carries a panel snapshot (an
    upsert, a patch, or an earlier restore). The panel is recreated exactly as
    it was then; the restore is itself journalled, so it can be undone in turn.
    """
    entry = persist.get_history_entry(int(seq))
    if entry is None:
        raise ValueError(f"No history entry with seq {seq}.")
    if entry.get("panel_id") != panel_id:
        raise ValueError(
            f"History entry {seq} is for panel {entry.get('panel_id')!r}, not {panel_id!r}."
        )
    snap = entry.get("snapshot")
    if not snap:
        raise ValueError(
            f"History entry {seq} ({entry.get('op')}) has no restorable snapshot — "
            "only panel edits can be restored, not layout or comment changes."
        )

    view_name = snap.get("view")
    v = STORE.views.get(view_name)
    if v is None:
        # The view was deleted since; bring it back so the panel has a home.
        v = View(name=view_name, rows=1, cols=1)
        STORE.views[view_name] = v
        if STORE.active_view is None:
            STORE.active_view = view_name

    existing = STORE.panels.get(panel_id)
    if existing is not None and existing.view != view_name:
        STORE.remove_placement(panel_id)
    STORE.panels[panel_id] = Panel(
        id=panel_id,
        view=view_name,
        title=snap.get("title") or panel_id,
        type=snap.get("type") or "plotly",
        spec=snap.get("spec") or {},
        rev=(existing.rev + 1) if existing else 1,
        code=snap.get("code"),
    )
    if not any(p.panel_id == panel_id for p in v.placements):
        r, c = STORE.next_free_cell(v)
        v.placements.append(Placement(panel_id, r, c))

    await STORE.broadcast_state()
    return {"ok": True, "panel_id": panel_id, "restored_from_seq": int(seq), "view": view_name}


async def reset_workspace() -> dict:
    """Clear every view, panel, and comment without restarting the daemon."""
    views, panels, comments = len(STORE.views), len(STORE.panels), len(STORE.comments)
    STORE.views.clear()
    STORE.panels.clear()
    STORE.comments.clear()
    STORE.active_view = None
    # The plots go, but the history log does not — it is the record of what was
    # here, and clearing the screen is not a reason to forget it.
    persist.clear()
    await STORE.broadcast_state()
    return {"ok": True, "cleared_views": views, "cleared_panels": panels,
            "cleared_comments": comments}


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


# --------------------------------------------------------------------------
# Comments — margin notes anchored to a region of a panel
# --------------------------------------------------------------------------


def _require_comment(comment_id: str) -> Comment:
    comment = STORE.comments.get(comment_id)
    if comment is None:
        known = ", ".join(STORE.comments) or "(none yet)"
        raise ValueError(f"No comment {comment_id!r}. Existing comments: {known}")
    return comment


def _ordered(a: Any, b: Any) -> tuple[Any, Any]:
    """Low, high — but only numbers can be ordered; dates and categories pass through."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a, b) if a <= b else (b, a)
    return a, b


def _anchor(x0: Any, x1: Any, y0: Any, y1: Any, space: str = "data") -> Anchor | None:
    """Build an anchor, or None for a whole-panel comment."""
    coords = (x0, x1, y0, y1)
    if all(c is None for c in coords):
        return None
    if any(c is None for c in coords):
        raise ValueError(
            "An anchored comment needs all four of x0, x1, y0, y1. "
            "Omit all four to comment on the panel as a whole."
        )
    if space not in ("data", "panel"):
        raise ValueError(
            f"space must be 'data' (coordinates on the axes) or 'panel' "
            f"(fractions of the panel box), got {space!r}"
        )
    if space == "panel":
        for name, c in zip(("x0", "x1", "y0", "y1"), coords):
            if not isinstance(c, (int, float)) or isinstance(c, bool):
                raise ValueError(
                    f"A panel-space anchor needs numeric fractions; {name}={c!r}."
                )
            if not 0.0 <= c <= 1.0:
                raise ValueError(
                    f"A panel-space anchor is measured in fractions of the panel, "
                    f"so every coordinate must be between 0 and 1; {name}={c!r}."
                )
    lo_x, hi_x = _ordered(x0, x1)
    lo_y, hi_y = _ordered(y0, y1)
    return Anchor(x0=lo_x, x1=hi_x, y0=lo_y, y1=hi_y, space=space)


async def add_comment(
    panel_id: str,
    text: str,
    x0: Any = None,
    x1: Any = None,
    y0: Any = None,
    y1: Any = None,
    space: str = "data",
    author: str = "agent",
) -> dict:
    panel = _require_panel(panel_id)
    text = (text or "").strip()
    if not text:
        raise ValueError("A comment needs non-empty text.")
    if author not in ("agent", "human"):
        raise ValueError(f"author must be 'agent' or 'human', got {author!r}")
    anchor = _anchor(x0, x1, y0, y1, space)
    if anchor is not None and anchor.space == "data" and panel.type != "plotly":
        raise ValueError(
            f"Only plotly panels have data coordinates to anchor to; {panel_id!r} is {panel.type!r}. "
            "Pass space='panel' to anchor to a fraction of the panel box instead, "
            "or omit x0/x1/y0/y1 to comment on the panel as a whole."
        )
    comment = Comment(
        id=uuid.uuid4().hex[:12],
        panel_id=panel_id,
        view=panel.view,
        author=author,
        text=text,
        ts=time.time(),
        anchor=anchor,
    )
    STORE.comments[comment.id] = comment
    await STORE.broadcast_comments()
    return {"ok": True, "comment": STORE.comment_dict(comment)}


async def list_comments(panel_id: str | None = None, view: str | None = None,
                        include_resolved: bool = False) -> dict:
    comments = STORE.sorted_comments()
    if panel_id is not None:
        comments = [c for c in comments if c.panel_id == panel_id]
    if view is not None:
        comments = [c for c in comments if c.view == view]
    if not include_resolved:
        comments = [c for c in comments if not c.resolved]
    return {"comments": [STORE.comment_dict(c) for c in comments]}


async def resolve_comment(comment_id: str, resolved: bool = True) -> dict:
    comment = _require_comment(comment_id)
    comment.resolved = bool(resolved)
    await STORE.broadcast_comments()
    return {"ok": True, "comment": STORE.comment_dict(comment)}


async def edit_comment(comment_id: str, text: str) -> dict:
    """Rewrite a comment's text in place, keeping its id, anchor, and place in the order."""
    comment = _require_comment(comment_id)
    text = (text or "").strip()
    if not text:
        raise ValueError("A comment needs non-empty text.")
    # `ts` is the posting time and drives pin numbering, so an edit leaves it be.
    comment.text = text
    comment.edited_ts = time.time()
    await STORE.broadcast_comments()
    return {"ok": True, "comment": STORE.comment_dict(comment)}


async def delete_comment(comment_id: str) -> dict:
    _require_comment(comment_id)
    del STORE.comments[comment_id]
    await STORE.broadcast_comments()
    return {"ok": True, "deleted": comment_id}


# --------------------------------------------------------------------------
# Dispatch table — every mutation is journalled on its way through
# --------------------------------------------------------------------------

# Ops that change the workspace. Wrapping here rather than decorating each
# function catches both callers: the /rpc endpoint and the browser-driven
# comment mutations in web.py, which dispatch through this same table.
MUTATING_OPS = {
    "create_view", "delete_view", "set_layout", "upsert_panel", "patch_panel",
    "append_data", "remove_panel", "reset_workspace", "add_comment",
    "resolve_comment", "edit_comment", "delete_comment", "restore_panel",
}

# Ops that define a distinct, restorable version of a plot. Each records a
# snapshot of the resulting panel; streaming appends and layout moves do not,
# so the timeline offers restore points at meaningful edits rather than at every
# frame of a live curve.
SNAPSHOT_OPS = {"upsert_panel", "patch_panel", "restore_panel"}


def _entry_view(op: str, args: dict, panel_id: str | None) -> str | None:
    """Which tab a mutation belongs to — the history is read per-tab."""
    if args.get("view"):
        return args["view"]
    if op in ("create_view", "delete_view") and args.get("name"):
        return args["name"]
    if panel_id:
        panel = STORE.panels.get(panel_id)
        if panel is not None:
            return panel.view
    return None


def _journalled(op: str, fn):
    async def run(**args):
        result = await fn(**args)
        panel_id = args.get("panel_id")
        panel = STORE.panels.get(panel_id) if panel_id else None
        snapshot = None
        if op in SNAPSHOT_OPS and panel is not None:
            # The resolved panel as it stands after the op — enough to recreate
            # it verbatim. $data ids inside point at arrays already persisted.
            snapshot = {
                "view": panel.view,
                "title": panel.title,
                "type": panel.type,
                "spec": panel.spec,
                "code": panel.code,
            }
        persist.record(
            op,
            args,
            view=_entry_view(op, args, panel_id),
            panel_id=panel_id,
            rev=panel.rev if panel else None,
            code=args.get("code"),
            snapshot=snapshot,
        )
        persist.schedule_save()
        return result

    return run


def _build_ops() -> dict:
    ops = {
        "get_workspace": get_workspace,
        "describe_data": describe_data,
        "get_history": get_history,
        "create_view": create_view,
        "delete_view": delete_view,
        "set_layout": set_layout,
        "upsert_panel": upsert_panel,
        "patch_panel": patch_panel,
        "append_data": append_data,
        "remove_panel": remove_panel,
        "restore_panel": restore_panel,
        "reset_workspace": reset_workspace,
        "snapshot": snapshot,
        "get_events": get_events,
        "wait_for_feedback": wait_for_feedback,
        "add_comment": add_comment,
        "list_comments": list_comments,
        "resolve_comment": resolve_comment,
        "edit_comment": edit_comment,
        "delete_comment": delete_comment,
    }
    return {name: _journalled(name, fn) if name in MUTATING_OPS else fn
            for name, fn in ops.items()}


# Dispatch table used by the daemon's /rpc endpoint.
OPS = _build_ops()
