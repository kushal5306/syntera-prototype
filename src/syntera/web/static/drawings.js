// Drawings page: upload DWG/DXF, view the server-rendered SVG with pan and zoom, list what was read.
"use strict";

const MARGIN_MM = 4; // must match layout.Margins in syntera.drawings.reader.render_svg
const $ = (id) => document.getElementById(id);
const state = { summary: null, scale: 1, x: 0, y: 0, mark: null, mode: "2d", check: null, options: {} };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const fmt = (value, digits = 2) =>
  value === null || value === undefined ? "–" : Number(value).toLocaleString("en-GB", { maximumFractionDigits: digits });

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ") : body.detail;
    throw new Error(detail || `request failed (${response.status})`);
  }
  return body;
}

// ---- drawing coordinates <-> page millimetres (mirrors ezdxf fit-to-page, centred) ----
function pageTransform() {
  const s = state.summary;
  if (!s || !s.view_extents) return null;
  const [[minX, minY], [maxX, maxY]] = s.view_extents;
  const [pw, ph] = s.svg_page_mm;
  const w = Math.max(maxX - minX, 1e-9), h = Math.max(maxY - minY, 1e-9);
  const k = Math.min((pw - 2 * MARGIN_MM) / w, (ph - 2 * MARGIN_MM) / h);
  const ox = MARGIN_MM + (pw - 2 * MARGIN_MM - w * k) / 2;
  const oy = MARGIN_MM + (ph - 2 * MARGIN_MM - h * k) / 2;
  return {
    toPage: (x, y) => [ox + (x - minX) * k, oy + (maxY - y) * k],
    toDrawing: (px, py) => [minX + (px - ox) / k, maxY - (py - oy) / k],
  };
}

// ---- view ----
function applyView() {
  const s = state.summary;
  if (!s) return;
  const stage = $("stage");
  const [pw, ph] = s.svg_page_mm;
  stage.style.width = `${pw * state.scale}px`;
  stage.style.height = `${ph * state.scale}px`;
  stage.style.transform = `translate(${state.x}px, ${state.y}px)`;
  const pins = $("sheet-pins");
  pins.replaceChildren();
  const t = pageTransform();
  if (t && state.mark) {
    const [px, py] = t.toPage(...state.mark);
    pins.append(el("div", { class: "mark", style: `left:${px * state.scale}px;top:${py * state.scale}px` }));
  }
}

function fit() {
  const s = state.summary;
  if (!s) return;
  const box = $("sheet").getBoundingClientRect();
  const [pw, ph] = s.svg_page_mm;
  state.scale = Math.min(box.width / pw, box.height / ph) * 0.94;
  state.x = (box.width - pw * state.scale) / 2;
  state.y = (box.height - ph * state.scale) / 2;
  applyView();
}

function zoomAt(factor, cx, cy) {
  const next = Math.min(Math.max(state.scale * factor, 0.02), 400);
  const ratio = next / state.scale;
  state.x = cx - (cx - state.x) * ratio;
  state.y = cy - (cy - state.y) * ratio;
  state.scale = next;
  applyView();
}

function focusOn(x, y) {
  const t = pageTransform();
  if (!t) return;
  state.mark = [x, y];
  const box = $("sheet").getBoundingClientRect();
  const [pw, ph] = state.summary.svg_page_mm;
  state.scale = Math.max(state.scale, (Math.min(box.width / pw, box.height / ph) * 0.94) * 4);
  const [px, py] = t.toPage(x, y);
  state.x = box.width / 2 - px * state.scale;
  state.y = box.height / 2 - py * state.scale;
  applyView();
}

function bindSheet() {
  const sheet = $("sheet");
  let drag = null;
  sheet.addEventListener("wheel", (event) => {
    if (!state.summary || state.mode === "3d") return;
    event.preventDefault();
    const box = sheet.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });
  sheet.addEventListener("pointerdown", (event) => {
    if (!state.summary || state.mode === "3d" || event.target.closest(".vp-tools")) return;
    drag = { x: event.clientX - state.x, y: event.clientY - state.y };
    sheet.setPointerCapture(event.pointerId);
    sheet.classList.add("panning");
  });
  sheet.addEventListener("pointermove", (event) => {
    const box = sheet.getBoundingClientRect();
    const t = pageTransform();
    if (t) {
      const [x, y] = t.toDrawing((event.clientX - box.left - state.x) / state.scale, (event.clientY - box.top - state.y) / state.scale);
      $("cursor").textContent = `X ${fmt(x)}  Y ${fmt(y)}`;
    }
    if (!drag) return;
    state.x = event.clientX - drag.x;
    state.y = event.clientY - drag.y;
    applyView();
  });
  const end = () => { drag = null; sheet.classList.remove("panning"); };
  sheet.addEventListener("pointerup", end);
  sheet.addEventListener("pointercancel", end);
  $("zoom-in").addEventListener("click", () => { if (state.mode === "3d") return model.zoom(1 / 1.3); const b = sheet.getBoundingClientRect(); zoomAt(1.5, b.width / 2, b.height / 2); });
  $("zoom-out").addEventListener("click", () => { if (state.mode === "3d") return model.zoom(1.3); const b = sheet.getBoundingClientRect(); zoomAt(1 / 1.5, b.width / 2, b.height / 2); });
  $("zoom-fit").addEventListener("click", () => { if (state.mode === "3d") { model.fit(); return; } state.mark = null; fit(); });
  $("mode-2d").addEventListener("click", () => setMode("2d"));
  $("mode-3d").addEventListener("click", () => setMode("3d"));
  window.addEventListener("resize", fit);

  // drag and drop upload
  ["dragenter", "dragover"].forEach((type) => sheet.addEventListener(type, (event) => { event.preventDefault(); sheet.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((type) => sheet.addEventListener(type, () => sheet.classList.remove("dragover")));
  sheet.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) upload(file);
  });
}

// ---- details panel ----
function kv(pairs) {
  return el("dl", { class: "kv" }, pairs.flatMap(([key, value]) => [el("dt", {}, key), el("dd", {}, value)]));
}

function section(title, aside, ...body) {
  return el("section", {}, el("div", { class: "section-title" }, el("h3", {}, title), aside ? el("span", { class: "note" }, aside) : null), ...body);
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map(([label, cls]) => el("th", { class: cls || "" }, label)))),
    el("tbody", {}, rows)));
}

function fold(title, aside, ...body) {
  return el("details", { class: "fold" }, el("summary", {}, el("span", {}, title), aside ? el("span", { class: "note" }, aside) : null), ...body);
}

function renderDetails(s) {
  const units = s.units.mm_per_unit === 1 ? el("span", { class: "chip pass" }, "mm")
    : el("span", { class: "chip warn" }, s.units.name);
  const box = s.view_extents;
  const extentText = box ? `${fmt(box[1][0] - box[0][0])} × ${fmt(box[1][1] - box[0][1])}` : "empty";
  const parts = [
    el("div", { class: "file-line" }, el("strong", { title: s.filename }, s.filename), units),
  ];
  const cautions = [];
  if (s.units.mm_per_unit === null) cautions.push("The drawing does not declare units ($INSUNITS). Confirm the scale before using any dimension.");
  if (s.clipped_handles.length) cautions.push(`${s.clipped_handles.length} entity(ies) far outside the drawing were left out of the default view: ${s.clipped_handles.join(", ")}.`);
  if (s.audit_errors) cautions.push(`The DXF needed ${s.audit_errors} repair(s) while reading; compare against the original before relying on it.`);
  cautions.forEach((text) => parts.push(el("div", { class: "reason soft" }, text)));
  parts.push(checkSection(s));
  const read = [
    kv([
      ["Format", s.source_format.toUpperCase()],
      ["DWG release", s.dwg_version || "–"],
      ["Converter", s.conversion ? `${s.conversion.converter}${s.conversion.converter_version ? ` (${s.conversion.converter_version})` : ""}` : "none (DXF read directly)"],
      ["Shown", !s.space || s.space === "Model" ? "Model space" : `Paper space: ${s.space} (model space draws nothing)`],
      ["Drawing size", extentText],
      ["Entities", fmt(s.entity_count, 0)],
      ["SHA-256", s.source_sha256.slice(0, 16) + "…"],
    ]),
    el("div", { class: "row wrap" },
      el("a", { class: "btn", href: `/api/drawings/${s.drawing_id}/drawing.dxf`, download: `${s.filename.replace(/\.[^.]+$/, "")}.dxf` }, "Download DXF"),
      el("a", { class: "btn", href: `/api/drawings/${s.drawing_id}/summary.json`, download: "summary.json" }, "Summary JSON")),
  ];
  if (s.truncated) read.push(el("p", { class: "note" }, "Only the first 500 dimensions and 500 text notes are listed."));
  if (s.conversion && s.conversion.warnings.length) {
    read.push(section("Converter messages", `${s.conversion.warnings.length}`,
      el("p", { class: "note" }, "Objects the converter could not fully decode may be missing from the view."),
      el("ul", { class: "warnings" }, s.conversion.warnings.map((line) => el("li", {}, line)))));
  }
  read.push(section("Layers", `${s.layers.length}`, table([["Layer"], ["Entities", "r"], ["State"]],
    s.layers.map((layer) => el("tr", {}, el("td", {}, layer.name), el("td", { class: "r" }, fmt(layer.entity_count, 0)),
      el("td", {}, layer.frozen ? "frozen" : layer.visible ? "on" : "off"))))));
  read.push(section("Dimensions", `${s.dimensions.length}`, s.dimensions.length ? table([["Type"], ["Value", "r"], ["Text"]],
    s.dimensions.map((dim) => el("tr", {}, el("td", {}, dim.kind), el("td", { class: "r" }, fmt(dim.measurement, 3)),
      el("td", { class: "cell-text" }, dim.text && dim.text !== "<>" ? dim.text : "–"))))
    : el("p", { class: "note" }, "No dimension entities in model space.")));
  read.push(section("Text notes", `${s.texts.length}`, s.texts.length ? table([["Text"], ["Layer"]],
    s.texts.map((note) => el("tr", { class: "clickable", title: "Show on drawing", onclick: () => { setMode("2d"); focusOn(...note.position); } },
      el("td", { class: "cell-text" }, note.text), el("td", { class: "cell-text" }, note.layer))))
    : el("p", { class: "note" }, "No text in model space.")));
  parts.push(fold("What Syntera read from the drawing", `${s.layers.length} layers · ${s.texts.length} notes`, ...read));
  $("details").replaceChildren(...parts);
  $("units-foot").textContent = `${s.units.name} · Y up`;
}

// ---- 3D assembly and piping check ----
const FIELDS = [
  ["equipment_height_mm", "Equipment height, mm"],
  ["pipe_elevation_mm", "Pipe elevation, mm"],
  ["outer_diameter_mm", "Pipe OD, mm"],
  ["bend_radius_mm", "Bend radius, mm"],
  ["clearance_mm", "Clearance, mm"],
];
const COSTS = [["cost_per_metre", "Cost per metre", 120], ["cost_per_bend", "Cost per bend", 45]];

function checkSection(s) {
  const form = el("form", { class: "check-form", id: "check-form", onsubmit: (event) => { event.preventDefault(); runCheck(s.drawing_id); } },
    el("p", { class: "note wide" }, "Leave a field empty to read it from the drawing or use the stated default."),
    FIELDS.map(([key, label]) => el("label", {}, label, el("input", { name: key, type: "number", min: "0", step: "any", placeholder: "from drawing", value: state.options[key] ?? "" }))),
    el("label", {}, "Currency", el("input", { name: "currency", maxlength: "8", value: state.options.currency ?? "USD" })),
    COSTS.map(([key, label, value]) => el("label", {}, label, el("input", { name: key, type: "number", min: "0", step: "any", value: state.options[key] ?? value }))),
    el("label", { class: "wide" }, "Layer roles (layer=equipment, pipe, pipe_centreline, nozzle, structure, frame or ignore)",
      el("input", { name: "layer_roles", placeholder: "e.g. M-EQPT=equipment, P-PIPE=pipe", value: Object.entries(state.options.layer_roles || {}).map(([k, v]) => `${k}=${v}`).join(", ") })));
  const result = el("div", { id: "check-result" });
  const done = state.check && state.check.report.drawing_id === s.drawing_id;
  if (done) result.append(...renderCheck(state.check.report));
  return el("section", { class: "check-section" },
    el("div", { class: "run-row" },
      el("button", { class: "btn primary", id: "check-run", onclick: () => runCheck(s.drawing_id) }, done ? "Run the check again" : "Check design and piping"),
      el("label", { class: "check", title: "Routes every line again to look for a shorter verified route. About 5 minutes on a 14-line plant room." },
        el("input", { id: "opt-optimise", type: "checkbox", ...((state.options.optimise ?? true) ? { checked: "" } : {}) }), " Look for shorter routes")),
    el("details", { class: "settings" }, el("summary", {}, "Settings: sizes, clearance, cost rates, layer roles"), form),
    result);
}

function readOptions() {
  const form = $("check-form"), options = {};
  if (!form) return options;
  FIELDS.forEach(([key]) => { const v = form.elements[key].value.trim(); if (v !== "") options[key] = Number(v); });
  COSTS.forEach(([key]) => { const v = form.elements[key].value.trim(); if (v !== "") options[key] = Number(v); });
  const currency = form.elements.currency.value.trim(); if (currency) options.currency = currency;
  options.optimise = $("opt-optimise").checked;
  const roles = {};
  form.elements.layer_roles.value.split(",").map((pair) => pair.split("=")).forEach(([layer, role]) => {
    if (layer && role && layer.trim() && role.trim()) roles[layer.trim()] = role.trim().toLowerCase();
  });
  if (Object.keys(roles).length) options.layer_roles = roles;
  return options;
}

async function runCheck(drawingId) {
  const button = $("check-run");
  state.options = readOptions();
  button.disabled = true;
  button.textContent = state.options.optimise ? "Checking and routing (a few minutes)…" : "Checking the drawn design…";
  try {
    const payload = await api(`/api/drawings/${drawingId}/assembly`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.options),
    });
    state.check = payload;
    $("check-result").replaceChildren(...renderCheck(payload.report));
    const has3d = payload.meshes.length > 0;
    $("mode-3d").disabled = !has3d;
    $("mode-3d").title = has3d ? "Show the 3D assembly" : "No 3D assembly for this drawing";
    if (has3d) { model.load(payload); setMode("3d"); }
  } catch (error) {
    $("check-result").replaceChildren(el("div", { class: "reason" }, `Check failed: ${error.message}`));
  } finally {
    button.disabled = false; button.textContent = "Run the check again";
  }
}

const metres = (mm) => (mm === null || mm === undefined ? "–" : `${fmt(mm / 1000, 2)} m`);
const money = (report, value) => (value === null || value === undefined ? "–" : `${report.currency} ${fmt(value, 0)}`);
const notRun = (route) => (route.failure_reasons[0] || "").startsWith("not requested");
const verdict = (route) => notRun(route) ? el("span", { class: "chip warn" }, "not run")
  : el("span", { class: `chip ${route.assurance_pass ? "pass" : "fail"}` }, route.assurance_pass ? "pass" : "fail");
const lineKey = (name) => String(name).toUpperCase();

// One entry per real error: the backend's deduplicated `issues` when present, otherwise built from the audit.
function collectIssues(r) {
  if (Array.isArray(r.issues)) return r.issues.map((i) => ({ ...i, inDrawing: true }));
  const issues = [], seen = new Map();
  const lineNames = new Set(r.lines.map((l) => lineKey(l.name)));
  const isPipe = (part) => lineNames.has(lineKey(part));
  const rules = r.audit ? r.audit.rules.filter((rule) => rule.status === "fail") : [];
  rules.forEach((rule) => rule.findings.forEach((f) => {
    if (rule.id === "completeness") {
      const line = r.lines.find((l) => f.parts.includes(l.name));
      const reason = line ? (line.drawn.failure_reasons[0] || "") : "";
      issues.push({ rule: "buildable", title: `${f.parts[0]} cannot be built as drawn`, lines: f.parts, parts: [],
        what: reason ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}.` : f.message, where: null,
        evidence: { text: reason || f.message }, fix: "Change the run's level or leg lengths so every elbow gets its full bend radius." });
      return;
    }
    const key = [...f.parts].sort().join(" | ");
    const pipes = f.parts.filter(isPipe), others = f.parts.filter((p) => !isPipe(p));
    const existing = seen.get(key);
    if (existing) { // a clash and its 0 mm clearance finding are one error
      if (rule.id === "clearance" && f.required_mm !== null) existing.evidence.required_mm = f.required_mm;
      return;
    }
    const clash = rule.id === "clash";
    const named = [...pipes, ...others];
    const issue = {
      rule: rule.id, lines: pipes, parts: others, title: clash ? `${named.join(" clashes with ")}` : `${named.join(" too close to ")}`,
      what: f.message, where: f.location ? { x: f.location[0], y: f.location[1], el: f.location[2] } : null,
      evidence: { measured_mm: clash ? 0 : f.measured_mm, required_mm: f.required_mm, text: f.message },
      fix: pipes.length > 1 ? `Run ${pipes[pipes.length - 1]} at a different elevation, or offset it sideways, so the pipes keep the required gap.`
        : `Move ${pipes[0] || f.parts[0]} clear of ${others.join(", ") || f.parts[1]} by at least the required gap.`,
    };
    seen.set(key, issue); issues.push(issue);
  }));
  return issues;
}

// Lines where Syntera's verified route is worth adopting: shorter, or passing where the drawn one fails.
function suggestions(r) {
  return r.lines.filter((l) => l.optimised.assurance_pass && l.optimised.buildable && !notRun(l.optimised)
    && ((l.length_saving_mm || 0) > 1 || !l.drawn.assurance_pass));
}

function whereText(r, w) {
  if (!w) return null;
  const el_ = w.el ?? w.z;
  return `x ${fmt(w.x, 0)}, y ${fmt(w.y, 0)}${el_ !== undefined && el_ !== null ? `, EL +${fmt(el_ / 1000, 3)}` : ""}`;
}

function showIssue(r, issue) {
  const w = issue.where;
  if (!w) return;
  const origin = r.drawing_origin_mm;
  // `where` from the backend is in drawing coordinates; the audit's own locations are model coordinates.
  const modelPt = issue.inDrawing ? (origin ? [w.x - origin[0], w.y - origin[1], w.el] : null) : [w.x, w.y, w.el ?? w.z];
  const drawingPt = issue.inDrawing ? [w.x, w.y] : (origin ? [w.x + origin[0], w.y + origin[1]] : null);
  if (state.mode === "3d" && modelPt) model.mark(modelPt);
  else if (drawingPt) { setMode("2d"); focusOn(...drawingPt); }
  else if (modelPt) { setMode("3d"); model.mark(modelPt); }
}

function renderCheck(r) {
  if (!r.supported) return [el("div", { class: "reason soft" }, r.message)];
  const issues = collectIssues(r);
  const picks = suggestions(r);
  const routed = r.lines.some((l) => !notRun(l.optimised));
  const savedMm = picks.reduce((sum, l) => sum + Math.max(0, l.length_saving_mm || 0), 0);
  const savedCost = picks.reduce((sum, l) => sum + Math.max(0, l.cost_saving || 0), 0);
  const ok = issues.length === 0;
  const box = el("section", { class: `results ${ok ? "ok" : "bad"}`, "aria-label": "Results" });
  box.append(el("div", { class: "verdict" },
    el("span", { class: `chip ${ok ? "pass" : "fail"}` }, ok ? "Design OK" : "Design has errors"),
    el("b", {}, ok ? `No errors in ${r.lines.length} lines` : `${issues.length} error${issues.length === 1 ? "" : "s"} in the drawn design`)));
  box.append(el("div", { class: "stats" },
    stat("Main piping as drawn", metres(r.total_drawn_length_mm)),
    stat("Saving with Syntera's routes", routed ? (savedMm > 0 ? `${metres(savedMm)}` : "none found") : "not checked",
      routed && savedMm > 0 ? `${fmt(100 * savedMm / r.total_drawn_length_mm, 1)}% · ${money(r, savedCost)} · ${picks.length} reroutes` : null),
    stat("Lines checked", fmt(r.lines.length, 0), `${r.lines.filter((l) => l.drawn.assurance_pass).length} pass as drawn`)));

  if (issues.length) {
    box.append(el("h4", {}, "Errors found"));
    box.append(el("ol", { class: "issues" }, issues.map((issue) => {
      const ev = issue.evidence || {};
      const measured = ev.measured_mm !== undefined && ev.measured_mm !== null && ev.required_mm !== undefined && ev.required_mm !== null
        ? (issue.rule === "buildable" ? `${fmt(ev.measured_mm, 0)} mm available, ${fmt(ev.required_mm, 0)} mm needed`
          : ev.measured_mm < 0 ? `overlap of ${fmt(-ev.measured_mm, 0)} mm, ${fmt(ev.required_mm, 0)} mm gap required`
            : `${fmt(ev.measured_mm, 0)} mm gap, ${fmt(ev.required_mm, 0)} mm required`) : null;
      const at = whereText(r, issue.where);
      return el("li", {},
        el("div", { class: "issue-title" }, el("b", {}, issue.title),
          issue.where ? el("button", { class: "link", type: "button", onclick: () => showIssue(r, issue) }, "Show") : null),
        el("dl", { class: "issue-kv" },
          el("dt", {}, "What"), el("dd", {}, issue.what),
          at ? el("dt", {}, "Where") : null, at ? el("dd", { class: "num" }, at, issue.inDrawing || r.drawing_origin_mm ? "" : " (3D model coordinates)") : null,
          measured ? el("dt", {}, "Evidence") : null, measured ? el("dd", {}, measured, ", measured on the 3D solids") : null,
          el("dt", {}, "Fix"), el("dd", { class: "fix" }, issue.fix)));
    })));
  }

  if (picks.length) {
    box.append(el("h4", {}, "Suggested reroutes"));
    box.append(el("p", { class: "note method" }, "How each route was found: the shortest path that keeps every clearance, with a penalty for each bend, then verified on the exact CAD solid (no clash, full bend radius, ends on the nozzle axis)."));
    box.append(el("ul", { class: "reroutes" }, picks.map((line) => rerouteItem(r, line))));
  } else if (routed) {
    box.append(el("p", { class: "note" }, "Syntera found no verified route shorter than the drawn ones."));
  } else {
    box.append(el("p", { class: "note" }, "Shorter routes were not searched. Tick \"Look for shorter routes\" and run again to get savings."));
  }

  const files = el("div", { class: "row wrap" },
    el("a", { class: "btn", href: `/api/drawings/${r.drawing_id}/assembly/piping_check.json`, download: "piping_check.json" }, "Check report JSON"),
    r.audit ? el("a", { class: "btn", href: `/api/drawings/${r.drawing_id}/assembly/drawn_plant.step` }, "Plant STEP as drawn") : null);
  const defaults = r.assumptions.filter((a) => a.source === "default");
  box.append(el("div", { class: "trace" },
    el("p", { class: "note" }, `Every number above is measured on CAD solids by deterministic checks. ${defaults.length ? `${defaults.length} input${defaults.length === 1 ? " was" : "s were"} assumed, not read from the drawing (${defaults.map((a) => a.quantity.toLowerCase()).join(", ")}); see the details below.` : "All inputs were read from the drawing."}`),
    files));

  return [box, ...detailFolds(r)];
}

function stat(label, value, sub) {
  return el("div", { class: "stat" }, el("span", { class: "label" }, label), el("b", { class: "num" }, value), sub ? el("span", { class: "note num" }, sub) : null);
}

function rerouteItem(r, line) {
  const d = line.drawn, o = line.optimised, why = line.route_rationale;
  const gain = d.minimum_clearance_mm !== null && o.minimum_clearance_mm !== null ? o.minimum_clearance_mm - d.minimum_clearance_mm : null;
  const facts = [];
  if ((line.length_saving_mm || 0) > 1) facts.push(`${metres(line.length_saving_mm)} shorter (${fmt(line.length_saving_percent, 1)}%)`);
  else facts.push("same length");
  if (line.bend_saving) facts.push(`${Math.abs(line.bend_saving)} ${line.bend_saving > 0 ? "fewer" : "more"} bend${Math.abs(line.bend_saving) === 1 ? "" : "s"}`);
  if ((line.cost_saving || 0) > 0) facts.push(`${money(r, line.cost_saving)} less`);
  if (gain !== null && gain > 1) facts.push(`clearance +${fmt(gain, 0)} mm (${fmt(d.minimum_clearance_mm, 0)} → ${fmt(o.minimum_clearance_mm, 0)})`);
  const reasons = [];
  if (!d.assurance_pass) reasons.push(`Fixes the drawn route, which fails (${(d.failure_reasons[0] || "see errors").replace(/\.$/, "")}).`);
  if (why && why.summary) reasons.push(why.summary);
  if (why && !why.summary && why.limiting && why.limiting.length) {
    reasons.push(`Limited by ${why.limiting.map((p) => `${p.name} (${fmt(p.distance_mm, 0)} mm, needs ${fmt(p.required_mm, 0)})`).join(", ")}.`);
  }
  const moved = why && why.nozzle_moves_mm ? Math.max(why.nozzle_moves_mm.start || 0, why.nozzle_moves_mm.end || 0) : line.nozzle_shift_mm;
  const cautions = [];
  if (moved && moved > 1) cautions.push(`Nozzles moved up to ${fmt(moved, 0)} mm onto the ${fmt(line.voxel_resolution_mm, 0)} mm grid.`);
  if (why && why.plant_check) {
    const pc = why.plant_check;
    cautions.push(pc.passes ? `Re-checked against the whole plant: clear, closest part ${pc.closest_part || "–"} at ${fmt(pc.minimum_clearance_mm, 0)} mm.`
      : `Re-checked against the whole plant: clashes with ${pc.clashes.join(", ")}.`);
  } else {
    cautions.push("Not yet re-checked against the other drawn lines.");
  }
  const plantBad = why && why.plant_check && !why.plant_check.passes;
  return el("li", { class: plantBad ? "warn" : "" },
    el("div", { class: "issue-title" }, el("b", {}, line.name), el("span", { class: "num" }, `${metres(d.length_mm)} → ${metres(o.length_mm)}`),
      el("button", { class: "link", type: "button", onclick: () => openBench(r.drawing_id, line) }, "Open in workbench")),
    el("p", { class: "facts" }, facts.join(" · ")),
    reasons.length ? el("p", { class: "why" }, reasons.join(" ")) : null,
    why && why.summary ? null : el("p", { class: "note" }, cautions.join(" ")));
}

function detailFolds(r) {
  const out = [];
  out.push(fold("Line by line", `${r.lines.length} lines`, table([["Line"], ["Drawn"], ["Length", "r"], ["Syntera", "r"], [""]],
    r.lines.map((line) => {
      const d = line.drawn, o = line.optimised;
      return el("tr", { title: [...d.failure_reasons, ...o.failure_reasons].join("; ") },
        el("td", { class: "cell-text" }, line.name), el("td", {}, verdict(d)),
        el("td", { class: "r num" }, metres(d.length_mm)),
        el("td", { class: "r num" }, o.buildable && !notRun(o) ? metres(o.length_mm) : verdict(o)),
        el("td", { class: "r" }, el("button", { class: "link", type: "button", title: "Open this line in the route workbench", onclick: () => openBench(r.drawing_id, line) }, "Open")));
    })),
  el("p", { class: "note" }, "Hover a row for its check messages. STEP and DXF files for each line are in the workbench's Assurance tab.")));
  const audit = r.audit ? renderAudit(r, "Whole-plant audit") : [];
  out.push(fold("Audit rules and assumptions", r.audit ? r.audit.summary : null, ...audit,
    section("What was assumed", null, table([["Quantity"], ["Value"], ["Source"]], r.assumptions.map((a) =>
      el("tr", { title: a.note }, el("td", {}, a.quantity), el("td", { class: "cell-text" }, a.value),
        el("td", {}, el("span", { class: `chip ${a.source === "default" ? "warn" : "pass"}` }, a.source)))))),
    section("All check messages", `${r.findings.length}`, el("ul", { class: "findings" }, r.findings.map((text) => el("li", {}, text))))));
  return out;
}

// ---- route workbench, shown inside this page ----
function openBench(drawingId, line) {
  const folder = line.folder.split("/").pop();
  benchAt(`/?embed=1&drawing=${encodeURIComponent(drawingId)}&line=${encodeURIComponent(folder)}`, line.name);
}

function benchAt(src, title) {
  $("bench-title").textContent = title;
  if ($("bench-frame").getAttribute("src") !== src) $("bench-frame").setAttribute("src", src);
  $("bench").hidden = false;
  document.querySelector(".drawings-main").classList.add("bench-open");
  model.stop();
}

function closeBench() {
  $("bench").hidden = true;
  document.querySelector(".drawings-main").classList.remove("bench-open");
  if (state.mode === "3d") model.start();
}

function setMode(mode) {
  state.mode = mode;
  const three = mode === "3d";
  $("mode-2d").setAttribute("aria-pressed", String(!three));
  $("mode-3d").setAttribute("aria-pressed", String(three));
  $("model-canvas").hidden = !three;
  $("model-legend").hidden = !three;
  $("stage").hidden = three || !state.summary;
  $("sheet").classList.toggle("model-mode", three);
  if (state.summary) $("units-foot").textContent = three ? "millimetres · Z up · drag to orbit" : `${state.summary.units.name} · Y up`;
  if (three) model.start(); else model.stop();
}

// Minimal orbit viewer for the inferred assembly (Z up, millimetres).
const model = (() => {
  let renderer = null, scene, camera, world, frameId = null, bounds = null, marker = null;
  const orbit = { target: null, yaw: -0.9, pitch: 0.55, dist: 1000 };
  function init() {
    if (renderer || typeof THREE === "undefined") return;
    const canvas = $("model-canvas");
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    scene = new THREE.Scene();
    scene.background = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--view").trim() || "#dfe7ea");
    camera = new THREE.PerspectiveCamera(40, 1, 1, 1e6);
    camera.up.set(0, 0, 1);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.8));
    const key = new THREE.DirectionalLight(0xffffff, 0.6); key.position.set(0.6, -0.8, 1.2); scene.add(key);
    orbit.target = new THREE.Vector3();
    let drag = null;
    canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      orbit.yaw -= (e.clientX - drag.x) * 0.008;
      orbit.pitch = Math.min(1.5, Math.max(-0.2, orbit.pitch + (e.clientY - drag.y) * 0.008));
      drag = { x: e.clientX, y: e.clientY };
    });
    canvas.addEventListener("pointerup", () => { drag = null; });
    canvas.addEventListener("wheel", (e) => { e.preventDefault(); orbit.dist *= Math.exp(e.deltaY * 0.001); }, { passive: false });
  }
  function load(payload) {
    init();
    if (!renderer) return;
    marker = null;
    if (world) { scene.remove(world); world.traverse((o) => { o.geometry && o.geometry.dispose(); o.material && o.material.dispose(); }); }
    world = new THREE.Group(); scene.add(world);
    bounds = new THREE.Box3();
    payload.meshes.forEach((m) => {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.Float32BufferAttribute(m.positions, 3));
      geom.setAttribute("normal", new THREE.Float32BufferAttribute(m.normals, 3));
      const drawn = m.kind === "drawn";
      const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(...m.color), roughness: 0.55, metalness: 0.1, transparent: drawn || m.kind === "obstacle", opacity: drawn ? 0.75 : m.kind === "obstacle" ? 0.85 : 1, side: THREE.DoubleSide });
      world.add(new THREE.Mesh(geom, mat));
      bounds.expandByPoint(new THREE.Vector3(...m.bounds.min)); bounds.expandByPoint(new THREE.Vector3(...m.bounds.max));
    });
    const ws = payload.report && payload.report.workspace_mm;
    if (ws) {
      const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(...ws)), new THREE.LineBasicMaterial({ color: 0x8b9aa3 }));
      frame.position.set(ws[0] / 2, ws[1] / 2, ws[2] / 2); world.add(frame);
      bounds.expandByPoint(new THREE.Vector3(0, 0, 0)); bounds.expandByPoint(new THREE.Vector3(...ws));
    }
    fit();
  }
  function fit() {
    if (!bounds || bounds.isEmpty()) return;
    bounds.getCenter(orbit.target);
    orbit.dist = bounds.getSize(new THREE.Vector3()).length() * 1.2;
  }
  function frame() {
    const canvas = $("model-canvas"), w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.floor(w * renderer.getPixelRatio()) || canvas.height !== Math.floor(h * renderer.getPixelRatio())) {
      renderer.setSize(w, h, false); camera.aspect = w / Math.max(1, h); camera.updateProjectionMatrix();
    }
    const flat = orbit.dist * Math.cos(orbit.pitch);
    camera.position.set(orbit.target.x + flat * Math.cos(orbit.yaw), orbit.target.y + flat * Math.sin(orbit.yaw), orbit.target.z + orbit.dist * Math.sin(orbit.pitch));
    camera.near = orbit.dist / 1000; camera.far = orbit.dist * 20; camera.updateProjectionMatrix();
    camera.lookAt(orbit.target);
    renderer.render(scene, camera);
    frameId = requestAnimationFrame(frame);
  }
  // Ring and sphere around a reported error location, then orbit around it.
  function mark(point) {
    if (!renderer || !point || point.some((v) => v === null || v === undefined || Number.isNaN(v))) return;
    if (marker) { world.remove(marker); marker.traverse((o) => { o.geometry && o.geometry.dispose(); o.material && o.material.dispose(); }); }
    const size = Math.max(150, bounds.getSize(new THREE.Vector3()).length() * 0.012);
    marker = new THREE.Group();
    marker.add(new THREE.Mesh(new THREE.SphereGeometry(size, 24, 16), new THREE.MeshBasicMaterial({ color: 0xff2d55, transparent: true, opacity: 0.35, depthTest: false })));
    marker.add(new THREE.Mesh(new THREE.SphereGeometry(size * 0.25, 16, 12), new THREE.MeshBasicMaterial({ color: 0xff2d55, depthTest: false })));
    marker.position.set(...point);
    marker.renderOrder = 10;
    world.add(marker);
    orbit.target.set(...point);
    orbit.dist = size * 40;
  }
  return {
    load, fit, mark,
    zoom(factor) { orbit.dist *= factor; },
    start() { if (renderer && frameId === null) frameId = requestAnimationFrame(frame); },
    stop() { if (frameId !== null) cancelAnimationFrame(frameId); frameId = null; },
  };
})();

// ---- P&ID (DEXPI XML) to a proposed 3D plant ----
async function uploadPid(file) {
  busy("Placing equipment and routing every line. Large P&IDs take a minute or two…");
  try {
    const payload = await api(`/api/pid?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "application/xml" }, body: file,
    });
    state.summary = null;
    state.check = null;
    document.querySelectorAll("#drawing-list li").forEach((li) => li.setAttribute("aria-selected", "false"));
    $("drop").hidden = true;
    $("panel-title").textContent = "P&ID to 3D";
    $("details").replaceChildren(...renderPlant(payload));
    $("mode-3d").disabled = false;
    $("mode-2d").disabled = true;
    $("legend-drawn").hidden = true;
    model.load(payload);
    setMode("3d");
    $("units-foot").textContent = "millimetres · Z up · drag to orbit";
  } catch (error) {
    $("details").replaceChildren(el("div", { class: "reason" }, `Could not build ${file.name}: ${error.message}`));
  } finally {
    $("busy").hidden = true;
  }
}

function renderPlant(payload) {
  const r = payload.report, base = `/api/pid/${payload.plant_id}`;
  const money = (v) => (v === null || v === undefined ? "–" : `${r.currency} ${fmt(v, 0)}`);
  const ws = r.workspace_mm;
  return [
    section("Proposed 3D layout from P&ID", null, el("p", { class: "note" },
      "A P&ID shows what connects to what, not where things sit. Syntera placed the equipment and routed every line to satisfy it. Treat this as a starting layout to review, not the real plant.")),
    kv([
      ["P&ID", r.filename],
      ["Lines routed", `${r.routed} of ${r.routes.length}`],
      ["Total pipe", `${fmt(r.total_pipe_length_mm / 1000, 1)} m`],
      ["Bends", fmt(r.total_bends, 0)],
      ["Estimated pipe cost", money(r.total_cost)],
      ["Skid", `${fmt(ws[0] / 1000, 1)} × ${fmt(ws[1] / 1000, 1)} × ${fmt(ws[2] / 1000, 1)} m`],
      ["Footprint", `${fmt(r.footprint_m2, 1)} m²`],
    ]),
    el("div", { class: "row wrap" },
      el("a", { class: "btn", href: `${base}/plant_assembly.step` }, "Plant STEP"),
      el("a", { class: "btn", href: `${base}/plant_layout.json` }, "Layout JSON")),
    section("Lines", `grid ${fmt(r.voxel_resolution_mm, 0)} mm`, table([["Line"], ["Length", "r"], ["Checks"]],
      r.routes.map((route) => el("tr", { title: route.failure_reasons.join("; ") },
        el("td", {}, route.routed ? el("button", { class: "link", type: "button", title: "Open in route workbench", onclick: () => benchAt(`/?embed=1&pid=${encodeURIComponent(payload.plant_id)}&route=${encodeURIComponent(route.folder)}`, route.line) }, route.line) : route.line,
          el("div", { class: "note line-label" }, route.label)),
        el("td", { class: "r num" }, route.length_mm === null ? "–" : `${fmt(route.length_mm / 1000, 2)} m`),
        el("td", {}, el("span", { class: `chip ${route.assurance_pass ? "pass" : "fail"}` }, route.assurance_pass ? "pass" : "not routed")))))),
    r.failed ? el("div", { class: "reason soft" }, `${r.failed} line(s) could not be routed in this layout: ${r.routes.filter((x) => !x.routed).map((x) => `${x.line} (${x.failure_reasons[0] || "no route"})`).join("; ")}.`) : null,
    ...renderAudit(r),
    section("Equipment", `${r.equipment.length}`, table([["Tag"], ["Class"], ["Size, mm"], ["Source"]],
      r.equipment.map((e) => el("tr", { title: e.size_note }, el("td", {}, e.tag), el("td", { class: "cell-text" }, e.component_class),
        el("td", { class: "num" }, e.size.map((v) => fmt(v, 0)).join(" × ")),
        el("td", {}, el("span", { class: `chip ${e.size_source === "pid" ? "pass" : "warn"}` }, e.size_source === "pid" ? "P&ID" : "default")))))),
    section("What was assumed", null, el("ul", { class: "findings" }, [...r.assumptions, ...r.skipped].map((text) => el("li", {}, text)))),
  ].filter(Boolean);
}

// ---- any STEP assembly: whole-assembly design audit ----
async function uploadAudit(file) {
  busy("Reading every solid and checking each pair. Large assemblies take a minute or two…");
  try {
    const payload = await api(`/api/audit?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "application/step" }, body: file,
    });
    state.summary = null;
    state.check = null;
    document.querySelectorAll("#drawing-list li").forEach((li) => li.setAttribute("aria-selected", "false"));
    $("drop").hidden = true;
    $("panel-title").textContent = "STEP assembly audit";
    $("details").replaceChildren(
      section("Design audit of a STEP assembly", null, el("p", { class: "note" },
        "Solids that are a single swept tube up to DN600 are treated as pipe; everything else as equipment. Red solids are named in a failing finding.")),
      kv([
        ["File", payload.filename],
        ["Pipe solids", fmt(payload.pipes, 0)],
        ["Other solids", fmt(payload.equipment, 0)],
        ["Pipe ODs, mm", payload.sizes_mm.map((v) => fmt(v, 1)).join(", ") || "–"],
      ]),
      el("div", { class: "row wrap" }, el("a", { class: "btn", href: `/api/audit/${payload.audit_id}/audit.json` }, "Audit JSON")),
      ...renderAudit({ audit: payload.audit }, "Result"));
    $("mode-3d").disabled = false;
    $("mode-2d").disabled = true;
    $("legend-drawn").hidden = true;
    model.load(payload);
    setMode("3d");
    $("units-foot").textContent = "millimetres · Z up · drag to orbit";
  } catch (error) {
    $("details").replaceChildren(el("div", { class: "reason" }, `Could not audit ${file.name}: ${error.message}`));
  } finally {
    $("busy").hidden = true;
  }
}

// The whole-assembly design audit and, for a plant, the routing orders the optimiser tried.
function renderAudit(r, title = "Design audit") {
  const a = r.audit;
  if (!a) return [];
  const chip = { pass: "pass", fail: "fail", advisory: "warn" };
  const findings = a.rules.flatMap((rule) => rule.findings.map((f) => ({ rule, f })));
  const parts = [
    section(title, `${a.parts} solids`, el("p", { class: "note" },
      "Every solid checked against every other one, with sizes measured from the CAD solids themselves. ",
      el("strong", {}, a.summary), ".")),
    table([["Rule"], ["Checked", "r"], ["Result"]], a.rules.map((rule) => el("tr", { title: rule.basis },
      el("td", {}, rule.title),
      el("td", { class: "r num" }, fmt(rule.checked, 0)),
      el("td", {}, el("span", { class: `chip ${chip[rule.status]}` },
        rule.status === "pass" ? "pass" : `${rule.findings.length} ${rule.status === "fail" ? "fail" : "to review"}`))))),
  ];
  if (findings.length) {
    parts.push(el("ul", { class: "findings" }, findings.map(({ rule, f }) =>
      el("li", { class: rule.status === "fail" ? "fail" : "" }, `${rule.status === "fail" ? "Fail" : "Review"}: ${f.message}`))));
  }
  if (r.optimisation && r.optimisation.length > 1) {
    parts.push(section("Optimisation", "rip-up and reroute", el("p", { class: "note" },
      "The plant was routed again in other line orders. The kept pass has the fewest unrouted lines, then the fewest audit failures, then the least pipe."),
      table([["Order"], ["Routed", "r"], ["Audit", "r"], ["Pipe", "r"], ["Bends", "r"]], r.optimisation.map((t) =>
        el("tr", { class: t.chosen ? "chosen" : "" },
          el("td", {}, t.strategy.replaceAll("-", " "), t.chosen ? el("span", { class: "chip pass" }, "kept") : null),
          el("td", { class: "r num" }, `${t.routed}/${t.routed + t.failed}`),
          el("td", { class: "r num" }, t.audit_pass ? "pass" : `${t.audit_findings} fail`),
          el("td", { class: "r num" }, `${fmt(t.total_pipe_length_mm / 1000, 1)} m`),
          el("td", { class: "r num" }, fmt(t.total_bends, 0)))))));
  }
  return parts;
}

// ---- loading ----
async function show(drawingId) {
  const s = await api(`/api/drawings/${drawingId}/summary.json`);
  state.summary = s;
  state.mark = null;
  if (state.check && state.check.report.drawing_id !== drawingId) {
    state.check = null;
    $("mode-3d").disabled = true;
  }
  $("mode-2d").disabled = false;
  $("legend-drawn").hidden = false;
  setMode("2d");
  $("drop").hidden = true;
  $("stage").hidden = false;
  $("sheet-img").src = `/api/drawings/${drawingId}/drawing.svg?v=${s.render_version}`;
  $("panel-title").textContent = "Design check";
  closeBench();
  renderDetails(s);
  document.querySelectorAll("#drawing-list li").forEach((li) => li.setAttribute("aria-selected", String(li.dataset.id === drawingId)));
  history.replaceState(null, "", `/drawings#${drawingId}`);
  fit();
}

async function refreshList() {
  const items = await api("/api/drawings");
  $("drawing-list").replaceChildren(...items.map((item) =>
    el("li", { class: "drawing-item", "data-id": item.drawing_id, onclick: () => show(item.drawing_id) },
      el("b", {}, item.source_format), el("strong", { title: item.filename }, item.filename))));
  return items;
}

function busy(text) {
  $("busy-text").textContent = text;
  $("busy").hidden = false;
}

async function upload(file) {
  if (/\.xml$/i.test(file.name)) return uploadPid(file);
  busy("Converting and reading the drawing…");
  try {
    const s = await api(`/api/drawings?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
    });
    await refreshList();
    await show(s.drawing_id);
  } catch (error) {
    $("details").replaceChildren(el("div", { class: "reason" }, `Could not open ${file.name}: ${error.message}`));
  } finally {
    $("busy").hidden = true;
  }
}

async function init() {
  bindSheet();
  $("bench-back").addEventListener("click", closeBench);
  $("open-demo-bench").addEventListener("click", (event) => { event.target.closest("details").open = false; benchAt("/?embed=1", "Demo skid"); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("bench").hidden) closeBench(); });
  document.querySelectorAll(".menu input[type=file]").forEach((input) => input.addEventListener("change", () => { input.closest("details").open = false; }));
  $("audit-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadAudit(file);
    event.target.value = "";
  });
  $("pid-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadPid(file);
    event.target.value = "";
  });
  $("file-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) upload(file);
    event.target.value = "";
  });
  try {
    const caps = await api("/api/drawings/capabilities");
    const chip = $("converter-chip");
    chip.querySelector("i").style.background = caps.dwg ? "var(--pass)" : "var(--warn)";
    chip.querySelector("span").textContent = caps.dwg
      ? `DWG via ${caps.converter === "oda" ? "ODA File Converter" : "LibreDWG"} · up to ${caps.max_upload_mb} MB`
      : "DXF only: no DWG converter installed on the server";
    const items = await refreshList();
    const wanted = location.hash.slice(1);
    if (items.some((item) => item.drawing_id === wanted)) await show(wanted);
  } catch (error) {
    $("converter-chip").querySelector("span").textContent = `Server unavailable: ${error.message}`;
  }
}

init();
