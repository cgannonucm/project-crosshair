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
}

export interface WorkspaceState {
  active_view: string | null;
  views: View[];
  panels: Record<string, Panel>;
}

export interface LogEntry {
  id: number;
  from: "agent" | "you";
  text: string;
  ts: number;
}
