/** WebSocket client: holds server state, reconnects, and routes agent messages. */
import { useEffect, useState } from "react";
import type { Anchor, WorkspaceState } from "./types";

const EMPTY: WorkspaceState = { active_view: null, views: [], panels: {}, comments: [] };

type AppendHandler = (panelId: string, trace: number, x: any[], y: any[]) => void;
type SnapshotHandler = (requestId: string, target: any) => void;
type ErrorHandler = (text: string) => void;

let socket: WebSocket | null = null;
const appendHandlers = new Set<AppendHandler>();
const snapshotHandlers = new Set<SnapshotHandler>();
const errorHandlers = new Set<ErrorHandler>();

export function send(msg: any) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
}

/** Report a human interaction back to the agent's event queue. */
export function sendEvent(kind: string, data: any, panelId?: string, view?: string | null) {
  send({ type: "event", kind, data, panel_id: panelId ?? null, view: view ?? null });
}

/** Pin a comment to a panel. `anchor` is a data-coordinate box, or null for the whole panel. */
export function addComment(panelId: string, text: string, anchor: Anchor | null) {
  send({ type: "add_comment", args: { panel_id: panelId, text, ...(anchor ?? {}) } });
}

export function resolveComment(commentId: string, resolved: boolean) {
  send({ type: "resolve_comment", args: { comment_id: commentId, resolved } });
}

/** Rewrite a comment's text in place; its pin, anchor, and number are unchanged. */
export function editComment(commentId: string, text: string) {
  send({ type: "edit_comment", args: { comment_id: commentId, text } });
}

export function deleteComment(commentId: string) {
  send({ type: "delete_comment", args: { comment_id: commentId } });
}

export function onAppend(fn: AppendHandler): () => void {
  appendHandlers.add(fn);
  return () => {
    appendHandlers.delete(fn);
  };
}

/** Server-side rejections (bad request, or a daemon older than this page). */
export function onClientError(fn: ErrorHandler): () => void {
  errorHandlers.add(fn);
  return () => {
    errorHandlers.delete(fn);
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
          // Comments arrive on their own channel so adding a pin never waits on
          // a full workspace round trip.
          case "comments":
            setState((prev) => ({ ...prev, comments: msg.comments }));
            break;
          case "append":
            appendHandlers.forEach((h) => h(msg.panel_id, msg.trace, msg.x, msg.y));
            break;
          case "snapshot_request":
            snapshotHandlers.forEach((h) => h(msg.request_id, msg.target));
            break;
          case "client_error":
            console.error("[crosshair]", msg.text);
            errorHandlers.forEach((h) => h(msg.text));
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

  return { state, connected };
}
