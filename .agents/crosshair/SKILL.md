---
name: crosshair
description: Drive the Crosshair visualization workbench (mcp__crosshair__* tools) — a live, multi-panel browser display an agent builds and a human reviews. Use whenever plotting data for a human to look at, comparing runs side by side, streaming a metric as it is computed, or asking a human to select/comment on points. Prefer this over emitting a static PNG or matplotlib script when a Crosshair MCP server is available.
---

# Crosshair

A persistent browser display you drive over MCP. Views are tabs holding an R×C
grid; panels are plots (Plotly), markdown, or images placed in that grid. The
daemon owns the state and outlives your session, so plots and the human's tab
survive an MCP restart.

## When to use this instead of a static image

Reach for Crosshair when the human should *look at* something: comparing several
runs, watching a metric grow, or when you want them to point at outliers. Use a
one-off matplotlib PNG only for a single throwaway figure nobody will interact
with.

## The normal flow

```
open_ui()                       # once, at the start — puts it on their screen
describe_data(file=...)         # never guess column names
create_view(name=..., rows=, cols=)
upsert_panel(...)               # one per plot
snapshot(...)                   # look at your own output before claiming it works
add_comment(...) + wait_for_feedback()  # only when you need a human answer
```

If a display may already be up from an earlier session, call `get_workspace`
first and either build on what's there or `reset_workspace()` for a clean slate.
`reset_workspace` keeps the server and the human's tab alive — prefer it over
`shutdown_server`, which discards everything and shows "reconnecting".

## Data goes by reference, not through tool calls

Never paste a large array into a spec. Any array in a Plotly spec can be a file
reference, resolved server-side and fetched directly by the browser:

```json
{"$ref": {"file": "results/run3.parquet", "column": "loss"}}
```

Optional keys: `query` (pandas query string, e.g. `"epoch > 10"`) and `stride`
(take every Nth point). Supported: `.parquet` `.pq` `.csv` `.tsv` `.json`
`.feather` `.arrow`. Relative paths resolve against the working directory (or
`--data-dir` / `CROSSHAIR_DATA_DIR`).

Inline arrays are fine for small or streaming series — and *required* for
streaming, since `append_data` cannot extend a `$ref`-backed trace.

If you have data in memory that isn't on disk yet, write it to a parquet file
and `$ref` it rather than inlining thousands of numbers.

## Panels

```python
upsert_panel(
    view="training", panel_id="loss", title="Loss — A vs B",
    row=1, col=1, col_span=2,
    spec={
      "data": [
        {"type": "scattergl", "mode": "lines", "name": "run A",
         "x": {"$ref": {"file": "runs.parquet", "column": "step"}},
         "y": {"$ref": {"file": "runs.parquet", "column": "loss_a"}}},
      ],
      "layout": {"xaxis": {"title": {"text": "step"}},
                 "yaxis": {"title": {"text": "loss"}}},
    },
)
```

- `panel_id` is **globally unique across views**, not per view. Reusing an id
  moves the panel to the new view.
- Omit `row`/`col` to keep the current position, or take the next free cell if
  new. Give both to place explicitly (1-indexed); `row_span`/`col_span` let a
  panel straddle cells — a wide time series over two detail plots.
- `type="markdown"` with `spec={"text": "..."}` puts your analysis next to the
  plots. `type="image"` takes `spec={"src": "data:image/png;base64,..."}` or a
  URL.

### Scrubbing through slices — don't drive a slider with `animate`

`spec.frames` is passed through to Plotly, but **a slider whose steps use
`method: "animate"` will hang after the first move.** Plotly's animate promise
never settles for these figures: the frame renders, the animation loop keeps
rescheduling, and every later step queues behind it — so the slider goes dead
and the frame appears frozen. This is upstream behaviour, not something the
display can work around.

Build a scrubber by preloading every slice as its own trace and having the
slider toggle `visible`. No animation queue is involved and scrubbing stays
immediate (~20ms a step) even for dozens of megabytes of images:

```python
n = len(slices)
one_hot = lambda i: [j == i for j in range(n)]
spec = {
  "data": [{"type": "image", "source": src, "hoverinfo": "skip", "visible": i == 0}
           for i, src in enumerate(slices)],
  "layout": {
    "sliders": [{
      "active": 0,
      "currentvalue": {"prefix": "slice z = "},
      "steps": [{"label": str(z), "method": "restyle", "args": [{"visible": one_hot(i)}]}
                for i, z in enumerate(depths)],
    }],
  },
}
```

Set `hoverinfo: "skip"` on image traces unless you need the pixel readout —
hover on a `source` image makes Plotly decode it to read pixel values.

### Cheap edits

`patch_panel(panel_id, spec_patch)` is an RFC 7386 merge patch — nested objects
merge, `null` deletes a key. Use it for axis renames, log scales, recoloring:

```python
patch_panel("loss", {"layout": {"yaxis": {"type": "log"}}})
```

`data` is a list, so a patch **replaces the whole trace list**. To change traces,
use `upsert_panel`.

### Streaming

`append_data(panel_id, trace=0, x=[...], y=[...])` extends an inline trace in
place — call it inside a loop to watch a curve grow. `trace` is the 0-indexed
position in `spec["data"]`. Seed the panel with `"x": [], "y": []`.

## Layout

`set_layout(view, rows, cols, placements=[...])` re-tiles a view. Each placement
is `{"panel_id", "row", "col", "row_span"?, "col_span"?}`; omitted panels keep
their position. Panels must already belong to that view.

## Verify your own work

`snapshot(view=...)` or `snapshot(panel_id=...)` returns a PNG of exactly what
the human sees — **requires an open browser tab**, since it renders client-side.
Call `open_ui()` first. Use it after building a layout to check that axes,
labels, legends, and scales are actually readable before saying you're done.

## Human in the loop

There is no chat pane — discussion happens *on the plot*. `add_comment` pins a
note to a region of a panel, shown as a numbered pin the human clicks to read:

```python
add_comment("resid", "These four sit well off the trend — sensor dropouts?",
            x0=200, x1=260, y0=0.4, y1=0.9)
wait_for_feedback(timeout_s=120)   # -> their reply comment, with its anchor
```

`x0/x1/y0/y1` are **data coordinates**, so the pin tracks the data through zoom
and pan; omit all four to comment on a panel as a whole. `list_comments()` reads
the thread (yours and theirs), `resolve_comment(id)` closes one out.

Then `wait_for_feedback(timeout_s=...)` blocks until they respond (capped at
3600s), or `get_events(since_seq=N)` drains without blocking. Pass the highest
`seq` you've seen back as `since_seq`.

Event kinds: `selection` (box/lasso — `indices`, `points`, `trace`), `zoom`
(axis ranges), `click`, `comment` (their pinned text plus `anchor`),
`comment_resolved`.

Don't block on `wait_for_feedback` unless you actually asked a question and a
browser is connected (`get_workspace()["browser_connected"]`).

## Plotting quality

- Use `scattergl`, not `scatter`, above a few thousand points — it renders on
  the GPU and stays interactive into the millions.
- Colors come from a colorblind-validated categorical palette applied as Plotly
  defaults, tuned for both light and dark. **Don't set trace colors unless you
  have a reason** — an explicit spec always wins, and usually wins wrongly.
- Always label axes (`{"title": {"text": ...}}`) and name traces.
- One idea per panel; use the grid for comparison rather than cramming traces.

## Lifecycle

The daemon (default port 8137, stepping up to 8146 if taken) starts on the first
tool call and keeps running after you go away — nothing needs starting by hand.
State is in memory only.

| Need | Tool |
|---|---|
| Is a display already up, with what on it? | `server_status` / `get_workspace` |
| Start it early or pin a port | `ensure_server(port=)` |
| Clean slate, keep the tab | `reset_workspace` |
| Tear it all down | `shutdown_server` |

## Common errors

- *"No view named X"* — `create_view` first; the message lists existing views.
- *"Trace N axis 'y' is a file reference"* — you tried to `append_data` to a
  `$ref` trace. Rebuild the panel with inline arrays.
- *"Column 'x' not in file"* — run `describe_data` instead of guessing.
- `snapshot` hanging or failing — no browser tab is open; call `open_ui`.
