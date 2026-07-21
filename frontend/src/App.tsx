import { useEffect, useMemo, useState } from "react";
import { toPng } from "html-to-image";
import Plotly from "plotly.js-dist-min";
import PanelFrame from "./panels/PanelFrame";
import { plotNodes } from "./panels/PlotlyPanel";
import { onSnapshotRequest, send, sendEvent, useWorkspace } from "./ws";
import { useChrome } from "./theme";
import type { View } from "./types";

export default function App() {
  const { state, connected, log, appendLog } = useWorkspace();
  const chrome = useChrome();
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const viewName = selected ?? state.active_view ?? state.views[0]?.name ?? null;
  const view: View | undefined = useMemo(
    () => state.views.find((v) => v.name === viewName),
    [state.views, viewName]
  );

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

  const submitNote = () => {
    const text = note.trim();
    if (!text) return;
    sendEvent("comment", { text }, undefined, viewName);
    appendLog("you", text);
    setNote("");
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
                  <PanelFrame panel={panel} />
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

      <aside className="sidebar">
        <h2>Activity</h2>
        <div className="log">
          {log.length === 0 && <p className="hint">Notes from the agent appear here.</p>}
          {log.map((e) => (
            <div key={e.id} className={`log-entry ${e.from}`}>
              <span className="who">{e.from === "agent" ? "agent" : "you"}</span>
              <span className="what">{e.text}</span>
            </div>
          ))}
        </div>
        <div className="note-box">
          <textarea
            value={note}
            placeholder="Note to agent — it can read this with get_events…"
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitNote();
            }}
          />
          <button onClick={submitNote}>Send</button>
        </div>
      </aside>
    </div>
  );
}
