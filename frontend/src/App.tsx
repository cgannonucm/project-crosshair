import { useEffect, useMemo, useState } from "react";
import { toPng } from "html-to-image";
import Plotly from "plotly.js-dist-min";
import PanelFrame from "./panels/PanelFrame";
import { plotNodes } from "./panels/PlotlyPanel";
import HistoryDrawer from "./HistoryDrawer";
import { onClientError, onSnapshotRequest, send, useWorkspace } from "./ws";
import { useChrome } from "./theme";
import type { Comment, View } from "./types";

export default function App() {
  const { state, connected } = useWorkspace();
  const chrome = useChrome();
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  // A rejected action must be visible — silently dropping one is how a comment
  // disappears with no explanation.
  useEffect(() => {
    let clear: number | undefined;
    return onClientError((text) => {
      setError(text);
      window.clearTimeout(clear);
      clear = window.setTimeout(() => setError(null), 8000);
    });
  }, []);

  const viewName = selected ?? state.active_view ?? state.views[0]?.name ?? null;
  const view: View | undefined = useMemo(
    () => state.views.find((v) => v.name === viewName),
    [state.views, viewName]
  );

  // Open comments, bucketed by the panel they are pinned to.
  const commentsByPanel = useMemo(() => {
    const byPanel = new Map<string, Comment[]>();
    for (const c of state.comments ?? []) {
      if (c.resolved) continue;
      const list = byPanel.get(c.panel_id);
      if (list) list.push(c);
      else byPanel.set(c.panel_id, [c]);
    }
    return byPanel;
  }, [state.comments]);

  // Answer snapshot requests so the agent can see exactly what the human sees.
  useEffect(
    () =>
      onSnapshotRequest(async (requestId, target) => {
        try {
          let png: string;
          if (target.panel_id) {
            const node = plotNodes.get(target.panel_id);
            if (node) {
              png = await Plotly.toImage(node, {
                format: "png",
                width: node.clientWidth || 900,
                height: node.clientHeight || 600,
                scale: 2,
              });
            } else {
              const el = document.querySelector<HTMLElement>(
                `[data-panel="${CSS.escape(target.panel_id)}"]`
              );
              if (!el) throw new Error(`panel ${target.panel_id} is not on screen`);
              png = await toPng(el, { pixelRatio: 2, backgroundColor: chrome.surface });
            }
          } else {
            const el = document.getElementById("grid");
            if (!el) throw new Error("no view is rendered");
            png = await toPng(el, { pixelRatio: 2, backgroundColor: chrome.plane });
          }
          send({ type: "snapshot_result", request_id: requestId, png: png.split(",")[1] });
        } catch (err: any) {
          send({ type: "snapshot_result", request_id: requestId, error: String(err?.message ?? err) });
        }
      }),
    [chrome]
  );

  const selectView = (name: string) => {
    setSelected(name);
    send({ type: "set_active_view", view: name });
  };

  const placements = view?.placements ?? [];

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Crosshair</span>
        <nav className="tabs">
          {state.views.map((v) => (
            <button
              key={v.name}
              className={v.name === viewName ? "tab active" : "tab"}
              onClick={() => selectView(v.name)}
            >
              {v.name}
            </button>
          ))}
        </nav>
        <button
          className={showHistory ? "ghost active" : "ghost"}
          onClick={() => setShowHistory((v) => !v)}
          title="Explore the history of every plot"
        >
          History
        </button>
        <span className={connected ? "status ok" : "status down"}>
          {connected ? "connected" : "reconnecting…"}
        </span>
      </header>

      <main className="main">
        {view ? (
          <div
            id="grid"
            className="grid"
            style={{
              gridTemplateColumns: `repeat(${view.cols}, minmax(0, 1fr))`,
              gridTemplateRows: `repeat(${view.rows}, minmax(0, 1fr))`,
            }}
          >
            {placements.map((p) => {
              const panel = state.panels[p.panel_id];
              if (!panel) return null;
              return (
                <div
                  key={p.panel_id}
                  data-panel={p.panel_id}
                  className="cell"
                  style={{
                    gridRow: `${p.row} / span ${p.row_span}`,
                    gridColumn: `${p.col} / span ${p.col_span}`,
                  }}
                >
                  <PanelFrame
                    panel={panel}
                    comments={commentsByPanel.get(p.panel_id) ?? []}
                  />
                </div>
              );
            })}
          </div>
        ) : (
          <div className="empty">
            <p>No views yet.</p>
            <p className="hint">
              Waiting for an agent to call <code>create_view</code>.
            </p>
          </div>
        )}
      </main>

      {showHistory && (
        <HistoryDrawer view={viewName} onClose={() => setShowHistory(false)} />
      )}

      {error && (
        <div className="toast" role="alert" onClick={() => setError(null)}>
          {error}
        </div>
      )}
    </div>
  );
}
