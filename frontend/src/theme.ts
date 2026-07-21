/** Validated categorical palette + chart chrome, applied as Plotly layout defaults.
 *  Slot order is the colorblind-safety mechanism — do not reorder or cycle. */

export const CATEGORICAL_LIGHT = [
  "#2a78d6", // blue
  "#eb6834", // orange
  "#1baf7a", // aqua
  "#eda100", // yellow
  "#e87ba4", // magenta
  "#008300", // green
  "#4a3aa7", // violet
  "#e34948", // red
];

export const CATEGORICAL_DARK = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#008300",
  "#9085e9",
  "#e66767",
];

export const SEQUENTIAL_BLUE: Array<[number, string]> = [
  [0, "#cde2fb"],
  [0.25, "#86b6ef"],
  [0.5, "#3987e5"],
  [0.75, "#256abf"],
  [1, "#0d366b"],
];

export interface Chrome {
  surface: string;
  plane: string;
  textPrimary: string;
  textSecondary: string;
  muted: string;
  grid: string;
  axis: string;
  border: string;
  categorical: string[];
}

export const LIGHT: Chrome = {
  surface: "#fcfcfb",
  plane: "#f9f9f7",
  textPrimary: "#0b0b0b",
  textSecondary: "#52514e",
  muted: "#898781",
  grid: "#e1e0d9",
  axis: "#c3c2b7",
  border: "rgba(11,11,11,0.10)",
  categorical: CATEGORICAL_LIGHT,
};

export const DARK: Chrome = {
  surface: "#1a1a19",
  plane: "#0d0d0d",
  textPrimary: "#ffffff",
  textSecondary: "#c3c2b7",
  muted: "#898781",
  grid: "#2c2c2a",
  axis: "#383835",
  border: "rgba(255,255,255,0.10)",
  categorical: CATEGORICAL_DARK,
};

const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

/** Chart chrome the agent's spec can still override — recessive grid, muted axes. */
export function plotlyLayoutDefaults(c: Chrome) {
  const axis = {
    gridcolor: c.grid,
    zerolinecolor: c.axis,
    linecolor: c.axis,
    tickcolor: c.axis,
    tickfont: { color: c.muted, size: 11 },
    title: { font: { color: c.textSecondary, size: 12 } },
    automargin: true,
  };
  return {
    paper_bgcolor: c.surface,
    plot_bgcolor: c.surface,
    colorway: c.categorical,
    colorscale: { sequential: SEQUENTIAL_BLUE },
    font: { family: FONT, color: c.textSecondary, size: 12 },
    margin: { l: 56, r: 16, t: 16, b: 44 },
    xaxis: axis,
    yaxis: axis,
    legend: {
      font: { color: c.textSecondary, size: 11 },
      bgcolor: "rgba(0,0,0,0)",
      orientation: "h" as const,
      y: -0.18,
      x: 0,
    },
    hoverlabel: { font: { family: FONT, size: 12 } },
  };
}

/** Deep merge where the agent's spec always wins over our defaults. */
export function mergeDefaults(defaults: any, override: any): any {
  if (override === undefined || override === null) return defaults;
  if (typeof override !== "object" || Array.isArray(override)) return override;
  if (typeof defaults !== "object" || defaults === null || Array.isArray(defaults)) return override;
  const out: any = { ...defaults };
  for (const key of Object.keys(override)) {
    out[key] = mergeDefaults(defaults[key], override[key]);
  }
  return out;
}

export function useChrome(): Chrome {
  const root = document.documentElement.getAttribute("data-theme");
  if (root === "dark") return DARK;
  if (root === "light") return LIGHT;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? DARK : LIGHT;
}
