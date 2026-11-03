// Design page: edit parameters through change sets with a before/after diff (G2), and
// keep and compare versions of the design document (G4). Display data only: every value,
// diff and verdict shown here comes from the server.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const make = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    node.append(...children.map((c) => (c == null ? "" : c)));
    return node;
  };
  const show = (v) => (Array.isArray(v) ? v.join(", ") : v == null ? "–" : String(v));
  let current = null;
  let pending = null;

  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : response.statusText);
    return data;
  }

  function edits() {
    const result = [];
    document.querySelectorAll("#values input[data-path]").forEach((input) => {
      const before = Number(input.dataset.value);
      const after = Number(input.value);
      if (input.value !== "" && Number.isFinite(after) && after !== before) {
        result.push({ op: "set_parameter", path: input.dataset.path, value: after,
          explanation: "Edited on the design page" });
      }
    });
    return result;
  }

  function diffTable(diff, verdicts) {
    const box = make("div");
    const rows = diff.values.map((v) => make("tr", {}, make("td", {}, v.path), make("td", {}, show(v.before)), make("td", {}, show(v.after))));
    box.append(make("table", {}, make("thead", {}, make("tr", {}, make("th", {}, "Value"), make("th", {}, "Before"), make("th", {}, "After"))), make("tbody", {}, ...rows)));
    if (diff.rationale.length) box.append(make("p", { class: "muted" }, `${diff.rationale.length} rationale record(s) change.`));
    if (diff.routes_changed.length) box.append(make("p", { class: "muted" }, `Stored routes cleared for: ${diff.routes_changed.join(", ")}.`));
    for (const v of diff.verdicts || []) {
      box.append(make("p", {}, `Check ${v.check}: ${show(v.before)} → ${show(v.after)}`));
    }
    if (verdicts) {
      const cls = verdicts.overall_pass ? "verdict-pass" : "verdict-fail";
      box.append(make("p", { class: cls }, `Exact geometry checks: ${verdicts.overall_pass ? "PASS" : "FAIL"}`));
      for (const reason of verdicts.failure_reasons) box.append(make("p", { class: "muted" }, reason));
    }
    return box;
  }

  function renderValues(view) {
    current = view;
    $("design-title").textContent = view.title;
    $("version-chip").textContent = `version ${view.version}`;
    $("values").replaceChildren(...view.values.map((v) => {
      const cell = v.editable && typeof v.value === "number"
        ? make("input", { type: "number", step: "any", value: v.value, "data-path": v.path, "data-value": v.value, "aria-label": v.path })
        : show(v.value);
      return make("tr", {}, make("td", {}, v.path), make("td", {}, cell, v.unit ? ` ${v.unit}` : ""), make("td", {}, v.source), make("td", { class: "why" }, v.text));
    }));
  }

  async function renderVersions() {
    const versions = await api("/api/design/versions");
    $("versions").replaceChildren(...versions.map((v) => make("li", {}, `v${v.number}: ${v.description} (${v.sha256.slice(0, 10)})`)));
    for (const id of ["compare-a", "compare-b"]) {
      $(id).replaceChildren(...versions.map((v) => make("option", { value: v.number }, `v${v.number}`)));
    }
    if (versions.length > 1) $("compare-a").value = versions.length - 1;
    $("compare-b").value = versions.length;
  }

  async function load() {
    renderValues(await api("/api/design"));
    await renderVersions();
  }

  $("preview").addEventListener("click", async () => {
    const list = edits();
    if (!list.length) { $("diff").replaceChildren(make("p", { class: "muted" }, "Change a value first.")); return; }
    pending = { description: `Edited ${list.map((e) => e.path).join(", ")}`, edits: list };
    try {
      const result = await api("/api/design/preview", { changes: pending });
      if (!result.valid) { $("diff").replaceChildren(make("p", { class: "verdict-fail" }, result.error)); $("apply").disabled = true; return; }
      $("diff").replaceChildren(diffTable(result.diff));
      $("apply").disabled = false;
    } catch (error) { $("diff").replaceChildren(make("p", { class: "verdict-fail" }, error.message)); }
  });

  $("apply").addEventListener("click", async () => {
    if (!pending) return;
    $("apply").disabled = true;
    $("diff").replaceChildren(make("p", { class: "muted" }, "Applying…"));
    try {
      const result = await api("/api/design/apply", { changes: pending, regenerate: $("regenerate").checked });
      pending = null;
      await load();
      $("diff").replaceChildren(make("p", {}, `Saved as version ${result.version}.`), diffTable(result.diff, result.verdicts));
    } catch (error) { $("diff").replaceChildren(make("p", { class: "verdict-fail" }, error.message)); }
  });

  $("compare").addEventListener("click", async () => {
    try {
      const result = await api(`/api/design/compare?a=${$("compare-a").value}&b=${$("compare-b").value}`);
      $("compare-out").replaceChildren(result.diff.values.length || result.diff.rationale.length
        ? diffTable(result.diff) : make("p", { class: "muted" }, "No differences."));
    } catch (error) { $("compare-out").replaceChildren(make("p", { class: "verdict-fail" }, error.message)); }
  });

  function sheetItem(sheet) {
    const links = sheet.downloads.map((d) => make("a", { href: d.url, download: d.name }, d.name));
    const item = make("li", {}, `Sheet ${sheet.id}${sheet.sheet ? ` (ISO ${sheet.sheet}, ${sheet.scale})` : ""}: `);
    if (sheet.viewer) item.append(make("a", { href: sheet.viewer }, "view"), " · ");
    links.forEach((link, i) => item.append(link, i < links.length - 1 ? " · " : ""));
    return item;
  }

  async function renderSheets() {
    const [sheets, caps] = await Promise.all([api("/api/sheets"), api("/api/sheets/capabilities")]);
    $("sheet-caps").textContent = caps.dwg ? "DXF, PDF and DWG" : "DXF and PDF (DWG needs the ODA File Converter)";
    $("sheets").replaceChildren(...sheets.map(sheetItem));
  }

  $("make-sheet").addEventListener("click", async () => {
    $("make-sheet").disabled = true;
    try {
      await api("/api/sheets", {});
      await renderSheets();
    } catch (error) {
      $("sheets").prepend(make("li", { class: "verdict-fail" }, error.message));
    } finally {
      $("make-sheet").disabled = false;
    }
  });

  renderSheets().catch(() => {});
  load().catch((error) => { $("diff").replaceChildren(make("p", { class: "verdict-fail" }, error.message)); });
})();
