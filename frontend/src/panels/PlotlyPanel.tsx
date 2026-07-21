import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";
import type { Panel } from "../types";
import { hydrate } from "../hydrate";
import { onAppend, sendEvent } from "../ws";
import { mergeDefaults, plotlyLayoutDefaults, useChrome } from "../theme";

/** Registry so the snapshot handler can render any live plot to PNG. */
export const plotNodes = new Map<string, HTMLDivElement>();

const ZOOM_DEBOUNCE_MS = 400;

export default function PlotlyPanel({ panel }: { panel: Panel }) {
  const ref = useRef<HTMLDivElement>(null);
  const chrome = useChrome();
  const zoomTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    let disposed = false;

    (async () => {
      const spec = await hydrate(panel.spec);
      if (disposed || !ref.current) return;

      const layout = mergeDefaults(plotlyLayoutDefaults(chrome), spec.layout ?? {});
      const config = {
        responsive: true,
        displaylogo: false,
        // Selection tools are how the human hands point sets back to the agent.
        modeBarButtonsToAdd: ["select2d", "lasso2d"] as any,
        ...(spec.config ?? {}),
      };

      await Plotly.react(node, spec.data ?? [], layout, config);
      plotNodes.set(panel.id, node);

      const plot = node as any;
      plot.removeAllListeners?.("plotly_selected");
      plot.removeAllListeners?.("plotly_relayout");
      plot.removeAllListeners?.("plotly_click");

      plot.on("plotly_selected", (ev: any) => {
        if (!ev?.points) return;
        sendEvent(
          "selection",
          {
            count: ev.points.length,
            trace: ev.points[0]?.curveNumber ?? 0,
            indices: ev.points.map((p: any) => p.pointIndex ?? p.pointNumber),
            points: ev.points.slice(0, 500).map((p: any) => ({ x: p.x, y: p.y })),
            range: ev.range ?? null,
          },
          panel.id,
          panel.view
        );
      });

      plot.on("plotly_click", (ev: any) => {
        const p = ev?.points?.[0];
        if (!p) return;
        sendEvent(
          "click",
          { trace: p.curveNumber, index: p.pointIndex ?? p.pointNumber, x: p.x, y: p.y },
          panel.id,
          panel.view
        );
      });

      plot.on("plotly_relayout", (ev: any) => {
        const keys = Object.keys(ev ?? {});
        if (!keys.some((k) => k.includes("axis.range") || k.includes("autorange"))) return;
        window.clearTimeout(zoomTimer.current);
        zoomTimer.current = window.setTimeout(() => {
          sendEvent("zoom", ev, panel.id, panel.view);
        }, ZOOM_DEBOUNCE_MS);
      });
    })();

    return () => {
      disposed = true;
      window.clearTimeout(zoomTimer.current);
      plotNodes.delete(panel.id);
      if (node) Plotly.purge(node);
    };
  }, [panel.id, panel.spec, panel.view, chrome]);

  // Streaming appends bypass a full re-render so long-running plots stay smooth.
  useEffect(
    () =>
      onAppend((panelId, trace, x, y) => {
        if (panelId !== panel.id || !ref.current) return;
        Plotly.extendTraces(ref.current, { x: [x], y: [y] }, [trace]);
      }),
    [panel.id]
  );

  return <div className="plot" ref={ref} />;
}
