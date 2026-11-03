// Assistant panel shared by the workbench and the drawings page (roadmap G1).
// It sends questions to /api/assistant, shows the answer with its citations, and
// highlights a cited drawing entity in 2D or a cited line or equipment in 3D.
// The panel only displays what the server returns; verdicts come from the checks.
"use strict";
(function () {
  if (window.top !== window) return; // embedded workbench: the host page has the panel
  const onDrawings = location.pathname.startsWith("/drawings");
  const history = [];
  const graphs = new Map();
  const make = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    node.append(...children);
    return node;
  };

  const log = make("div", { class: "assist-log", "aria-live": "polite" });
  const input = make("textarea", { rows: "2", placeholder: "Ask why a value was chosen, or about the drawing…", "aria-label": "Question for the assistant" });
  const status = make("p", { class: "assist-note" }, "Checking whether a model is configured…");
  const send = make("button", { class: "btn primary", type: "button" }, "Ask");
  const panel = make("section", { class: "assist-panel", "aria-label": "Assistant", hidden: "" },
    make("header", {}, make("b", {}, "Ask Syntera"), make("span", { class: "assist-note" }, "Answers cite their sources; verdicts come only from the checks.")),
    log, make("div", { class: "assist-input" }, input, send), status);
  const toggle = make("button", { class: "assist-toggle btn", type: "button", "aria-expanded": "false" }, "Ask Syntera");
  toggle.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  });
  document.body.append(panel, toggle);

  function drawingId() {
    const id = location.hash.replace("#", "");
    return /^[0-9a-f]{16}$/.test(id) ? id : null;
  }

  async function graphFor(id) {
    if (!graphs.has(id)) {
      const response = await fetch(`/api/drawings/${id}/drawing_graph.json`);
      graphs.set(id, response.ok ? await response.json() : null);
    }
    return graphs.get(id);
  }

  async function highlight(citation) {
    const id = citation.id;
    if (citation.kind === "drawing" && id.startsWith("drawing:")) {
      const [, drawing, handle] = id.split(":");
      const graph = await graphFor(drawing);
      const node = graph && graph.nodes.find((n) => n.handle === handle);
      if (node && typeof focusOn === "function") {
        if (typeof setMode === "function") setMode("2d");
        const [[x0, y0], [x1, y1]] = node.bbox;
        focusOn((x0 + x1) / 2, (y0 + y1) / 2);
      }
      return;
    }
    if (typeof select !== "function") return;
    if (citation.kind === "rationale" && id.startsWith("lines.")) select("routed-tube");
    else if (citation.kind === "drawing" && id.startsWith("equipment:")) select(id.slice(10));
  }

  function addTurn(role, text, citations = [], meta = "") {
    const body = make("div", { class: `assist-turn ${role}` }, make("p", {}, text));
    if (citations.length) {
      const chips = make("div", { class: "assist-cites" });
      for (const citation of citations) {
        chips.append(make("button", { class: "chip", type: "button", title: `${citation.kind}: ${citation.id}`, onclick: () => highlight(citation) }, citation.id));
      }
      body.append(chips);
    }
    if (meta) body.append(make("p", { class: "assist-note" }, meta));
    log.append(body);
    log.scrollTop = log.scrollHeight;
  }

  async function ask() {
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    addTurn("user", question);
    send.disabled = true;
    try {
      const payload = { question, history: history.slice(-10) };
      if (onDrawings && drawingId()) payload.drawing_id = drawingId();
      const response = await fetch("/api/assistant", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || response.statusText);
      const meta = `${body.tool_calls.length} tool call(s)${body.checks_run ? " · checks ran" : ""}`;
      addTurn("assistant", body.answer, body.citations, meta);
      history.push({ role: "user", text: question }, { role: "assistant", text: body.answer });
      const first = body.citations.find((c) => c.kind === "drawing");
      if (first) highlight(first);
    } catch (error) {
      addTurn("error", `The assistant could not answer: ${error.message}`);
    } finally {
      send.disabled = false;
    }
  }

  send.addEventListener("click", ask);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(); } });
  fetch("/api/assistant/status").then((r) => r.json()).then((s) => {
    status.textContent = s.available ? `Model: ${s.provider}.` : "No language model is configured on this server (ANTHROPIC_API_KEY).";
    send.disabled = !s.available;
  }).catch(() => { status.textContent = "Assistant status unavailable."; });
})();
