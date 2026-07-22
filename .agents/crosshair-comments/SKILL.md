---
name: crosshair-comments
description: Read the human's pinned comments on a Crosshair display, work out what each one is asking for, edit the panels to match, and resolve them. Use when asked to "check the comments", "see what I marked on the plot", "address the feedback on the chart", or after wait_for_feedback returns comment events. Assumes the mcp__crosshair__* tools; load the `crosshair` skill for how panels and specs work.
---

# Acting on Crosshair comments

The human marks up the plot instead of writing you a spec. Each comment is a
short phrase pinned to a region — the region carries most of the meaning, so
resolve *where* it points before deciding *what* it asks for.

## The loop

```
server_status()            # running at all? browser_connected?
list_comments()            # open comments, oldest first
get_workspace()            # the specs you are about to edit — ground truth
  → for each comment: locate → interpret → edit → resolve
snapshot(view=...)         # verify, if the tab is on that view
```

`list_comments(panel_id=, view=)` narrows the list. Resolved ones are hidden
unless you pass `include_resolved=True`.

## Locating a comment

`anchor` is either `space: "data"` or `space: "panel"`, or null for a
whole-panel comment.

**`space: "data"`** — `x0/x1/y0/y1` are in the panel's own axis units, so
compare them against that trace's data. On a log axis they are still raw data
values, not exponents. A box spanning nearly the full extent of both axes is
not pointing at a data feature; treat it as a whole-panel remark.

**`space: "panel"`** — fractions of the panel box, **origin top-left, y
increasing downward** (`CommentLayer.tsx` lays them out as `top: y0 * height`).
So:

| Region | Roughly |
|---|---|
| y0 ≳ 0.8, centred x | the x-axis: its title and tick labels |
| x1 ≲ 0.15, mid y | the y-axis: its title and tick labels |
| y1 ≲ 0.15, centred x | the panel title |
| y0 ≳ 0.85, wide x | the legend, when `legend.orientation` is `"h"` with negative `y` |

These are guides, not guarantees — margins move with the layout. Cross-check
against the spec: a comment naming a unit can only mean an axis that carries
that quantity.

## Interpreting

Comments are terse. Reconstruct the request from the anchor plus the spec.

- *"Change this to km"* on the x-axis of a plot whose x is AU → convert the x
  values, retitle the axis, refit `range`, relabel array `tickvals`/`ticktext`,
  and fix any `hovertemplate` that names the old unit.
- *"log?"* on an axis → `patch_panel(id, {"layout": {"yaxis": {"type": "log"}}})`.
- A tight data-space box round a few points → they mean those points: label,
  annotate, exclude, or explain them.

**Apply the clear ones; ask about the rest.** If a comment is gibberish, a
stray keystroke, or has more than one plausible reading, leave it open and ask
the human — do not guess and resolve. A resolved comment is a claim that you
handled it.

## Editing

- `patch_panel` for layout-only tweaks — titles, scales, ranges, colors. It is
  an RFC 7386 merge patch, so nested layout keys merge.
- `upsert_panel` whenever trace values change. `data` is a list, so a patch
  would replace the whole trace list anyway. Pass `row`/`col`/spans to keep the
  panel where it was.
- **Sweep for stale copies of what you changed.** A unit or label usually
  appears in more than one place: sibling panels, a markdown notes panel, axis
  titles, hover templates. Changing the axis and leaving "distance in AU" in
  the prose next to it is a half-done edit.

## Verifying, and its one real limit

`snapshot` renders client-side from the human's tab, so it **only works for the
view currently on screen** — `active_view` in `get_workspace`. Editing a panel
in another view gives `panel ... is not on screen` or a bare render error.

There is no tool to change the active view. If your work is not on the active
view, say so plainly and ask the human to switch tabs, rather than reporting
the edit as visually confirmed. Never describe a snapshot you did not get.

## Closing out

`resolve_comment(id)` per comment you actually acted on — pins vanish and the
display shows only what is still open. Then report, per comment: what it said,
where it pointed, what you changed. Call out anything left open and why.

If you want a decision before editing, `add_comment(panel_id, text, x0=…)`
pins your question to the same region and `wait_for_feedback(timeout_s=…)`
blocks for the reply. Only block if a browser is actually connected.
