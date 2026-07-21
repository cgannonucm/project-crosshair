/** WebSocket client: holds server state, reconnects, and routes agent messages. */
import { useEffect, useRef, useState } from "react";
import type { LogEntry, WorkspaceState } from "./types";

const EMPTY: WorkspaceState = { active_view: null, views: [], panels: {} };

type AppendHandler = (panelId: string, trace: number, x: any[], y: any[]) => void;
type SnapshotHandler = (requestId: string, target: any) => void;

let socket: WebSocket | null = null;
const appendHandlers = new Set<AppendHandler>();
const snapshotHandlers = new Set<SnapshotHandler>();

export function send(msg: any) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
}

/** Report a human interaction back to the agent's event queue. */
export function sendEvent(kind: string, data: any, panelId?: string, view?: string | null) {
  send({ type: "event", kind, data, panel_id: panelId ?? null, view: view ?? null });
}

export function onAppend(fn: AppendHandler): () => void {
  appendHandlers.add(fn);
  return () => {
    appendHandlers.delete(fn);
  };
}

export function onSnapshotRequest(fn: SnapshotHandler): () => void {
  snapshotHandlers.add(fn);
  return () => {
    snapshotHandlers.delete(fn);
  };
}

export function useWorkspace() {
  const [state, setState] = useState<WorkspaceState>(EMPTY);
  const [connected, setConnected] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const logId = useRef(0);

  const appendLog = (from: "agent" | "you", text: string) =>
    setLog((prev) => [...prev.slice(-199), { id: ++logId.current, from, text, ts: Date.now() }]);

  useEffect(() => {
    let closed = false;
    let retry: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws`);
      socket = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 1000);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        switch (msg.type) {
          case "state":
            setState(msg.state);
            break;
          case "append":
            appendHandlers.forEach((h) => h(msg.panel_id, msg.trace, msg.x, msg.y));
            break;
          case "snapshot_request":
            snapshotHandlers.forEach((h) => h(msg.request_id, msg.target));
            break;
          case "agent_note":
            appendLog("agent", msg.text);
            break;
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { state, connected, log, appendLog };
}
