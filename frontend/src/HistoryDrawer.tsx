/** A slide-over that explores the on-disk mutation log — how the display got here.
 *
 *  History is read over HTTP (GET /api/history), not the WS state channel: it is
 *  a separate on-disk record that outlives the live workspace, and it is only
 *  wanted when the drawer is open, so it is fetched on demand rather than pushed.
 */
import { useCallback, useEffect, useState } from "react";
import type { HistoryEntry } from "./types";

// A short, readable label per op. Missing keys fall back to the raw op name.
const OP_LABEL: Record<string, string> = {
  create_view: "view +",
  delete_view: "view −",
  set_layout: "layout",
  upsert_panel: "panel",
  patch_panel: "patch",
  append_data: "append",
  remove_panel: "panel −",
  reset_workspace: "reset",
  add_comment: "comment",
  resolve_comment: "resolved",
  edit_comment: "comment ✎",
  delete_comment: "comment −",
};

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  if (sameDay) return time;
  const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${date} ${time}`;
}

export default function HistoryDrawer({
  view,
  onClose,
}: {
  view: string | null;
  onClose: () => void;
}) {
  const [scope, setScope] = useState<"view" | "all">(view ? "view" : "all");
  const [includeArgs, setIncludeArgs] = useState(false);
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setEntries(null);
    const params = new URLSearchParams({ limit: "500" });
    if (scope === "view" && view) params.set("view", view);
    if (includeArgs) params.set("include_args", "true");
    try {
      const resp = await fetch(`/api/history?${params}`);
      if (!resp.ok) throw new Error(`server returned ${resp.status}`);
      const body = await resp.json();
      setEntries(body.history ?? []);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  }, [scope, view, includeArgs]);

  const restore = useCallback(
    async (entry: HistoryEntry) => {
      if (!entry.panel_id) return;
      setRestoring(entry.seq);
      setNotice(null);
      try {
        const resp = await fetch("/api/restore", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ panel_id: entry.panel_id, seq: entry.seq }),
        });
        const body = await resp.json();
        if (!resp.ok || !body.ok) throw new Error(body.error ?? `server returned ${resp.status}`);
        // The plot itself updates over the state channel; refresh the log so the
        // new restore entry shows at the top.
        setNotice(`Restored ${entry.panel_id} to its rev ${entry.rev ?? "?"} version.`);
        await load();
      } catch (err: any) {
        setNotice(`Restore failed: ${String(err?.message ?? err)}`);
      } finally {
        setRestoring(null);
      }
    },
    [load]
  );

  useEffect(() => {
    load();
  }, [load]);

  // Esc closes, matching the scrim click.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toggle = (seq: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(seq) ? next.delete(seq) : next.add(seq);
      return next;
    });

  // Newest first — the most recent change is what you usually came to see.
  const ordered = entries ? [...entries].reverse() : [];

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside
        className="history-drawer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Plot history"
      >
        <header className="history-head">
          <span className="history-title">History</span>
          <button className="ghost" onClick={onClose}>
            close
          </button>
        </header>

        <div className="history-controls">
          <div className="seg" role="group" aria-label="scope">
            <button
              className={scope === "view" ? "active" : ""}
              disabled={!view}
              onClick={() => setScope("view")}
              title={view ? `Only the ${view} tab` : "No view selected"}
            >
              {view ?? "this view"}
            </button>
            <button
              className={scope === "all" ? "active" : ""}
              onClick={() => setScope("all")}
            >
              all views
            </button>
          </div>
          <label className="history-check">
            <input
              type="checkbox"
              checked={includeArgs}
              onChange={(e) => setIncludeArgs(e.target.checked)}
            />
            specs
          </label>
          <button className="ghost" onClick={load}>
            refresh
          </button>
        </div>

        {notice && (
          <div className="history-notice" onClick={() => setNotice(null)}>
            {notice}
          </div>
        )}

        <div className="history-list">
          {error && <p className="history-empty">Couldn’t load history: {error}</p>}
          {!error && entries === null && <p className="history-empty">Loading…</p>}
          {!error && entries?.length === 0 && (
            <p className="history-empty">
              No history yet
              {scope === "view" && view ? ` for “${view}”` : ""}.
            </p>
          )}

          {ordered.map((e) => {
            const label = OP_LABEL[e.op] ?? e.op;
            const target = e.panel_id ?? e.view ?? "—";
            const open = expanded.has(e.seq);
            return (
              <div className="history-entry" key={e.seq}>
                <div className="history-row">
                  <span className={`op-badge op-${e.op}`}>{label}</span>
                  <span className="history-target" title={target}>
                    {target}
                  </span>
                  {e.rev != null && <span className="history-rev">rev {e.rev}</span>}
                  {scope === "all" && e.view && (
                    <span className="history-view">{e.view}</span>
                  )}
                  <span className="history-time">{formatTime(e.ts)}</span>
                  {e.restorable && e.panel_id && (
                    <button
                      className="history-restore"
                      disabled={restoring === e.seq}
                      onClick={() => restore(e)}
                      title={`Bring ${e.panel_id} back to this version`}
                    >
                      {restoring === e.seq ? "restoring…" : "restore"}
                    </button>
                  )}
                </div>

                {e.code && (
                  <pre className="history-code">
                    <code>{e.code}</code>
                  </pre>
                )}

                {includeArgs && e.args && (
                  <div className="history-args-wrap">
                    <button className="ghost history-detail" onClick={() => toggle(e.seq)}>
                      {open ? "hide spec" : "show spec"}
                    </button>
                    {open && (
                      <pre className="history-args">
                        <code>{JSON.stringify(e.args, null, 2)}</code>
                      </pre>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </aside>
    </div>
  );
}
