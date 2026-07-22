/** Word-style margin comments pinned to a region of a panel.
 *
 * Anchors are stored in data coordinates, so a pin stays on the data it refers
 * to as the human zooms and pans; this layer projects them back to pixels using
 * the live Plotly axes. Only the numbered pin is drawn — the text appears when
 * the pin is selected.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Anchor, Comment } from "../types";
import { deleteComment, editComment, resolveComment } from "../ws";
import { onPlotLayout, plotNodes } from "./PlotlyPanel";

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface Size {
  w: number;
  h: number;
}

/** Track an element's own box, so panel-space anchors survive a resize. */
function useElementSize<T extends HTMLElement>(): [React.RefObject<T>, Size | null] {
  const ref = useRef<T>(null!);
  const [size, setSize] = useState<Size | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

/** The live drawing box of a panel's axes, in pixels relative to the plot div.
 *
 *  Anchored to each axis's own `_offset`/`_length` rather than the figure's
 *  margins: an aspect-locked axis (an image, `scaleanchor`, `constrain:"domain"`)
 *  occupies only part of the plot area and re-centres itself as the view zooms,
 *  so margins would put a pin hundreds of pixels adrift and drifting further
 *  with every zoom step.
 */
function axesOf(node: HTMLDivElement | undefined) {
  const fl = (node as any)?._fullLayout;
  const xa = fl?.xaxis;
  const ya = fl?.yaxis;
  if (typeof xa?.d2p !== "function" || typeof ya?.d2p !== "function") return null;
  if (typeof xa._offset !== "number" || typeof ya._offset !== "number") return null;
  // Panel bounds, not just the drawing area: comments may legitimately sit in the
  // margins, on the tick labels or an axis title.
  const pw = node?.clientWidth || fl.width || xa._offset + xa._length;
  const ph = node?.clientHeight || fl.height || ya._offset + ya._length;
  return { xa, ya, l: xa._offset, t: ya._offset, w: xa._length, h: ya._length, pw, ph };
}

/** Project an anchor onto panel pixels. Null if it cannot be placed on screen. */
function project(node: HTMLDivElement | undefined, anchor: Anchor, size: Size | null): Box | null {
  // Panel-space anchors are fractions of the panel box and need no axes at all,
  // which is what lets them sit on the chrome — or on a markdown panel.
  if (anchor.space === "panel") {
    if (!size) return null;
    const nums = [anchor.x0, anchor.x1, anchor.y0, anchor.y1];
    if (nums.some((v) => typeof v !== "number" || !Number.isFinite(v))) return null;
    const [fx0, fx1] = [Number(anchor.x0), Number(anchor.x1)].sort((a, b) => a - b);
    const [fy0, fy1] = [Number(anchor.y0), Number(anchor.y1)].sort((a, b) => a - b);
    return {
      left: fx0 * size.w,
      top: fy0 * size.h,
      width: Math.max((fx1 - fx0) * size.w, 2),
      height: Math.max((fy1 - fy0) * size.h, 2),
    };
  }

  const ax = axesOf(node);
  if (!ax) return null;

  const { xa, ya, l, t, pw, ph } = ax;
  // Sorting the projected pixels handles reversed axes (image y runs downward).
  // Axis pixels are relative to the axis origin, so shift them into panel space.
  const xs = [l + xa.d2p(anchor.x0), l + xa.d2p(anchor.x1)].sort((a, b) => a - b);
  const ys = [t + ya.d2p(anchor.y0), t + ya.d2p(anchor.y1)].sort((a, b) => a - b);
  if (xs.some(Number.isNaN) || ys.some(Number.isNaN)) return null;

  // Cull anchors scrolled entirely outside the panel — the margins still count,
  // so a region drawn over the tick labels stays where it was drawn.
  if (xs[1] < 0 || xs[0] > pw || ys[1] < 0 || ys[0] > ph) return null;

  const clampX = (v: number) => Math.max(0, Math.min(pw, v));
  const clampY = (v: number) => Math.max(0, Math.min(ph, v));
  const x0 = clampX(xs[0]);
  const x1 = clampX(xs[1]);
  const y0 = clampY(ys[0]);
  const y1 = clampY(ys[1]);
  return { left: x0, top: y0, width: Math.max(x1 - x0, 2), height: Math.max(y1 - y0, 2) };
}

/** Does a drawn box touch the axes' drawing area at all? */
function overlapsAxes(ax: NonNullable<ReturnType<typeof axesOf>>, box: Box): boolean {
  return (
    box.left < ax.l + ax.w &&
    box.left + box.width > ax.l &&
    box.top < ax.t + ax.h &&
    box.top + box.height > ax.t
  );
}

/** A pixel box as a fraction of the panel box. */
function panelAnchor(box: Box, size: Size | null): Anchor | null {
  if (!size || size.w <= 0 || size.h <= 0) return null;
  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  return {
    x0: clamp(box.left / size.w),
    x1: clamp((box.left + box.width) / size.w),
    y0: clamp(box.top / size.h),
    y1: clamp((box.top + box.height) / size.h),
    space: "panel",
  };
}

/** Invert `project` — a pixel box back into an anchor.
 *
 *  A box that touches the plotting area is about the data, so it is stored in
 *  data coordinates and tracks zoom. A box entirely in the margins is about the
 *  chrome — a tick label, an axis title — which has no data coordinates and
 *  should not wander when the human zooms, so it is stored against the panel.
 *
 *  Nothing here touches Plotly's own state: driving its `dragmode` re-lays out
 *  the whole figure, which corrupts plots carrying frames, sliders, or menus.
 */
function unproject(node: HTMLDivElement | undefined, box: Box, size: Size | null): Anchor | null {
  const ax = axesOf(node);
  if (!ax || typeof ax.xa.p2d !== "function" || typeof ax.ya.p2d !== "function") {
    return panelAnchor(box, size);
  }
  if (!overlapsAxes(ax, box)) return panelAnchor(box, size);

  const { xa, ya, l, t } = ax;
  // Deliberately unclamped: `p2d` extrapolates past the axis ends, so a region
  // drawn over the tick labels or in a margin keeps its real width instead of
  // collapsing onto the edge of the drawing area.
  const xs = [xa.p2d(box.left - l), xa.p2d(box.left + box.width - l)];
  const ys = [ya.p2d(box.top - t), ya.p2d(box.top + box.height - t)];
  // Date and category axes hand back strings, which are legitimate anchors.
  const bad = (v: any) => typeof v === "number" && !Number.isFinite(v);
  if ([...xs, ...ys].some((v) => v == null || bad(v))) return panelAnchor(box, size);
  return { x0: xs[0], x1: xs[1], y0: ys[0], y1: ys[1], space: "data" };
}

/** Re-render whenever the panel's axes move. */
function useLayoutTick(panelId: string): number {
  const [tick, setTick] = useState(0);
  useEffect(() => onPlotLayout(panelId, () => setTick((t) => t + 1)), [panelId]);
  return tick;
}

const MIN_DRAG_PX = 4;

/** Rubber-band region picker, drawn over the plot without disturbing it. */
function RegionCapture({ onPick, onCancel }: { onPick: (box: Box) => void; onCancel: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const [box, setBox] = useState<Box | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const local = (e: React.MouseEvent) => {
    const r = ref.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const boxFrom = (a: { x: number; y: number }, b: { x: number; y: number }): Box => ({
    left: Math.min(a.x, b.x),
    top: Math.min(a.y, b.y),
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  });

  return (
    <div
      ref={ref}
      className="region-capture"
      onMouseDown={(e) => {
        e.preventDefault();
        start.current = local(e);
        setBox(null);
      }}
      onMouseMove={(e) => {
        if (start.current) setBox(boxFrom(start.current, local(e)));
      }}
      onMouseUp={(e) => {
        const from = start.current;
        start.current = null;
        if (!from) return;
        const drawn = boxFrom(from, local(e));
        // A stray click is not a region.
        if (drawn.width < MIN_DRAG_PX || drawn.height < MIN_DRAG_PX) {
          setBox(null);
          return;
        }
        onPick(drawn);
      }}
    >
      {box && (
        <div
          className="comment-region draft"
          style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
        />
      )}
    </div>
  );
}

export default function CommentLayer({
  panelId,
  comments,
  capturing = false,
  onRegion,
  onCancelCapture,
  draftAnchor,
  onSubmitDraft,
  onCancelDraft,
}: {
  panelId: string;
  comments: Comment[];
  /** True while the human is dragging out a region to comment on. */
  capturing?: boolean;
  onRegion?: (anchor: Anchor) => void;
  onCancelCapture?: () => void;
  /** Set while the human is composing; `null` composes a whole-panel comment. */
  draftAnchor?: Anchor | null;
  onSubmitDraft: (text: string) => void;
  onCancelDraft: () => void;
}) {
  const tick = useLayoutTick(panelId);
  const [layerRef, size] = useElementSize<HTMLDivElement>();
  const [openId, setOpenId] = useState<string | null>(null);
  const composing = draftAnchor !== undefined;

  // A comment resolved elsewhere should not leave an orphaned popover open.
  useEffect(() => {
    if (openId && !comments.some((c) => c.id === openId)) setOpenId(null);
  }, [comments, openId]);

  const node = plotNodes.get(panelId);
  void tick; // projections below are recomputed on every layout tick

  // Anchored comments that project onto the visible area get a pin in place;
  // the rest fall back to a stack in the corner, so nothing is unreachable.
  const pinned: Array<{ comment: Comment; index: number; box: Box }> = [];
  const stacked: Array<{ comment: Comment; index: number }> = [];
  comments.forEach((comment, i) => {
    const box = comment.anchor ? project(node, comment.anchor, size) : null;
    if (box) pinned.push({ comment, index: i + 1, box });
    else stacked.push({ comment, index: i + 1 });
  });

  const draftBox = draftAnchor ? project(node, draftAnchor, size) : null;

  return (
    <div className="comment-layer" ref={layerRef}>
      {capturing && (
        <RegionCapture
          onPick={(box) => {
            const anchor = unproject(node, box, size);
            if (anchor) onRegion?.(anchor);
            else onCancelCapture?.();
          }}
          onCancel={() => onCancelCapture?.()}
        />
      )}

      {pinned.map(({ comment, index, box }) => (
        <div key={comment.id} className="comment-anchor">
          <div
            className={openId === comment.id ? "comment-region open" : "comment-region"}
            style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
          />
          <Pin
            comment={comment}
            index={index}
            style={{ left: box.left + box.width, top: box.top }}
            open={openId === comment.id}
            onToggle={() => setOpenId((id) => (id === comment.id ? null : comment.id))}
            onClose={() => setOpenId(null)}
          />
        </div>
      ))}

      {stacked.length > 0 && (
        <div className="comment-stack">
          {stacked.map(({ comment, index }) => (
            <Pin
              key={comment.id}
              comment={comment}
              index={index}
              open={openId === comment.id}
              onToggle={() => setOpenId((id) => (id === comment.id ? null : comment.id))}
              onClose={() => setOpenId(null)}
            />
          ))}
        </div>
      )}

      {composing && (
        <>
          {draftBox && (
            <div
              className="comment-region draft"
              style={{
                left: draftBox.left,
                top: draftBox.top,
                width: draftBox.width,
                height: draftBox.height,
              }}
            />
          )}
          <Composer
            style={
              draftBox ? { left: draftBox.left + draftBox.width, top: draftBox.top } : undefined
            }
            onSubmit={onSubmitDraft}
            onCancel={onCancelDraft}
          />
        </>
      )}
    </div>
  );
}

function Pin({
  comment,
  index,
  style,
  open,
  onToggle,
  onClose,
}: {
  comment: Comment;
  index: number;
  style?: React.CSSProperties;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
}) {
  return (
    <div className="comment-pin-wrap" style={style}>
      <button
        className={`comment-pin ${comment.author}${open ? " open" : ""}`}
        onClick={onToggle}
        title={`${comment.author === "agent" ? "Agent" : "Your"} comment — click to read`}
      >
        {index}
      </button>
      {open && <Popover comment={comment} onClose={onClose} />}
    </div>
  );
}

function Popover({ comment, onClose }: { comment: Comment; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;

  // An edit landing from elsewhere wins over an untouched draft of the same text.
  useEffect(() => {
    setDraft((d) => (d === null || d === comment.text ? null : d));
  }, [comment.text]);

  const save = () => {
    const text = (draft ?? "").trim();
    if (!text || text === comment.text) return setDraft(null);
    editComment(comment.id, text);
    setDraft(null);
  };

  // Flip back inside the panel when the pin sits near an edge.
  useLayoutEffect(() => {
    const el = ref.current;
    const panel = el?.closest(".panel-body");
    if (!el || !panel) return;
    const box = el.getBoundingClientRect();
    const bounds = panel.getBoundingClientRect();
    if (box.right > bounds.right) el.classList.add("flip-x");
    if (box.bottom > bounds.bottom) el.classList.add("flip-y");
  }, []);

  return (
    <div className="comment-popover" ref={ref} role="dialog">
      <header>
        <span className="who">{comment.author === "agent" ? "agent" : "you"}</span>
        <time>
          {new Date(comment.ts * 1000).toLocaleTimeString()}
          {comment.edited_ts ? " · edited" : ""}
        </time>
      </header>
      {editing ? (
        <textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setDraft(null);
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              save();
            }
          }}
        />
      ) : (
        <p>{comment.text}</p>
      )}
      <footer>
        {editing ? (
          <>
            <button className="ghost" onClick={() => setDraft(null)}>
              cancel
            </button>
            <button disabled={!draft.trim()} onClick={save}>
              save
            </button>
          </>
        ) : (
          <>
            <button className="ghost" onClick={() => setDraft(comment.text)}>
              edit
            </button>
            <button className="ghost" onClick={() => resolveComment(comment.id, true)}>
              resolve
            </button>
            <button className="ghost" onClick={() => deleteComment(comment.id)}>
              delete
            </button>
            <button className="ghost" onClick={onClose}>
              close
            </button>
          </>
        )}
      </footer>
    </div>
  );
}

function Composer({
  style,
  onSubmit,
  onCancel,
}: {
  style?: React.CSSProperties;
  onSubmit: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  return (
    <div className={style ? "comment-popover composer" : "comment-popover composer stacked"} style={style}>
      <textarea
        autoFocus
        value={text}
        placeholder="Comment on this region…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onCancel();
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            if (text.trim()) onSubmit(text.trim());
          }
        }}
      />
      <footer>
        <button className="ghost" onClick={onCancel}>
          cancel
        </button>
        <button disabled={!text.trim()} onClick={() => onSubmit(text.trim())}>
          comment
        </button>
      </footer>
    </div>
  );
}
