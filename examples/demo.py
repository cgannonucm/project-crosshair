"""End-to-end demo: drives the Crosshair MCP server exactly as an agent would.

    python examples/demo.py

Generates a sample dataset, launches the server over stdio, builds a 2x2
comparison view, streams points into a live plot, then waits for you to
select points in the browser. Open the printed URL to watch it happen.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "examples" / "data"


def make_dataset() -> Path:
    """Two simulated training runs plus residuals, written to parquet."""
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "runs.parquet"
    rng = np.random.default_rng(7)
    step = np.arange(1, 601)
    frame = pd.DataFrame(
        {
            "step": step,
            "loss_a": 2.4 * np.exp(-step / 160) + 0.09 + rng.normal(0, 0.012, step.size),
            "loss_b": 2.4 * np.exp(-step / 95) + 0.14 + rng.normal(0, 0.02, step.size),
        }
    )
    frame["residual"] = frame["loss_a"] - frame["loss_b"]
    frame.to_parquet(path)
    return path


async def main() -> None:
    make_dataset()

    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "crosshair"],
        env={"PYTHONPATH": str(ROOT / "server"), "CROSSHAIR_DATA_DIR": str(DATA)},
        cwd=str(ROOT),
    )

    async with Client(transport) as client:
        async def call(tool_name: str, /, **kwargs):
            result = await client.call_tool(tool_name, kwargs)
            return result.data

        print("tools:", ", ".join(sorted(t.name for t in await client.list_tools())))

        # 1. Inspect the data rather than guessing column names.
        info = await call("describe_data", file="runs.parquet")
        print(f"runs.parquet: {info['rows']} rows, columns "
              f"{[c['name'] for c in info['columns']]}")

        # 2. Build a 2x2 comparison view.
        await call("create_view", name="training", rows=2, cols=2)

        ref = lambda col: {"$ref": {"file": "runs.parquet", "column": col}}  # noqa: E731

        await call(
            "upsert_panel",
            view="training", panel_id="loss", title="Loss — run A vs run B",
            row=1, col=1, col_span=2,
            spec={
                "data": [
                    {"type": "scattergl", "mode": "lines", "name": "run A",
                     "x": ref("step"), "y": ref("loss_a")},
                    {"type": "scattergl", "mode": "lines", "name": "run B",
                     "x": ref("step"), "y": ref("loss_b")},
                ],
                "layout": {
                    "xaxis": {"title": {"text": "step"}},
                    "yaxis": {"title": {"text": "loss"}},
                },
            },
        )

        await call(
            "upsert_panel",
            view="training", panel_id="residual", title="Residual (A − B)",
            row=2, col=1,
            spec={
                "data": [{"type": "scattergl", "mode": "markers", "name": "residual",
                          "marker": {"size": 4},
                          "x": ref("step"), "y": ref("residual")}],
                "layout": {"xaxis": {"title": {"text": "step"}},
                           "yaxis": {"title": {"text": "A − B"}}},
            },
        )

        await call(
            "upsert_panel",
            view="training", panel_id="live", title="Live stream",
            row=2, col=2,
            spec={
                "data": [{"type": "scatter", "mode": "lines", "name": "signal", "x": [], "y": []}],
                "layout": {"xaxis": {"title": {"text": "t"}},
                           "yaxis": {"title": {"text": "value"}}},
            },
        )

        # 3. Patch a spec without resending the figure.
        await call("patch_panel", panel_id="loss", spec_patch={"layout": {"yaxis": {"type": "log"}}})
        print("patched loss panel to a log y-axis")

        # 4. Stream points into the live panel.
        for i in range(20):
            xs = list(range(i * 5, i * 5 + 5))
            ys = [float(np.sin(x / 9) + np.random.normal(0, 0.05)) for x in xs]
            await call("append_data", panel_id="live", trace=0, x=xs, y=ys)
            await asyncio.sleep(0.15)
        print("streamed 100 points into the live panel")

        # 5. A markdown panel in a second view, for narration next to plots.
        await call("create_view", name="notes", rows=1, cols=1)
        await call(
            "upsert_panel",
            view="notes", panel_id="summary", type="markdown", title="Analysis",
            spec={"text": "## Run comparison\n\n"
                          "Run **A** converges more slowly but reaches a lower final loss.\n\n"
                          "- residual is positive early, negative after ~step 250\n"
                          "- `loss_b` shows higher variance throughout"},
        )

        state = await call("get_workspace")
        print(f"workspace: {len(state['views'])} views, {len(state['panels'])} panels; "
              f"browser_connected={state['browser_connected']}")
        print(f"open {state['url']}")

        # 6. Snapshot — only works with a browser open.
        if state["browser_connected"]:
            await call("add_note", text="Please lasso any outliers in the residual panel.")
            print("waiting 90s for you to select points in the browser…")
            fb = await call("wait_for_feedback", timeout_s=90)
            for ev in fb["events"]:
                print(f"  [{ev['kind']}] panel={ev['panel_id']} {str(ev['data'])[:120]}")
            if not fb["events"]:
                print("  (no interaction received)")
        else:
            print("no browser connected — open the URL above and rerun to exercise "
                  "snapshot() and wait_for_feedback()")


if __name__ == "__main__":
    asyncio.run(main())
