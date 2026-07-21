"""MCP tools — a thin client over the detached daemon.

Docstrings here are the agent's only documentation, so keep them concrete.
Every tool auto-starts the daemon if it isn't already running; `ensure_server`
exists for when you want to do that explicitly (or on a specific port).
"""
from __future__ import annotations

import base64
import os
import webbrowser

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from . import client
from .daemon import DEFAULT_PORT

mcp = FastMCP("crosshair")


def _base_dir() -> str:
    """Where the daemon should resolve relative $ref paths for *this* agent."""
    return os.environ.get("CROSSHAIR_DATA_DIR") or os.getcwd()


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------


@mcp.tool
async def ensure_server(port: int = DEFAULT_PORT) -> dict:
    """Make sure the display server is running, starting it if necessary.

    The server is a detached process that owns the workspace, so views and
    panels — and the human's open browser tab — survive across agent sessions.
    Every other tool calls this implicitly; use it directly to start the display
    early, to pin a port, or to check what is already on screen.

    If a Crosshair server is already running it is reused rather than replaced.
    If the port is taken by something else, the next free port is used and the
    real URL is returned.
    """
    url = await client.ensure(port=port)
    health = await client._health(url) or {}
    return {
        "ok": True,
        "url": url,
        "pid": health.get("pid"),
        "browser_connected": health.get("browser_connected", False),
        "views": health.get("views", 0),
        "panels": health.get("panels", 0),
    }


@mcp.tool
async def server_status() -> dict:
    """Report whether the display server is running, without starting one.

    Returns `running: false` if nothing is up — useful for deciding whether a
    previous session left a workspace behind.
    """
    url = await client.discover()
    if url is None:
        return {"running": False, "url": None}
    health = await client._health(url) or {}
    return {"running": True, "url": url, **{k: v for k, v in health.items() if k != "service"}}


@mcp.tool
async def shutdown_server() -> dict:
    """Stop the display server and discard the workspace.

    The human's browser tab will show "reconnecting". Only do this when you are
    finished with the display — a later tool call will start a fresh, empty
    server. To clear the plots but keep the server up, use reset_workspace.
    """
    url = await client.discover()
    if url is None:
        return {"ok": True, "was_running": False}
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.post(f"{url}/control/shutdown")
    except Exception:
        pass
    client.forget()
    return {"ok": True, "was_running": True, "url": url}


@mcp.tool
async def open_ui() -> dict:
    """Open the display in the human's default browser.

    Use this once at the start of a session instead of asking them to paste a
    URL. Returns the URL either way, so you can still mention it.
    """
    url = await client.ensure()
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    return {"ok": True, "url": url, "opened": opened}


@mcp.tool
async def reset_workspace() -> dict:
    """Delete every view and panel, keeping the server and the browser tab alive.

    This is the clean slate you usually want between unrelated tasks — cheaper
    and less disruptive than shutdown_server.
    """
    return await client.call("reset_workspace")


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


@mcp.tool
async def get_workspace() -> dict:
    """Return the full current workspace: every view, its grid layout, and every panel.

    Call this first if you are unsure what is already on screen — a previous
    session may have left plots up — or after a series of edits to confirm the
    result. This is the ground truth; the browser is a projection of it.
    """
    return await client.call("get_workspace")


@mcp.tool
async def describe_data(file: str) -> dict:
    """Inspect a data file before plotting it: row count, column names, dtypes, first 5 rows.

    Supports .parquet, .csv, .tsv, .json, .feather. Relative paths resolve
    against your working directory. Use this instead of guessing column names.
    """
    return await client.call("describe_data", file=file, base_dir=_base_dir())


# --------------------------------------------------------------------------
# Views and layout
# --------------------------------------------------------------------------


@mcp.tool
async def create_view(name: str, rows: int = 2, cols: int = 2) -> dict:
    """Create a named view — a tab in the browser holding a rows x cols grid of panels.

    Panels added to this view fill the grid in reading order unless you place
    them explicitly. If the view already exists its grid is resized in place.

    Example: create_view("training", rows=2, cols=2) gives a 2x2 comparison grid.
    """
    return await client.call("create_view", name=name, rows=rows, cols=cols)


@mcp.tool
async def delete_view(name: str) -> dict:
    """Delete a view and every panel in it."""
    return await client.call("delete_view", name=name)


@mcp.tool
async def set_layout(view: str, rows: int, cols: int, placements: list[dict] | None = None) -> dict:
    """Re-tile a view: resize its grid and optionally reposition panels.

    `placements` is a list of {"panel_id", "row", "col", "row_span"?, "col_span"?}
    with 1-indexed row/col. Panels you omit keep their current position.
    Spans let a panel straddle cells — e.g. a wide time series above two
    smaller detail plots:

        set_layout("run", rows=2, cols=2, placements=[
            {"panel_id": "timeseries", "row": 1, "col": 1, "col_span": 2},
            {"panel_id": "hist",       "row": 2, "col": 1},
            {"panel_id": "resid",      "row": 2, "col": 2},
        ])
    """
    return await client.call("set_layout", view=view, rows=rows, cols=cols, placements=placements)


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------


@mcp.tool
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
) -> dict:
    """Create or replace a panel. This is the main tool for putting a plot on screen.

    `type` is "plotly" (default), "markdown", or "image".

    For type="plotly", `spec` is a Plotly figure: {"data": [...traces], "layout": {...}}.
    Any data array may be replaced with a file reference so large datasets never
    travel through this tool call:

        {"$ref": {"file": "results/run3.parquet", "column": "loss"}}

    Optional $ref keys: "query" (a pandas query string, e.g. "epoch > 10") and
    "stride" (take every Nth point, for downsampling dense series).

    Example — a loss curve read straight from a parquet file:

        upsert_panel(
            view="training", panel_id="loss", title="Training loss",
            spec={
              "data": [{
                "type": "scattergl", "mode": "lines", "name": "run3",
                "x": {"$ref": {"file": "results/run3.parquet", "column": "step"}},
                "y": {"$ref": {"file": "results/run3.parquet", "column": "loss"}},
              }],
              "layout": {"xaxis": {"title": {"text": "step"}},
                         "yaxis": {"title": {"text": "loss"}}},
            },
        )

    For type="markdown", `spec` is {"text": "..."} — useful for putting a caption
    or your analysis next to the plots. For type="image", `spec` is
    {"src": "data:image/png;base64,..."} or {"src": "https://..."}.

    If `row`/`col` are omitted the panel keeps its existing position, or takes
    the next free cell if it is new. Use "scattergl" rather than "scatter" for
    more than a few thousand points — it renders on the GPU.
    """
    return await client.call(
        "upsert_panel",
        view=view, panel_id=panel_id, spec=spec, title=title, type=type,
        row=row, col=col, row_span=row_span, col_span=col_span,
        base_dir=_base_dir(),
    )


@mcp.tool
async def patch_panel(panel_id: str, spec_patch: dict) -> dict:
    """Apply a JSON merge patch to a panel's spec without resending the whole figure.

    Keys in `spec_patch` overwrite matching keys; nested objects merge; a null
    value deletes a key. Use this for cheap tweaks — renaming an axis, switching
    to a log scale, recoloring a trace.

    Note that `data` is a list, so a patch replaces the trace list wholesale
    rather than merging into it; use upsert_panel to change traces.

    Example: patch_panel("loss", {"layout": {"yaxis": {"type": "log"}}})
    """
    return await client.call("patch_panel", panel_id=panel_id, spec_patch=spec_patch,
                             base_dir=_base_dir())


@mcp.tool
async def append_data(panel_id: str, trace: int, x: list | None = None, y: list | None = None) -> dict:
    """Append points to an existing trace — for live/streaming plots.

    Extends the trace in place rather than re-sending the figure, so you can
    call this in a training loop to watch a curve grow. `trace` is the
    0-indexed position in the panel's `data` list.

    Only works on traces built from inline arrays: a trace whose data came from
    a file reference cannot be appended to, since the file on disk is the
    source of truth.

    Example: append_data("loss", trace=0, x=[101, 102], y=[0.31, 0.29])
    """
    return await client.call("append_data", panel_id=panel_id, trace=trace, x=x, y=y)


@mcp.tool
async def remove_panel(panel_id: str) -> dict:
    """Remove a panel from its view."""
    return await client.call("remove_panel", panel_id=panel_id)


# --------------------------------------------------------------------------
# Snapshot — lets you see what the human sees
# --------------------------------------------------------------------------


@mcp.tool
async def snapshot(view: str | None = None, panel_id: str | None = None) -> Image:
    """Render the current display to a PNG and return it, so you can verify your own plot.

    Pass `panel_id` for a single panel, or `view` (or neither, for the active
    view) to capture the whole grid. Requires an open browser tab — the image is
    rendered client-side from exactly what the human is looking at. Call
    open_ui first if nobody is watching yet.

    Use this after building a layout to check that axes, labels, and scales are
    readable before asking the human to review it.
    """
    result = await client.call("snapshot", view=view, panel_id=panel_id, timeout=45.0)
    return Image(data=base64.b64decode(result["png_base64"]), format="png")


# --------------------------------------------------------------------------
# Human feedback
# --------------------------------------------------------------------------


@mcp.tool
async def get_events(since_seq: int = 0, limit: int = 100) -> dict:
    """Drain human interactions from the browser without blocking.

    Returns events with a monotonic `seq` — pass the highest one you have seen
    back as `since_seq` next time. Event kinds:

      - "selection": the human box/lasso-selected points. data has `indices`
        (point indices within the trace), `points` (their x/y values), `trace`.
      - "zoom": the human zoomed or panned. data has the visible axis ranges.
      - "comment": free text the human wrote on a panel or the workspace.
      - "click": the human clicked a single point.

    Returns {"events": [...], "latest_seq": N}.
    """
    return await client.call("get_events", since_seq=since_seq, limit=limit)


@mcp.tool
async def wait_for_feedback(timeout_s: float = 300.0, since_seq: int = 0) -> dict:
    """Block until the human interacts with the display, or until `timeout_s` elapses.

    Use this when you want a human in the loop: render a plot, tell them what to
    look at with add_note, then call this to wait for their selection or comment.
    Returns the same shape as get_events, with `timed_out` set if nothing arrived.

    Example flow: upsert_panel(...) -> add_note("I've plotted the residuals;
    please lasso the outliers") -> wait_for_feedback() -> read the indices.
    """
    return await client.call(
        "wait_for_feedback", since_seq=since_seq, timeout_s=timeout_s, timeout=timeout_s + 15.0
    )


@mcp.tool
async def add_note(text: str, view: str | None = None) -> dict:
    """Post a note into the browser's activity log — tell the human what to look at.

    This is how you narrate: explain what you plotted and what you want them to
    check, then call wait_for_feedback.
    """
    return await client.call("add_note", text=text, view=view)
