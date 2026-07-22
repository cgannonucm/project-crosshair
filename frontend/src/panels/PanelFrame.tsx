import { useState } from "react";
import type { Anchor, Comment, Panel } from "../types";
import { addComment } from "../ws";
import CommentLayer from "./CommentLayer";
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

export default function PanelFrame({ panel, comments }: { panel: Panel; comments: Comment[] }) {
  // "arming" = waiting for the human to drag out a region on the plot.
  const [arming, setArming] = useState(false);
  // undefined = not composing; null = composing a whole-panel comment.
  const [draft, setDraft] = useState<Anchor | null | undefined>(undefined);

  const anchorable = panel.type === "plotly";

  const startComment = () => {
    if (draft !== undefined) return setDraft(undefined);
    if (anchorable) setArming((v) => !v);
    else setDraft(null);
  };

  const submit = (text: string) => {
    addComment(panel.id, text, draft ?? null);
    setDraft(undefined);
  };

  return (
    <section className="panel" aria-label={panel.title || panel.id}>
      <header className="panel-head">
        <span className="panel-title">{panel.title || panel.id}</span>
        {comments.length > 0 && (
          <span className="comment-count" title={`${comments.length} open comment(s)`}>
            {comments.length}
          </span>
        )}
        <button
          className={arming ? "ghost active" : "ghost"}
          onClick={startComment}
          title={
            anchorable
              ? "Select a region of the plot to comment on"
              : "Add a comment to this panel"
          }
        >
          {arming ? "select a region…" : "comment"}
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

        <CommentLayer
          panelId={panel.id}
          comments={comments}
          capturing={arming}
          onRegion={(anchor) => {
            setArming(false);
            setDraft(anchor);
          }}
          onCancelCapture={() => setArming(false)}
          draftAnchor={draft}
          onSubmitDraft={submit}
          onCancelDraft={() => setDraft(undefined)}
        />
      </div>
    </section>
  );
}
