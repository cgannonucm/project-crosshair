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
async def get_history(
    view: str | None = None,
    panel_id: str | None = None,
    limit: int = 50,
    include_args: bool = False,
) -> dict:
    """Read the on-disk record of how the display got to its current state.

    Every mutation — each view created, panel built or patched, comment pinned —
    is journalled to disk with its timestamp, its arguments, and any `code` the
    agent attached. This outlives the workspace itself: entries survive
    reset_workspace and daemon restarts, and cover revisions the live spec has
    long since overwritten.

    Filter by `view` for one tab's history, or by `panel_id` for one plot's.
    Returns the most recent `limit` entries, oldest first. Full figure specs are
    omitted unless you pass `include_args=True` — they are large, and usually
    the op, timestamp, and code are what you want.

    Use this to pick up an earlier session ("what was this plot built from?"),
    to reproduce a figure, or to show the human the provenance of what they are
    looking at.

    Each entry has a `restorable` flag: true for the panel edits (upsert, patch,
    or an earlier restore) whose exact figure can be brought back with
    restore_panel, false for layout, comment, and streaming entries.
    """
    return await client.call(
        "get_history", view=view, panel_id=panel_id, limit=limit, include_args=include_args
    )


@mcp.tool
async def restore_panel(panel_id: str, seq: int) -> dict:
    """Roll a panel back to the version recorded at a history entry.

    `seq` is the `seq` of a `restorable` entry from get_history for this panel —
    an upsert, a patch, or an earlier restore. The panel is recreated exactly as
    it was at that point (its view is recreated too, if it has since been
    deleted). The restore is itself journalled, so it can be undone in turn.

    Find a target with get_history(panel_id=...), then restore_panel(panel_id,
    seq). The human can do the same from the History drawer's restore button.
    """
    return await client.call("restore_panel", panel_id=panel_id, seq=seq)


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
    code: str | None = None,
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

    Pass `code` with the analysis that produced this figure — the snippet that
    computed the data or built the spec. It is stored with the panel and written
    to the on-disk history, which is what makes a plot reproducible later; the
    spec alone records what is on screen, not how you got there. Read it back
    with get_history.
    """
    return await client.call(
        "upsert_panel",
        view=view, panel_id=panel_id, spec=spec, title=title, type=type,
        row=row, col=col, row_span=row_span, col_span=col_span,
        base_dir=_base_dir(), code=code,
    )


@mcp.tool
async def patch_panel(panel_id: str, spec_patch: dict, code: str | None = None) -> dict:
    """Apply a JSON merge patch to a panel's spec without resending the whole figure.

    Keys in `spec_patch` overwrite matching keys; nested objects merge; a null
    value deletes a key. Use this for cheap tweaks — renaming an axis, switching
    to a log scale, recoloring a trace.

    Note that `data` is a list, so a patch replaces the trace list wholesale
    rather than merging into it; use upsert_panel to change traces.

    Example: patch_panel("loss", {"layout": {"yaxis": {"type": "log"}}})

    `code` optionally updates the panel's recorded provenance — pass it when the
    patch reflects a change in the analysis behind the figure, not for cosmetic
    tweaks. Omitting it leaves the existing code untouched.
    """
    return await client.call("patch_panel", panel_id=panel_id, spec_patch=spec_patch,
                             base_dir=_base_dir(), code=code)


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
      - "comment": the human pinned a comment. data has `comment_id`, `text`, and
        `anchor` (the region it is pinned to, or null for a whole-panel comment;
        `anchor.space` is "data" for axis coordinates, "panel" for fractions of
        the panel box — the latter means they marked up the chrome, not the data).
      - "comment_resolved": the human resolved or reopened a comment.
      - "click": the human clicked a single point.

    Returns {"events": [...], "latest_seq": N}.
    """
    return await client.call("get_events", since_seq=since_seq, limit=limit)


@mcp.tool
async def wait_for_feedback(timeout_s: float = 300.0, since_seq: int = 0) -> dict:
    """Block until the human interacts with the display, or until `timeout_s` elapses.

    Use this when you want a human in the loop: render a plot, pin a comment on
    the part you want them to look at with add_comment, then call this to wait
    for their answer. Returns the same shape as get_events, with `timed_out` set
    if nothing arrived.

    Example flow: upsert_panel(...) -> add_comment("resid", "These four points
    sit well off the trend — are they the sensor dropouts?", x0=200, x1=260,
    y0=0.4, y1=0.9) -> wait_for_feedback() -> read their reply comment.
    """
    return await client.call(
        "wait_for_feedback", since_seq=since_seq, timeout_s=timeout_s, timeout=timeout_s + 15.0
    )


# --------------------------------------------------------------------------
# Comments — margin notes pinned to a region of a plot
# --------------------------------------------------------------------------


@mcp.tool
async def add_comment(
    panel_id: str,
    text: str,
    x0: float | str | None = None,
    x1: float | str | None = None,
    y0: float | str | None = None,
    y1: float | str | None = None,
    space: str = "data",
) -> dict:
    """Pin a comment to a region of a plot — the way you point at something.

    The comment shows as a numbered pin on the panel; the human clicks it to read
    the text, exactly like a margin comment in a word processor. Use this to
    narrate ("this shoulder is the LR warmup ending") or to ask a question about
    a specific place in the data, then call wait_for_feedback.

    `x0`/`x1`/`y0`/`y1` bound the region. With the default `space="data"` they are
    **data coordinates**, not pixels — read them off the axes of the figure you
    built, and the pin stays on that data as the human zooms and pans.

    Example — flag a loss spike between steps 200 and 260:

        add_comment("loss", "Loss spikes here — is this the LR restart?",
                    x0=200, x1=260, y0=0.4, y1=0.9)

    Pass `space="panel"` to bound the region in fractions of the panel box
    instead (0-1, x rightward and y downward from the top-left corner). Use that
    for the chrome rather than the data — a tick label, an axis title, a legend —
    where there are no data coordinates and the pin should not move when the
    human zooms. It works on markdown and image panels too.

        add_comment("loss", "These tick labels want SI units",
                    x0=0.0, x1=0.09, y0=0.1, y1=0.9, space="panel")

    Omit all four coordinates to comment on the panel as a whole.
    """
    return await client.call(
        "add_comment", panel_id=panel_id, text=text,
        x0=x0, x1=x1, y0=y0, y1=y1, space=space, author="agent",
    )


@mcp.tool
async def list_comments(
    panel_id: str | None = None, view: str | None = None, include_resolved: bool = False
) -> dict:
    """Read the comments on the display — yours and the human's — oldest first.

    Each comment has an `id`, `author` ("agent" or "human"), `text`, and `anchor`
    (the region it is pinned to, or null for a whole-panel comment). An anchor
    carries a `space`: "data" for axis coordinates, "panel" for fractions of the
    panel box. Filter by `panel_id` or `view`; resolved comments are hidden unless
    you ask for them.

    Use this to pick up a thread from an earlier session, or after
    wait_for_feedback to see what the human pinned and where.
    """
    return await client.call(
        "list_comments", panel_id=panel_id, view=view, include_resolved=include_resolved
    )


@mcp.tool
async def resolve_comment(comment_id: str, resolved: bool = True) -> dict:
    """Mark a comment as resolved, hiding its pin from the plot.

    Do this once you have acted on the human's comment, so the display shows only
    what is still open. Pass `resolved=False` to reopen one.
    """
    return await client.call("resolve_comment", comment_id=comment_id, resolved=resolved)


@mcp.tool
async def edit_comment(comment_id: str, text: str) -> dict:
    """Rewrite a comment's text, keeping its pin, anchor, and number.

    Use this to revise a note you already posted — annotating a plot with a
    conclusion you have since sharpened — rather than deleting and re-adding it.
    """
    return await client.call("edit_comment", comment_id=comment_id, text=text)


@mcp.tool
async def delete_comment(comment_id: str) -> dict:
    """Delete a comment outright. Prefer resolve_comment, which keeps the record."""
    return await client.call("delete_comment", comment_id=comment_id)
