export interface Placement {
  panel_id: string;
  row: number;
  col: number;
  row_span: number;
  col_span: number;
}

export interface View {
  name: string;
  rows: number;
  cols: number;
  placements: Placement[];
}

export interface Panel {
  id: string;
  view: string;
  title: string;
  type: "plotly" | "markdown" | "image";
  spec: any;
  /** Bumped by the server whenever `spec` changes; drives re-plotting. */
  rev: number;
  /** The code the agent said produced this plot, when it supplied any. */
  code?: string | null;
}

/** A rectangle in data coordinates — comments track the data, not the pixels.
 *  Values are whatever the axis speaks: numbers, date strings, category names. */
/** A region a comment is pinned to.
 *
 *  "data" corners are axis coordinates and track the data under zoom; "panel"
 *  corners are fractions of the panel box (x rightward, y downward from the
 *  top-left) and stay put, for comments on the chrome rather than the data.
 *  Absent means "data" — anchors predate the discriminator.
 */
export interface Anchor {
  x0: number | string;
  x1: number | string;
  y0: number | string;
  y1: number | string;
  space?: "data" | "panel";
}

export interface Comment {
  id: string;
  panel_id: string;
  view: string;
  author: "agent" | "human";
  text: string;
  ts: number;
  anchor: Anchor | null;
  resolved: boolean;
  /** When the text was last revised; absent on comments never edited. */
  edited_ts?: number | null;
}

export interface WorkspaceState {
  active_view: string | null;
  views: View[];
  panels: Record<string, Panel>;
  comments: Comment[];
}

/** One entry in the on-disk mutation log, as served by GET /api/history. */
export interface HistoryEntry {
  seq: number;
  ts: number;
  op: string;
  view: string | null;
  panel_id: string | null;
  rev: number | null;
  /** The code the agent attached to the plot, when it supplied any. */
  code: string | null;
  /** True when this entry holds a panel snapshot that restore_panel can bring back. */
  restorable?: boolean;
  /** Full call arguments — present only when the fetch asked for them. */
  args?: Record<string, any>;
}
