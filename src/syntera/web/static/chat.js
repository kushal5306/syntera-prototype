// Chat-first Syntera. The page sends what the person writes (and any attached files) to
// /api/chat, polls the conversation while Syntera works, and shows each result as a card
// and a 3D view. It only displays what the server returns: every verdict, number and
// saving comes from Syntera's deterministic checks on the server.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
    return node;
  };
  const TOOL_NAMES = {
    design_plant: "Designing the plant",
    check_drawing: "Checking the drawing",
    pid_to_3d: "Building the P&ID in 3D",
    audit_step: "Auditing the 3D model",
    use_sample: "Getting the sample drawing",
  };
  const VERDICT_TEXT = { pass: "Passes all checks", fail: "Problems found", unsupported: "Could not read" };
  const state = { session: null, since: 0, busy: false, pending: [], nodes: new Map(), views: new Map(), active: null, polling: false };

  // ---- conversation ------------------------------------------------------------------
  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const body = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
    if (!response.ok) throw new Error((body && (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))) || response.statusText);
    return body;
  }

  async function status() {
    const chip = $("model-chip");
    try {
      const s = await api("/api/chat/status");
      chip.classList.add(s.ai ? "live" : "basic");
      chip.querySelector("span").textContent = s.label;
      chip.title = s.ai ? "Claude reads your messages and drives Syntera's tools" :
        "No Claude API key on this server: Syntera understands uploads, the sample, and simple 'from A to B' requests";
    } catch (error) {
      chip.querySelector("span").textContent = "Server not reachable";
    }
  }

  async function startSession(reuse) {
    if (reuse) {
      try {
        const snap = await api(`/api/chat/sessions/${reuse}`);
        state.session = reuse;
        apply(snap);
        if (snap.busy) poll();
        return;
      } catch (error) { /* expired: start a new one */ }
    }
    const snap = await api("/api/chat/sessions", { method: "POST" });
    state.session = snap.id;
    try { sessionStorage.setItem("syntera-chat", snap.id); } catch (error) { /* storage blocked */ }
  }

  function resetChat() {
    try { sessionStorage.removeItem("syntera-chat"); } catch (error) { /* ignore */ }
    location.reload();
  }

  function apply(snap) {
    for (const event of snap.events) render(event);
    const running = snap.events.filter((e) => e.type === "activity" && e.status === "running").map((e) => e.id);
    const last = snap.events.length ? snap.events[snap.events.length - 1].id : state.since;
    state.since = running.length ? Math.min(...running) - 1 : Math.max(state.since, last);
    state.busy = snap.busy;
    setBusy(snap.busy);
  }

  async function poll() {
    if (state.polling) return;
    state.polling = true;
    try {
      while (true) {
        const snap = await api(`/api/chat/sessions/${state.session}?since=${state.since}`);
        apply(snap);
        if (!snap.busy) break;
        await new Promise((resolve) => setTimeout(resolve, 900));
      }
    } catch (error) {
      render({ id: `local-${Date.now()}`, type: "error", text: `Lost contact with Syntera: ${error.message}` });
      setBusy(false);
    } finally {
      state.polling = false;
    }
  }

  function setBusy(busy) {
    $("send").disabled = busy;
    $("send").textContent = busy ? "Working…" : "Send";
    let thinking = document.querySelector(".thinking");
    if (busy && !thinking) {
      thinking = el("div", { class: "thinking" }, "Syntera is working…");
      $("log").append(thinking);
    } else if (!busy && thinking) thinking.remove();
    if (busy && thinking) $("log").append(thinking);
  }

  async function send(text) {
    text = text.trim();
    if (state.busy || (!text && !state.pending.length)) return;
    if (!text) text = "Please check this file.";
    const files = state.pending.map((f) => f.id);
    state.pending = [];
    renderAttachments();
    $("input").value = "";
    autosize();
    $("welcome")?.remove();
    state.busy = true;
    setBusy(true);
    try {
      await api(`/api/chat/sessions/${state.session}/messages`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ text, file_ids: files }),
      });
    } catch (error) {
      if (/unknown conversation/.test(error.message)) { await startSession(null); return send(text); }
      render({ id: `local-${Date.now()}`, type: "error", text: error.message });
      state.busy = false;
      setBusy(false);
      return;
    }
    poll();
  }

  // ---- rendering ---------------------------------------------------------------------
  function inline(text) {
    const parts = [];
    const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
    let last = 0, match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      parts.push(match[1] !== undefined ? el("b", {}, match[1]) : el("code", {}, match[2]));
      last = pattern.lastIndex;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }

  function richText(text) {
    const out = [];
    let list = null, para = [];
    const flush = () => { if (para.length) { out.push(el("p", {}, inline(para.join(" ")))); para = []; } };
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (bullet) { flush(); if (!list) { list = el("ul"); out.push(list); } list.append(el("li", {}, inline(bullet[1]))); continue; }
      list = null;
      if (!line) { flush(); continue; }
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      if (heading) { flush(); out.push(el("p", {}, el("b", {}, heading[1]))); continue; }
      para.push(line);
    }
    flush();
    return out;
  }

  function fileChip(file, removable) {
    return el("span", { class: "file-chip", title: `${file.kind}, ${(file.size / 1024).toFixed(0)} KB` },
      "📎 ", file.filename,
      removable ? el("button", { type: "button", "aria-label": `Remove ${file.filename}`, onclick: () => { state.pending = state.pending.filter((f) => f.id !== file.id); renderAttachments(); } }, "×") : null);
  }

  function render(event) {
    $("welcome")?.remove();
    let node;
    if (event.type === "user") {
      node = el("div", { class: "msg user" }, event.text ? richText(event.text) : null,
        event.files && event.files.length ? el("div", { class: "files" }, event.files.map((f) => fileChip(f, false))) : null);
    } else if (event.type === "assistant") {
      const views = event.views || [];
      views.forEach((view) => addView(view));
      node = el("div", { class: "msg assistant" }, richText(event.text),
        views.length ? el("div", { class: "view-link" }, views.map((view) => el("button", { class: "btn", type: "button", onclick: () => showView(view.id) }, `Show result: ${view.title}`))) : null);
      if (views.length) showView(views[views.length - 1].id);
    } else if (event.type === "activity") {
      const verdictClass = event.verdict === "fail" ? " fail" : "";
      node = el("div", { class: `activity ${event.status}${verdictClass}` }, el("span", { class: "state", "aria-hidden": "true" }),
        el("div", {}, el("b", {}, TOOL_NAMES[event.tool] || event.tool), el("div", {}, event.text)));
    } else if (event.type === "file") {
      node = el("div", { class: "msg assistant" }, el("p", {}, "Added a file to the conversation:"), el("div", { class: "files" }, fileChip(event.file, false)));
    } else if (event.type === "error") {
      node = el("div", { class: "msg error", role: "alert" }, event.text);
    } else return;
    const existing = state.nodes.get(event.id);
    if (existing) existing.replaceWith(node);
    else {
      const thinking = document.querySelector(".thinking");
      if (thinking) thinking.before(node); else $("log").append(node);
    }
    state.nodes.set(event.id, node);
    $("log").scrollTop = $("log").scrollHeight;
  }

  function renderAttachments() {
    $("attachments").replaceChildren(...state.pending.map((f) => fileChip(f, true)));
  }

  async function attach(fileList) {
    for (const file of fileList) {
      const note = $("composer-note");
      note.textContent = `Uploading ${file.name}…`;
      try {
        const stored = await api(`/api/chat/files?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
        state.pending.push(stored);
        renderAttachments();
        note.textContent = "Attached. Add a note if you like, then press Send.";
      } catch (error) {
        note.textContent = `Could not attach ${file.name}: ${error.message}`;
      }
    }
    $("input").focus();
  }

  // ---- results: card + 3D --------------------------------------------------------------
  function addView(view) {
    if (state.views.has(view.id)) return;
    state.views.set(view.id, view);
    $("view-tabs").append(el("button", { type: "button", role: "tab", "data-view": view.id, onclick: () => showView(view.id) },
      el("span", { class: `dot ${view.card.verdict === "pass" ? "pass" : "fail"}` }), view.title));
  }

  function showView(id) {
    const view = state.views.get(id);
    if (!view) return;
    state.active = id;
    $("result").hidden = false;
    document.querySelector(".chat-main").classList.add("has-result");
    for (const tab of $("view-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.view === id));
    renderCard(view);
    loadModel(id);
  }

  function renderCard(view) {
    const card = view.card;
    const box = $("card");
    box.className = `result-card ${card.verdict}`;
    box.replaceChildren(...[
      el("div", { class: "result-head" }, el("h2", {}, view.title),
        el("span", { class: `verdict-pill ${card.verdict}` }, VERDICT_TEXT[card.verdict] || card.verdict)),
      el("p", { class: "headline" }, card.headline),
      card.stats.length ? el("div", { class: "stats" }, card.stats.map((s) => el("div", { class: "stat" }, el("b", {}, s.value), el("span", {}, s.label)))) : null,
      card.issues.length ? el("div", {}, el("div", { class: "label" }, "What to fix"),
        el("ul", { class: "issues" }, card.issues.map((issue) => el("li", {},
          el("span", {}, issue.title),
          issue.fix ? el("span", { class: "fix" }, `Fix: ${issue.fix}`) : null,
          issue.where ? el("span", { class: "where" }, `On the drawing: ${issue.where}`) : null,
          issue.point_mm ? el("button", { class: "btn", type: "button", onclick: () => viewer.mark(issue.point_mm) }, "Show in 3D") : null)))) : null,
      card.links.length ? el("div", { class: "links" }, card.links.map((link) => el("a", { class: "btn", href: link.url, target: link.url.startsWith("/drawings") ? null : "_blank", rel: "noopener" }, link.label))) : null,
    ].filter(Boolean));
  }

  const meshCache = new Map();
  async function loadModel(id) {
    $("viewer-loading").hidden = false;
    try {
      if (!meshCache.has(id)) meshCache.set(id, await api(`/api/chat/views/${id}`));
      if (state.active === id) viewer.show(meshCache.get(id).meshes);
    } catch (error) {
      $("viewer-loading").textContent = `Could not load the 3D view: ${error.message}`;
      return;
    }
    $("viewer-loading").hidden = true;
  }

  // ---- 3D viewer (three.js r128, Z up, millimetres) -------------------------------------
  const viewer = (function () {
    const canvas = $("canvas");
    const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, 10, 1e6);
    camera.up.set(0, 0, 1);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.8));
    const key = new THREE.DirectionalLight(0xffffff, 0.6); key.position.set(0.6, -0.8, 1.2); scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.25); fill.position.set(-1, 0.7, 0.3); scene.add(fill);
    let world = new THREE.Group(); scene.add(world);
    let marker = null, box = new THREE.Box3(), xray = false;
    const hidden = new Set();
    const orbit = { target: new THREE.Vector3(), yaw: -0.9, pitch: 0.6, dist: 10000 };
    const KIND_LABEL = { port: "Nozzles", obstacle: "Equipment and steel", route: "Syntera's pipes", drawn: "Pipes as drawn" };

    function place() {
      const p = orbit.pitch, y = orbit.yaw;
      camera.position.set(orbit.target.x + orbit.dist * Math.cos(p) * Math.cos(y), orbit.target.y + orbit.dist * Math.cos(p) * Math.sin(y), orbit.target.z + orbit.dist * Math.sin(p));
      camera.lookAt(orbit.target);
      camera.near = Math.max(1, orbit.dist / 500); camera.far = orbit.dist * 50; camera.updateProjectionMatrix();
    }
    function resize() {
      const r = canvas.parentElement.getBoundingClientRect();
      if (!r.width || !r.height) return;
      renderer.setSize(r.width, r.height, false);
      camera.aspect = r.width / r.height; camera.updateProjectionMatrix();
    }
    function fit() {
      if (box.isEmpty()) return;
      orbit.target.copy(box.getCenter(new THREE.Vector3()));
      orbit.dist = box.getSize(new THREE.Vector3()).length() * 1.7;
      orbit.yaw = -0.9; orbit.pitch = 0.6;
    }
    function legend(meshes) {
      const kinds = new Map();
      for (const m of meshes) if (!kinds.has(m.kind)) kinds.set(m.kind, m.color);
      $("chat-legend").replaceChildren(...[...kinds].map(([kind, color]) => el("button", {
        type: "button", class: "legend-item", "aria-pressed": String(!hidden.has(kind)),
        title: "Show or hide", onclick: (e) => { if (hidden.has(kind)) hidden.delete(kind); else hidden.add(kind); e.currentTarget.setAttribute("aria-pressed", String(!hidden.has(kind))); apply(); },
      }, el("i", { style: `background: rgb(${color.map((c) => Math.round(c * 255)).join(",")})` }), KIND_LABEL[kind] || kind)));
    }
    function apply() {
      world.children.forEach((mesh) => {
        if (!mesh.userData.kind) return;
        mesh.visible = !hidden.has(mesh.userData.kind);
        mesh.material.opacity = xray && mesh.userData.kind === "obstacle" ? 0.25 : 1;
        mesh.material.depthWrite = !(xray && mesh.userData.kind === "obstacle");
      });
    }
    function show(meshes) {
      scene.remove(world); world.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      world = new THREE.Group(); scene.add(world); marker = null; box = new THREE.Box3(); hidden.clear();
      scene.background = new THREE.Color(css("--view") || "#dfe7ea");
      for (const m of meshes) {
        const geom = new THREE.BufferGeometry();
        geom.setAttribute("position", new THREE.Float32BufferAttribute(m.positions, 3));
        geom.setAttribute("normal", new THREE.Float32BufferAttribute(m.normals, 3));
        const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(...m.color), roughness: 0.55, metalness: 0.15, transparent: true, side: THREE.DoubleSide });
        const mesh = new THREE.Mesh(geom, mat); mesh.userData.kind = m.kind; mesh.name = m.name; world.add(mesh);
        box.expandByPoint(new THREE.Vector3(...m.bounds.min)); box.expandByPoint(new THREE.Vector3(...m.bounds.max));
      }
      if (!box.isEmpty()) {
        const size = box.getSize(new THREE.Vector3()), centre = box.getCenter(new THREE.Vector3());
        const grid = new THREE.GridHelper(Math.max(size.x, size.y) * 1.4, 28, 0x8899a0, 0xa9b8be);
        grid.rotation.x = Math.PI / 2; grid.position.set(centre.x, centre.y, box.min.z); grid.material.transparent = true; grid.material.opacity = 0.45; world.add(grid);
      }
      legend(meshes); apply(); resize(); fit();
    }
    function mark(point) {
      if (marker) world.remove(marker);
      const size = box.isEmpty() ? 300 : box.getSize(new THREE.Vector3()).length() / 60;
      marker = new THREE.Mesh(new THREE.SphereGeometry(size, 24, 16), new THREE.MeshBasicMaterial({ color: css("--fail") || "#b8323a", transparent: true, opacity: 0.85, depthTest: false }));
      marker.renderOrder = 10; marker.position.set(...point); world.add(marker);
      orbit.target.set(...point); orbit.dist = Math.max(size * 30, 2500);
      if (!xray) { xray = true; $("xray").setAttribute("aria-pressed", "true"); apply(); }
    }
    let drag = null;
    canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 }; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) {
        const s = orbit.dist * 0.0012, right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
        orbit.target.addScaledVector(right, -dx * s).addScaledVector(up, dy * s);
      } else {
        orbit.yaw -= dx * 0.006; orbit.pitch = Math.max(-1.45, Math.min(1.45, orbit.pitch + dy * 0.006));
      }
    });
    canvas.addEventListener("pointerup", () => { drag = null; });
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    canvas.addEventListener("wheel", (e) => { e.preventDefault(); orbit.dist = Math.max(50, orbit.dist * Math.exp(e.deltaY * 0.001)); }, { passive: false });
    $("fit").addEventListener("click", fit);
    $("xray").addEventListener("click", () => { xray = !xray; $("xray").setAttribute("aria-pressed", String(xray)); apply(); });
    new ResizeObserver(resize).observe(canvas.parentElement);
    (function loop() { place(); renderer.render(scene, camera); requestAnimationFrame(loop); })();
    return { show, mark };
  })();

  // ---- wiring --------------------------------------------------------------------------
  function autosize() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = `${Math.min(180, input.scrollHeight)}px`;
  }
  $("input").addEventListener("input", autosize);
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("input").value); } });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("input").value); });
  $("file-input").addEventListener("change", (e) => { attach([...e.target.files]); e.target.value = ""; });
  $("new-chat").addEventListener("click", resetChat);
  for (const button of document.querySelectorAll(".suggestion")) {
    button.addEventListener("click", () => {
      if (button.dataset.attach) $("file-input").click();
      else send(button.dataset.prompt);
    });
  }
  document.addEventListener("dragover", (e) => { e.preventDefault(); $("composer").classList.add("dragging"); });
  document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) $("composer").classList.remove("dragging"); });
  document.addEventListener("drop", (e) => { e.preventDefault(); $("composer").classList.remove("dragging"); if (e.dataTransfer.files.length) attach([...e.dataTransfer.files]); });

  status();
  let stored = null;
  try { stored = sessionStorage.getItem("syntera-chat"); } catch (error) { /* storage blocked */ }
  startSession(stored).catch((error) => render({ id: "start", type: "error", text: `Could not start a conversation: ${error.message}` }));
  window.synteraChat = { send, state };
})();
