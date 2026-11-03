// Optimisation page (H3): run a study on the server and show its Pareto set. Display only:
// every number comes from the server's verified evaluations.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const SVG = "http://www.w3.org/2000/svg";
  const make = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    node.append(...children);
    return node;
  };
  const svg = (tag, attrs = {}, text) => {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmt = (values) => Object.entries(values || {}).map(([k, v]) => `${k} ${Number(v).toPrecision(4)}`).join(", ");
  const DEFAULT = {
    title: "Bend radius and pump position",
    variables: [
      { kind: "bend_radius_factor", line: "main", low: 1.5, high: 2.5, step: 0.5 },
      { kind: "equipment_position", equipment: "auxiliary-pump", axis: "y", low: -20, high: 20, step: 20 },
    ],
    objectives: ["length", "bends", "cost"],
    constraints: { max_bends: 6 },
    strategy: "grid", seed: 7, budget: 9,
  };

  function chart(report) {
    const [a, b] = report.objectives;
    const points = report.pareto.map((p) => ({ id: p.candidate, x: p.objectives[a], y: b ? p.objectives[b] : 0 }));
    if (!points.length) return make("p", { class: "muted" }, "No feasible design: nothing to plot.");
    const w = 420, h = 260, pad = 44;
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    const sx = (x) => pad + ((x - x0) / ((x1 - x0) || 1)) * (w - 2 * pad);
    const sy = (y) => h - pad - ((y - y0) / ((y1 - y0) || 1)) * (h - 2 * pad);
    const root = svg("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", role: "img", "aria-label": `Pareto set: ${a} against ${b || a}` });
    root.append(svg("line", { x1: pad, y1: h - pad, x2: w - pad, y2: h - pad, stroke: "currentColor", "stroke-opacity": ".4" }));
    root.append(svg("line", { x1: pad, y1: pad, x2: pad, y2: h - pad, stroke: "currentColor", "stroke-opacity": ".4" }));
    root.append(svg("text", { x: w / 2, y: h - 8, "text-anchor": "middle", "font-size": "12", fill: "currentColor" }, a));
    if (b) root.append(svg("text", { x: 12, y: h / 2, "font-size": "12", fill: "currentColor", transform: `rotate(-90 12 ${h / 2})`, "text-anchor": "middle" }, b));
    for (const p of points) {
      const chosen = report.chosen && p.id === report.chosen.id;
      const dot = svg("circle", { cx: sx(p.x), cy: sy(p.y), r: chosen ? 7 : 5, fill: chosen ? "var(--accent, #1f6feb)" : "currentColor", "fill-opacity": chosen ? "1" : ".55" });
      dot.append(svg("title", {}, `${p.id}: ${a} ${p.x}${b ? `, ${b} ${p.y}` : ""}${chosen ? " (chosen)" : ""}`));
      root.append(dot);
    }
    return root;
  }

  function render(report) {
    $("summary").textContent = `${report.evaluated} candidates evaluated (${report.strategy}, seed ${report.seed}): ${report.feasible} passed every check and limit, ${report.rejected} rejected.` +
      (report.chosen ? ` Chosen: ${report.chosen.id} (${fmt(report.chosen.values)}).` : " No design passed.");
    $("chart").replaceChildren(chart(report));
    const head = make("tr", {}, make("th", {}, "Candidate"), make("th", {}, "Values"), ...report.objectives.map((o) => make("th", {}, `${o} (Δ vs chosen)`)));
    const rows = report.pareto.map((p) => make("tr", {}, make("td", {}, p.candidate), make("td", {}, fmt(p.values)),
      ...report.objectives.map((o) => make("td", {}, `${p.objectives[o]} (${p.deltas[o] >= 0 ? "+" : ""}${p.deltas[o]})`))));
    $("table").replaceChildren(make("table", {}, make("thead", {}, head), make("tbody", {}, ...rows)));
    $("sensitivity").replaceChildren(...report.sensitivity.map((s) => make("li", {},
      `${s.variable} ${s.from_value} → ${s.to_value}: ${s.feasible ? fmt(s.deltas) : `rejected (${s.reasons.join("; ")})`}`)));
  }

  async function runStudy() {
    let study;
    try { study = JSON.parse($("study").value); } catch (error) { $("status").textContent = `Not valid JSON: ${error.message}`; return; }
    $("run").disabled = true;
    $("status").textContent = "Routing and verifying every candidate…";
    try {
      const response = await fetch("/api/optimise", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(study) });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
      render(body);
      $("status").textContent = "Done.";
    } catch (error) {
      $("status").textContent = error.message;
    } finally {
      $("run").disabled = false;
    }
  }

  $("study").value = JSON.stringify(DEFAULT, null, 2);
  $("run").addEventListener("click", runStudy);
  fetch("/api/optimise").then((r) => (r.ok ? r.json() : null)).then((report) => { if (report) render(report); }).catch(() => {});
})();
