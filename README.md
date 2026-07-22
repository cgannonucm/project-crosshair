# Crosshair

A browser-based visualization workbench **designed for agents**. An agent drives a
live, multi-panel scientific display through an MCP server; a human reviews it in
the browser and hands selections, zooms, and comments back.

Existing agentic coding harnesses can emit a static PNG and little else. Crosshair
gives an agent a persistent, configurable canvas instead: tile several plots for
comparison, patch and stream them in place, screenshot its own output to verify it,
and block waiting for a human to point at something.

## Architecture

```
┌─────────┐ MCP (stdio) ┌──────────────┐  HTTP   ┌────────────────────┐  WS  ┌─────────┐
│  Agent  │────────────▶│ thin client  │────────▶│ crosshair daemon   │◀────▶│ Browser │
└─────────┘             │ (per session)│  /rpc   │ owns state + queue │      └─────────┘
     ▲                  └──────────────┘         │ static, /ws, /data │
     └──────────────────────────────────────────  events, snapshots ─┘
```

The display is a **detached daemon** (default port **8137**) that owns the
workspace. The MCP process an agent harness spawns is a thin client over it, so
plots and the human's open browser tab survive the agent session ending, an MCP
restart, or several sessions sharing one display. The workspace is checkpointed to
disk, so it survives the daemon itself restarting.

The browser is a pure projection of daemon-held state — full state on connect and
after every mutation, so reloads, reconnects, and multiple viewers are free.

## Setup

```bash
# 1. Build the frontend into the Python package
cd frontend && npm install && npm run build && cd ..

# 2. Install the server
pip install -e server        # or: uv pip install -e server
```

Register with Claude Code:

```bash
claude mcp add crosshair -- python -m crosshair
```

The agent can bring the display up itself with `open_ui`; otherwise open
<http://localhost:8137>. Point `$ref` file paths at a specific directory with
`--data-dir /path/to/results` (defaults to the working directory).

## Server lifecycle

The daemon starts automatically on the first tool call and keeps running after the
agent goes away. Nothing needs to be started by hand.

```bash
python -m crosshair serve      # start it explicitly (add --port to pin one)
python -m crosshair status     # is one running, and what is on screen?
python -m crosshair stop       # stop it
```

If port 8137 is held by an **unrelated** process, the daemon steps to the next free
port (up to 8146) and reports the real URL — it never dies from a port conflict. If
the port is held by an existing *Crosshair* daemon, that one is reused instead.

The workspace is persisted to disk, so it comes back after a restart — a crash, a
reboot, or `stop` followed by a later tool call. To clear the plots but keep the
server and browser tab alive, use `reset_workspace`.

## Persistence and history

Everything is written under `~/.crosshair/workspace` (or `$CROSSHAIR_HOME`):

```
state.json           the current workspace, rewritten after every mutation
history.jsonl        append-only record of every mutation, one JSON per line
data/<ref_id>.json   materialized $ref arrays, so restored panels still render
```

`state.json` is what makes plots survive a restart. `history.jsonl` is what makes
them **reproducible**: every view created, panel built or patched, and comment
pinned is journalled with its timestamp and arguments. It outlives the workspace —
entries survive `reset_workspace` and cover revisions the live spec has long since
overwritten.

Panel tools take an optional `code` argument, where an agent records the analysis
that produced a figure:

```python
upsert_panel(view="training", panel_id="loss", spec={...},
             code="df = pd.read_parquet('runs.parquet')\nfig = px.line(df, x='step', y='loss')")
```

The spec records what is on screen; `code` records how it was arrived at. Read
both back with `get_history(view=...)` or `get_history(panel_id=...)`, which
returns the record oldest-first and omits full specs unless you pass
`include_args=True`.

Panel edits (upsert, patch, and restores themselves) are marked `restorable` in
the record — each keeps a snapshot of the resulting figure. `restore_panel(panel_id,
seq)` rolls the panel back to that exact version; the restore is journalled in
turn, so it can be undone. In the browser, the **History** panel lists every
update and puts a **restore** button on each restorable version.

Nothing prunes `history.jsonl` or `data/` — delete the workspace directory if it
outgrows its usefulness.

## Tools

| Tool | Purpose |
|---|---|
| `ensure_server` | Start the display if it isn't running; reuse it if it is |
| `server_status` | Is a display running, and what does it already hold? |
| `shutdown_server` | Stop the display and discard the workspace |
| `open_ui` | Open the display in the human's browser |
| `reset_workspace` | Clear all views and panels, keeping the server up |
| `get_workspace` | Full current state — the agent's ground truth |
| `get_history` | The on-disk record of how the display got here, with code |
| `restore_panel` | Roll a panel back to a version recorded in the history |
| `describe_data` | Row count, columns, dtypes, head of a data file |
| `create_view` / `delete_view` | Manage views (browser tabs), each an R×C grid |
| `set_layout` | Re-tile a view; supports row/column spans |
| `upsert_panel` | Create or replace a panel (plotly / markdown / image), with optional `code` |
| `patch_panel` | JSON-merge-patch a spec without resending the figure |
| `append_data` | Extend a trace in place — live/streaming plots |
| `remove_panel` | Remove a panel |
| `snapshot` | PNG of a view or panel, returned to the agent as an image |
| `get_events` | Drain human interactions (non-blocking) |
| `wait_for_feedback` | Block until the human responds |
| `add_comment` | Pin a comment to a region of a plot |
| `list_comments` | Read the comment thread on a panel or view |
| `edit_comment` | Rewrite a comment's text, keeping its pin |
| `resolve_comment` / `delete_comment` | Close out a comment |

## Data plane

Datasets never travel through tool calls. Any array in a Plotly spec can be a file
reference:

```json
{"$ref": {"file": "results/run3.parquet", "column": "loss"}}
```

The server loads it (parquet / csv / tsv / json / feather), caches the frame, and
rewrites the ref to a `/data/{id}` handle the browser fetches directly. Optional
keys: `query` (a pandas query string, e.g. `"epoch > 10"`) and `stride` for
downsampling. Small data can still be inlined as a plain array.

## Comments

Discussion happens **on the plot**, not in a chat pane. A comment is pinned to a
region of a panel and shows as a numbered pin; clicking it reveals the text, the
way margin comments work in a word processor. The human drags out a region with
the panel's **comment** button; an agent does the same thing with `add_comment`:

```python
add_comment("loss", "This spike is the LR restart — should we damp it?",
            x0=300, x1=340, y0=0.6, y1=1.2)
```

Anchors are stored in **data coordinates**, so a pin stays on the data it refers
to as the human zooms and pans. A pin whose region scrolls off-screen falls back
to a stack in the panel's corner rather than disappearing. Agent pins are blue,
the human's are green. Omit the coordinates to comment on a panel as a whole —
that also works on markdown and image panels.

There is deliberately no chat sidebar: Crosshair is meant to sit alongside a
terminal agent harness, which is where open-ended conversation belongs. What the
display adds is the thing a terminal cannot do — pointing at a specific place in
the data.

## Feedback loop

The browser reports back to the agent's event queue:

- **selection** — box/lasso selection, with point indices and values
- **zoom** — axis ranges after zoom/pan (debounced)
- **click** — a single point
- **comment** — a comment the human pinned, with its text and anchor
- **comment_resolved** — the human resolved or reopened a comment
- **comment_edited** — the human rewrote a comment's text

The agent reads these with `get_events(since_seq)` or blocks on
`wait_for_feedback(timeout_s)` — so it can pin "are these the sensor dropouts?"
on a cluster of outliers and wait for the answer.

## Demo

```bash
python examples/demo.py
```

Generates two simulated training runs, builds a 2×2 view (a wide loss comparison
over a residual scatter and a live-streaming panel), patches the y-axis to log,
streams 100 points, pins a comment on a region of the residual plot, and — if a
browser is open — waits for you to answer it.

## Development

```bash
cd frontend && npm run dev      # Vite on :5173, proxying /ws and /data to :8137
python -m crosshair serve       # the daemon on its own, no MCP
```

`npm run build` writes into `server/crosshair/static/`, which the server mounts at
`/`.

## Notes

- Use `scattergl` rather than `scatter` above a few thousand points — it renders on
  the GPU and stays interactive into the millions.
- Panel colors come from a colorblind-validated categorical palette applied as
  Plotly defaults (`frontend/src/theme.ts`); an agent's explicit spec always wins.
- Light and dark are both selected against their own surfaces, following the
  viewer's OS theme.
