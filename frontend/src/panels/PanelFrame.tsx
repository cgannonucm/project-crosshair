import { useState } from "react";
import type { Panel } from "../types";
import { sendEvent } from "../ws";
import PlotlyPanel from "./PlotlyPanel";

/** Minimal markdown: headings, bold, italic, inline code, and paragraphs. */
function renderMarkdown(text: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc(text)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>")
    .replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^[-*] (.*)$/gm, "<li>$1</li>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\n{2,}/g, "<br/><br/>");
}

export default function PanelFrame({ panel }: { panel: Panel }) {
  const [commenting, setCommenting] = useState(false);
  const [draft, setDraft] = useState("");

  const submit = () => {
    const text = draft.trim();
    if (text) sendEvent("comment", { text }, panel.id, panel.view);
    setDraft("");
    setCommenting(false);
  };

  return (
    <section className="panel" aria-label={panel.title || panel.id}>
      <header className="panel-head">
        <span className="panel-title">{panel.title || panel.id}</span>
        <button
          className="ghost"
          onClick={() => setCommenting((v) => !v)}
          title="Send a comment about this panel to the agent"
        >
          comment
        </button>
      </header>

      <div className="panel-body">
        {panel.type === "plotly" && <PlotlyPanel panel={panel} />}
        {panel.type === "markdown" && (
          <div
            className="markdown"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(panel.spec?.text ?? "") }}
          />
        )}
        {panel.type === "image" && (
          <div className="image-wrap">
            <img src={panel.spec?.src} alt={panel.title || panel.id} />
          </div>
        )}
      </div>

      {commenting && (
        <div className="comment-bar">
          <input
            autoFocus
            value={draft}
            placeholder={`Comment on "${panel.title || panel.id}"…`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
              if (e.key === "Escape") setCommenting(false);
            }}
          />
          <button onClick={submit}>send</button>
        </div>
      )}
    </section>
  );
}
