// Drawings page: upload DWG/DXF, view the server-rendered SVG with pan and zoom, list what was read.
"use strict";

const MARGIN_MM = 4; // must match layout.Margins in syntera.drawings.reader.render_svg
const $ = (id) => document.getElementById(id);
const state = { summary: null, scale: 1, x: 0, y: 0, mark: null };

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
  if (!response.ok) throw new Error(body.detail || `request failed (${response.status})`);
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
    if (!state.summary) return;
    event.preventDefault();
    const box = sheet.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });
  sheet.addEventListener("pointerdown", (event) => {
    if (!state.summary || event.target.closest(".vp-tools")) return;
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
  $("zoom-in").addEventListener("click", () => { const b = sheet.getBoundingClientRect(); zoomAt(1.5, b.width / 2, b.height / 2); });
  $("zoom-out").addEventListener("click", () => { const b = sheet.getBoundingClientRect(); zoomAt(1 / 1.5, b.width / 2, b.height / 2); });
  $("zoom-fit").addEventListener("click", () => { state.mark = null; fit(); });
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

// ---- loading ----
async function show(drawingId) {
  const s = await api(`/api/drawings/${drawingId}/summary.json`);
  state.summary = s;
  state.mark = null;
  $("drop").hidden = true;
  $("stage").hidden = false;
  $("sheet-img").src = `/api/drawings/${drawingId}/drawing.svg`;
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
