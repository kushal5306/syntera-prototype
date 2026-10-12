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

function renderDetails(s) {
  const units = s.units.mm_per_unit === 1 ? el("span", { class: "chip pass" }, "mm")
    : el("span", { class: "chip warn" }, s.units.name);
  const box = s.view_extents;
  const extentText = box ? `${fmt(box[1][0] - box[0][0])} × ${fmt(box[1][1] - box[0][1])}` : "empty";
  const parts = [
    checkSection(s),
    section("Source", null, kv([
      ["File", s.filename],
      ["Format", s.source_format.toUpperCase()],
      ["DWG release", s.dwg_version || "–"],
      ["Converter", s.conversion ? `${s.conversion.converter}${s.conversion.converter_version ? ` (${s.conversion.converter_version})` : ""}` : "none (DXF read directly)"],
      ["Units", units],
      ["Drawing size", extentText],
      ["Entities", fmt(s.entity_count, 0)],
      ["SHA-256", s.source_sha256.slice(0, 16) + "…"],
    ]), el("div", { class: "row" },
      el("a", { class: "btn", href: `/api/drawings/${s.drawing_id}/drawing.dxf`, download: `${s.filename.replace(/\.[^.]+$/, "")}.dxf` }, "Download DXF"),
      el("a", { class: "btn", href: `/api/drawings/${s.drawing_id}/summary.json`, download: "summary.json" }, "Summary JSON"))),
  ];
  const cautions = [];
  if (s.units.mm_per_unit === null) cautions.push("The drawing does not declare units ($INSUNITS). Confirm the scale before using any dimension.");
  if (s.clipped_handles.length) cautions.push(`${s.clipped_handles.length} entity(ies) far outside the drawing were left out of the default view: ${s.clipped_handles.join(", ")}.`);
  if (s.audit_errors) cautions.push(`The DXF needed ${s.audit_errors} repair(s) while reading; compare against the original before relying on it.`);
  if (s.truncated) cautions.push("Only the first 500 dimensions and 500 text notes are listed.");
  cautions.forEach((text) => parts.push(el("div", { class: "reason soft" }, text)));
  if (s.conversion && s.conversion.warnings.length) {
    parts.push(section("Converter messages", `${s.conversion.warnings.length}`,
      el("p", { class: "note" }, "Objects the converter could not fully decode may be missing from the view."),
      el("ul", { class: "warnings" }, s.conversion.warnings.map((line) => el("li", {}, line)))));
  }
  parts.push(section("Layers", `${s.layers.length}`, table([["Layer"], ["Colour", "r"], ["Entities", "r"], ["State"]],
    s.layers.map((layer) => el("tr", {}, el("td", {}, layer.name), el("td", { class: "r" }, layer.color), el("td", { class: "r" }, fmt(layer.entity_count, 0)),
      el("td", {}, layer.frozen ? "frozen" : layer.visible ? "on" : "off"))))));
  parts.push(section("Dimensions", `${s.dimensions.length}`, s.dimensions.length ? table([["Type"], ["Value", "r"], ["Text"], ["Layer"]],
    s.dimensions.map((dim) => el("tr", {}, el("td", {}, dim.kind), el("td", { class: "r" }, fmt(dim.measurement, 3)),
      el("td", { class: "cell-text" }, dim.text && dim.text !== "<>" ? dim.text : "–"), el("td", { class: "cell-text" }, dim.layer))))
    : el("p", { class: "note" }, "No dimension entities in model space.")));
  parts.push(section("Text notes", `${s.texts.length}`, s.texts.length ? table([["Text"], ["Layer"]],
    s.texts.map((note) => el("tr", { class: "clickable", title: "Show on drawing", onclick: () => focusOn(...note.position) },
      el("td", { class: "cell-text" }, note.text), el("td", { class: "cell-text" }, note.layer))))
    : el("p", { class: "note" }, "No text in model space.")));
  parts.push(section("Entity types", null, kv(Object.entries(s.entity_types).map(([type, count]) => [type, fmt(count, 0)]))));
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
    FIELDS.map(([key, label]) => el("label", {}, label, el("input", { name: key, type: "number", min: "0", step: "any", placeholder: "from drawing", value: state.options[key] ?? "" }))),
    el("label", {}, "Currency", el("input", { name: "currency", maxlength: "8", value: state.options.currency ?? "USD" })),
    COSTS.map(([key, label, value]) => el("label", {}, label, el("input", { name: key, type: "number", min: "0", step: "any", value: state.options[key] ?? value }))),
    el("label", { class: "wide" }, "Layer roles (layer=equipment, pipe, pipe_centreline, nozzle, frame or ignore)",
      el("input", { name: "layer_roles", placeholder: "e.g. M-EQPT=equipment, P-PIPE=pipe", value: Object.entries(state.options.layer_roles || {}).map(([k, v]) => `${k}=${v}`).join(", ") })));
  const result = el("div", { id: "check-result" });
  if (state.check && state.check.report.drawing_id === s.drawing_id) result.append(...renderCheck(state.check.report));
  return el("section", { class: "check-section" },
    el("div", { class: "section-title" }, el("h3", {}, "3D assembly and piping check")),
    el("p", { class: "note" }, "Builds equipment and pipes in 3D from a plan layout, then checks each pipe run as drawn against the shortest route Syntera can verify. Leave a field empty to read it from the drawing or use the stated default."),
    el("details", {}, el("summary", {}, "Inputs and cost rates"), form),
    el("div", { class: "row" }, el("button", { class: "btn primary", id: "check-run", onclick: () => runCheck(s.drawing_id) }, "Generate 3D and check piping")),
    result);
}

function readOptions() {
  const form = $("check-form"), options = {};
  if (!form) return options;
  FIELDS.forEach(([key]) => { const v = form.elements[key].value.trim(); if (v !== "") options[key] = Number(v); });
  COSTS.forEach(([key]) => { const v = form.elements[key].value.trim(); if (v !== "") options[key] = Number(v); });
  const currency = form.elements.currency.value.trim(); if (currency) options.currency = currency;
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
  button.disabled = true; button.textContent = "Building the 3D assembly and routing…";
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
    button.disabled = false; button.textContent = "Generate 3D and check piping";
  }
}

const metres = (mm) => (mm === null || mm === undefined ? "–" : `${fmt(mm / 1000, 2)} m`);
const money = (report, value) => (value === null || value === undefined ? "–" : `${report.currency} ${fmt(value, 0)}`);
const verdict = (route) => el("span", { class: `chip ${route.assurance_pass ? "pass" : "fail"}` }, route.assurance_pass ? "pass" : "fail");

function renderCheck(r) {
  if (!r.supported) return [el("div", { class: "reason soft" }, r.message)];
  const out = [];
  if (r.total_cost_saving !== null) {
    out.push(kv([
      ["Pipe as drawn", metres(r.total_drawn_length_mm)],
      ["Syntera's verified route", metres(r.total_optimised_length_mm)],
      ["Estimated saving", money(r, r.total_cost_saving)],
    ]));
  }
  r.lines.forEach((line) => {
    const d = line.drawn, o = line.optimised;
    out.push(section(line.name, `grid ${fmt(line.voxel_resolution_mm, 0)} mm`, table([["", ""], ["As drawn", "r"], ["Syntera", "r"]], [
      el("tr", {}, el("td", {}, "Checks"), el("td", { class: "r" }, verdict(d)), el("td", { class: "r" }, verdict(o))),
      el("tr", {}, el("td", {}, "Length"), el("td", { class: "r num" }, metres(d.length_mm)), el("td", { class: "r num" }, metres(o.length_mm))),
      el("tr", {}, el("td", {}, "Bends"), el("td", { class: "r num" }, d.bends), el("td", { class: "r num" }, o.buildable ? o.bends : "–")),
      el("tr", {}, el("td", {}, "Min clearance"), el("td", { class: "r num" }, d.minimum_clearance_mm === null ? "–" : `${fmt(d.minimum_clearance_mm, 0)} mm`), el("td", { class: "r num" }, o.minimum_clearance_mm === null ? "–" : `${fmt(o.minimum_clearance_mm, 0)} mm`)),
      el("tr", {}, el("td", {}, "Est. cost"), el("td", { class: "r num" }, money(r, d.estimated_cost)), el("td", { class: "r num" }, money(r, o.estimated_cost))),
    ])));
    const base = `/api/drawings/${r.drawing_id}/${line.folder}`;
    out.push(el("div", { class: "row wrap" },
      el("a", { class: "btn", href: `/?drawing=${r.drawing_id}&line=${line.folder.split("/").pop()}` }, "Open in route workbench"),
      d.buildable ? el("a", { class: "btn", href: `${base}/drawn_assembly.step` }, "STEP as drawn") : null,
      o.buildable ? el("a", { class: "btn", href: `${base}/routed_assembly.step` }, "STEP Syntera route") : null,
      o.buildable ? el("a", { class: "btn", href: `${base}/layout.dxf` }, "Layout DXF") : null));
  });
  out.push(section("Findings", null, el("ul", { class: "findings" }, r.findings.map((text) => el("li", {}, text)))));
  out.push(section("What was assumed", null, table([["Quantity"], ["Value"], ["Source"]], r.assumptions.map((a) =>
    el("tr", { title: a.note }, el("td", {}, a.quantity), el("td", { class: "cell-text" }, a.value),
      el("td", {}, el("span", { class: `chip ${a.source === "default" ? "warn" : "pass"}` }, a.source)))))));
  out.push(el("a", { class: "btn", href: `/api/drawings/${r.drawing_id}/assembly/piping_check.json`, download: "piping_check.json" }, "Piping check JSON"));
  return out;
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
  let renderer = null, scene, camera, world, frameId = null, bounds = null;
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
    const ws = payload.report.workspace_mm;
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
  return {
    load, fit,
    zoom(factor) { orbit.dist *= factor; },
    start() { if (renderer && frameId === null) frameId = requestAnimationFrame(frame); },
    stop() { if (frameId !== null) cancelAnimationFrame(frameId); frameId = null; },
  };
})();

// ---- loading ----
async function show(drawingId) {
  const s = await api(`/api/drawings/${drawingId}/summary.json`);
  state.summary = s;
  state.mark = null;
  if (state.check && state.check.report.drawing_id !== drawingId) {
    state.check = null;
    $("mode-3d").disabled = true;
  }
  setMode("2d");
  $("drop").hidden = true;
  $("stage").hidden = false;
  $("sheet-img").src = `/api/drawings/${drawingId}/drawing.svg?v=${s.render_version}`;
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

async function upload(file) {
  $("busy").hidden = false;
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
