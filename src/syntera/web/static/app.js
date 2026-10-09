(() => {
"use strict";
const snapshotEl = document.getElementById("snapshot");
const DATA = snapshotEl ? JSON.parse(snapshotEl.textContent) : { geometries: {}, scenarios: [] };
const LIVE = !snapshotEl;
const $ = (s) => document.querySelector(s);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (v, d = 1) => v == null || !isFinite(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };

const state = { run: 0, shade: "solid", caseIdx: 0, xray: false, env: true, flow: true, deform: 150, tab: store.get("syntera.tab", "assurance"), selected: null, hidden: new Set() };

/* ---------- colour ramp for stress / displacement ---------- */
const RAMP = [[0, [0.17, 0.24, 0.56]], [0.3, [0.16, 0.56, 0.69]], [0.55, [0.37, 0.75, 0.54]], [0.78, [0.95, 0.76, 0.31]], [1, [0.85, 0.27, 0.18]]];
function ramp(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < RAMP.length; i++) if (t <= RAMP[i][0]) {
    const [t0, a] = RAMP[i - 1], [t1, b] = RAMP[i], f = (t - t0) / (t1 - t0);
    return a.map((v, k) => v + (b[k] - v) * f);
  }
  return RAMP[RAMP.length - 1][1];
}
const rampCss = `linear-gradient(90deg, ${RAMP.map(([t, c]) => `rgb(${c.map((v) => Math.round(v * 255)).join(",")}) ${t * 100}%`).join(",")})`;

/* ---------- route geometry: orthogonal polyline with circular fillets ---------- */
function routePath(points, R) {
  const P = points.map((p) => new THREE.Vector3(...p));
  const segs = [];
  if (P.length < 2) return { segs, samples: [], length: 0 };
  let cursor = P[0].clone(), s = 0, straightNo = 0, bendNo = 0;
  const samples = [];
  const addStraight = (a, b) => {
    const L = a.distanceTo(b);
    if (L > 1e-6) {
      const seg = { kind: "Straight", no: ++straightNo, length: L, from: a.clone(), to: b.clone(), s0: s };
      const n = Math.max(2, Math.ceil(L / 4));
      for (let i = 0; i <= n; i++) samples.push({ p: a.clone().lerp(b, i / n), s: s + L * i / n, seg: segs.length });
      segs.push(seg); s += L;
    }
  };
  for (let i = 1; i < P.length - 1; i++) {
    const a = P[i].clone().sub(P[i - 1]).normalize(), b = P[i + 1].clone().sub(P[i]).normalize();
    const S = P[i].clone().addScaledVector(a, -R), E = P[i].clone().addScaledVector(b, R), C = S.clone().addScaledVector(b, R);
    addStraight(cursor, S);
    const L = Math.PI * R / 2, n = 16;
    const seg = { kind: "Bend", no: ++bendNo, length: L, from: S.clone(), to: E.clone(), corner: P[i].clone(), s0: s };
    for (let k = 0; k <= n; k++) {
      const t = (Math.PI / 2) * k / n;
      samples.push({ p: C.clone().addScaledVector(b, -R * Math.cos(t)).addScaledVector(a, R * Math.sin(t)), s: s + L * k / n, seg: segs.length });
    }
    segs.push(seg); s += L; cursor = E;
  }
  addStraight(cursor, P[P.length - 1]);
  return { segs, samples, length: s };
}

/* ---------- derived engineering numbers per run ---------- */
function solvedFea(run) { return Boolean(run.fea && run.fea.nodes); }
// Fail closed: a run with FEA decks passes only when the solved results pass acceptance.
function runPasses(run) { return run.assurance.overall_pass && (!run.fea || run.fea.acceptance?.overall_pass === true); }
function analyse(run) {
  const cfg = run.config, a = run.assurance;
  const path = run.route.found ? routePath(run.route.points, cfg.tube.minimum_bend_radius) : { segs: [], samples: [], length: 0 };
  const box = new THREE.Box3();
  run.meshes.forEach((m) => { box.expandByPoint(new THREE.Vector3(...m.bounds.min)); box.expandByPoint(new THREE.Vector3(...m.bounds.max)); });
  if (solvedFea(run)) run.fea.nodes.forEach((n) => box.expandByPoint(new THREE.Vector3(...n)));
  // Prefer the server's exact B-rep envelope (metrics.json); fall back to display bounds.
  const size = run.metrics ? new THREE.Vector3(...run.metrics.envelope_dimensions_mm) : box.getSize(new THREE.Vector3());
  const out = { path, envelope: { size, volumeL: size.x * size.y * size.z / 1e6, footprintM2: size.x * size.y / 1e6, exact: Boolean(run.metrics) } };
  if (solvedFea(run)) {
    const f = run.fea, allow = f.manifest.allowable_von_mises_mpa, limit = f.manifest.maximum_displacement_mm;
    const centroids = f.elements.map((e) => { const c = new THREE.Vector3(); for (let k = 0; k < 4; k++) c.add(new THREE.Vector3(...f.nodes[e[k]])); return c.multiplyScalar(0.25); });
    const where = centroids.map((c) => { let best = null, d = Infinity; for (const smp of path.samples) { const dd = smp.p.distanceToSquared(c); if (dd < d) { d = dd; best = smp; } } return best; });
    const cases = f.cases.map((c, ci) => {
      const acc = f.acceptance.load_cases[ci];
      let maxE = 0; c.stress.forEach((v, i) => { if (v > c.stress[maxE]) maxE = i; });
      const mags = c.displacement.map((u) => Math.hypot(u[0], u[1], u[2]));
      // critical regions: elements within 85% of this case's peak, grouped by route segment
      const peak = c.stress[maxE], groups = new Map();
      c.stress.forEach((v, i) => {
        if (v < 0.85 * peak) return;
        const seg = where[i].seg, g = groups.get(seg) || { seg, peak: 0, count: 0, elem: i, s: where[i].s };
        g.count++; if (v > g.peak) { g.peak = v; g.elem = i; g.s = where[i].s; }
        groups.set(seg, g);
      });
      const regions = [...groups.values()].sort((x, y) => y.peak - x.peak).map((g) => ({ ...g, segment: path.segs[g.seg], util: g.peak / allow }));
      return { name: c.name, acc, peak, peakElem: maxE, peakAt: centroids[maxE], util: peak / allow, maxDisp: Math.max(...mags), dispUtil: Math.max(...mags) / limit, mags, regions };
    });
    out.fea = { allow, limit, cases, worst: cases.reduce((w, c) => (c.util > w.util ? c : w), cases[0]) };
  }
  return out;
}
const RUNS = DATA.scenarios.map((run) => ({ ...run, derived: analyse(run) }));
let BASE = RUNS[0];

/* ---------- three.js scene ---------- */
const canvas = $("#canvas"), viewport = $("#viewport");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(40, 1, 1, 20000);
camera.up.set(0, 0, 1);
scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.75));
const key = new THREE.DirectionalLight(0xffffff, 0.65); key.position.set(0.6, -0.8, 1.2); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.25); fill.position.set(-1, 0.7, 0.3); scene.add(fill);
let world = new THREE.Group(); scene.add(world);
const orbit = { target: new THREE.Vector3(300, 200, 150), yaw: -0.9, pitch: 0.55, dist: 1100 };
function placeCamera() {
  const h = orbit.dist * Math.cos(orbit.pitch);
  camera.position.set(orbit.target.x + h * Math.cos(orbit.yaw), orbit.target.y + h * Math.sin(orbit.yaw), orbit.target.z + orbit.dist * Math.sin(orbit.pitch));
  camera.lookAt(orbit.target);
}
function resize() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  renderer.setSize(w, h, false); camera.aspect = w / Math.max(1, h); camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewport);

const parts = []; // {id, name, kind, color, mesh, props}
let tube = null, flowDots = [], pathCurve = null;
function themeColors() {
  scene.background = new THREE.Color(css("--view"));
  if (world.userData.grid) world.userData.grid.material.color.set(css("--grid"));
  world.traverse((o) => { if (o.userData.inkLine) o.material.color.set(css(o.userData.inkLine)); });
}

function geometryFrom(mesh) {
  const geom = new THREE.BufferGeometry();
  if (mesh.positions) {
    geom.setAttribute("position", new THREE.Float32BufferAttribute(mesh.positions, 3));
    geom.setAttribute("normal", new THREE.Float32BufferAttribute(mesh.normals, 3));
    return geom;
  }
  const g = DATA.geometries[mesh.geometry];
  geom.setAttribute("position", new THREE.Float32BufferAttribute(g.v.map((v) => v / 10), 3));
  geom.setAttribute("normal", new THREE.Float32BufferAttribute(g.n.map((v) => v / 100), 3));
  geom.setIndex(g.i);
  return geom;
}

function buildScene() {
  scene.remove(world); world.traverse((o) => { o.geometry && o.geometry.dispose(); o.material && o.material.dispose && o.material.dispose(); });
  world = new THREE.Group(); scene.add(world); parts.length = 0; tube = null; flowDots = [];
  const run = RUNS[state.run], cfg = run.config, d = run.derived;
  const [W, D, H] = cfg.workspace.dimensions;
  // floor grid in XY at z = 0, workspace frame
  const grid = new THREE.GridHelper(Math.max(W, D) * 1.6, 32); grid.rotation.x = Math.PI / 2; grid.position.set(W / 2, D / 2, 0);
  grid.material.transparent = true; grid.material.opacity = 0.7; world.add(grid); world.userData.grid = grid;
  const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(W, D, H)), new THREE.LineBasicMaterial({ transparent: true, opacity: 0.55 }));
  frame.position.set(W / 2, D / 2, H / 2); frame.userData.inkLine = "--faint"; world.add(frame);
  // parts
  let tris = 0;
  run.meshes.forEach((m) => {
    if (m.kind === "route" && solvedFea(run)) return; // the solved shell mesh carries the tube instead
    const geom = geometryFrom(m); tris += m.triangle_count;
    const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(...m.color), roughness: 0.55, metalness: 0.15, transparent: true, side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(geom, mat); mesh.userData.partId = m.id; world.add(mesh);
    parts.push({ id: m.id, name: m.name, kind: m.kind, color: m.color, mesh, props: m.properties, bounds: m.bounds });
  });
  // clearance envelopes: obstacles inflated by tube radius + clearance (what the router treats as solid)
  const infl = cfg.tube.outer_diameter / 2 + cfg.tube.minimum_clearance;
  const envGroup = new THREE.Group(); envGroup.userData.env = true;
  cfg.obstacles.forEach((o) => {
    let geom, pos = new THREE.Vector3(...o.center), rot = null;
    if (o.type === "box") geom = new THREE.BoxGeometry(o.size[0] + 2 * infl, o.size[1] + 2 * infl, o.size[2] + 2 * infl);
    else if (o.type === "cylinder") { geom = new THREE.CylinderGeometry(o.radius + infl, o.radius + infl, o.height + 2 * infl, 40, 1, true); rot = o.axis; }
    else return;
    const line = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 1), new THREE.LineDashedMaterial({ color: css("--fail"), dashSize: 6, gapSize: 4, transparent: true, opacity: 0.8 }));
    line.computeLineDistances(); line.position.copy(pos); line.userData.inkLine = "--fail";
    if (rot === "z") line.rotation.x = Math.PI / 2; else if (rot === "x") line.rotation.z = Math.PI / 2;
    envGroup.add(line);
  });
  world.add(envGroup); world.userData.envGroup = envGroup;
  // routed tube: CalculiX shell mesh (mid-surface), so stress and displacement map onto real nodes
  if (solvedFea(run)) {
    const f = run.fea, geom = new THREE.BufferGeometry(), idx = [];
    f.elements.forEach((e) => idx.push(e[0], e[1], e[2], e[0], e[2], e[3]));
    geom.setAttribute("position", new THREE.Float32BufferAttribute(f.nodes.flat(), 3));
    geom.setAttribute("color", new THREE.Float32BufferAttribute(new Float32Array(f.nodes.length * 3), 3));
    geom.setIndex(idx); geom.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.35, metalness: 0.25, side: THREE.DoubleSide, transparent: true });
    tube = new THREE.Mesh(geom, mat); tube.userData.partId = "routed-tube"; world.add(tube);
    tris += idx.length / 3;
    parts.push({ id: "routed-tube", name: "Routed tube", kind: "route", color: hexToRgb(css("--accent")), mesh: tube, props: {
      outer_diameter_mm: cfg.tube.outer_diameter, wall_thickness_mm: cfg.analysis?.wall_thickness, bend_radius_mm: cfg.tube.minimum_bend_radius,
      length_mm: +fmt(run.route.length), shell_elements: f.elements.length, material: cfg.analysis?.material?.name } });
  }
  // centreline + flow particles
  if (d.path.samples.length) {
    const pts = d.path.samples.map((s) => s.p);
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineDashedMaterial({ dashSize: 8, gapSize: 6, transparent: true, opacity: 0.7 }));
    line.computeLineDistances(); line.userData.inkLine = "--ink"; world.add(line);
    pathCurve = d.path;
    const dotGeom = new THREE.SphereGeometry(cfg.tube.outer_diameter * 0.22, 12, 8);
    for (let i = 0; i < 14; i++) { const m = new THREE.Mesh(dotGeom, new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.9, depthTest: false })); m.renderOrder = 5; flowDots.push(m); world.add(m); }
  } else pathCurve = null;
  $("#tri-count").textContent = `${tris.toLocaleString("en-GB")} triangles`;
  themeColors(); applyShading(); applyVisibility(); fit();
}

function hexToRgb(h) { const c = new THREE.Color(h); return [c.r, c.g, c.b]; }

function applyShading() {
  const run = RUNS[state.run], f = solvedFea(run) ? run.fea : null, legend = $("#legend");
  $("#deform-wrap").hidden = !(f && state.shade === "disp");
  if (!tube || !f) { legend.hidden = true; return; }
  const geom = tube.geometry, col = geom.attributes.color, pos = geom.attributes.position;
  const c = f.cases[state.caseIdx], dc = run.derived.fea.cases[state.caseIdx];
  const N = f.nodes.length;
  if (state.shade === "solid") {
    const a = hexToRgb(css("--accent"));
    for (let i = 0; i < N; i++) col.setXYZ(i, ...a);
  } else if (state.shade === "stress") {
    const sum = new Float32Array(N), cnt = new Float32Array(N);
    f.elements.forEach((e, ei) => { for (const n of e) { sum[n] += c.stress[ei]; cnt[n]++; } });
    const allow = run.derived.fea.allow;
    for (let i = 0; i < N; i++) col.setXYZ(i, ...ramp(cnt[i] ? sum[i] / cnt[i] / allow : 0));
  } else {
    const max = dc.maxDisp || 1;
    for (let i = 0; i < N; i++) col.setXYZ(i, ...ramp(dc.mags[i] / max));
  }
  const k = state.shade === "disp" ? state.deform : 0;
  for (let i = 0; i < N; i++) { const p = f.nodes[i], u = c.displacement[i]; pos.setXYZ(i, p[0] + u[0] * k, p[1] + u[1] * k, p[2] + u[2] * k); }
  pos.needsUpdate = true; col.needsUpdate = true; geom.computeVertexNormals();
  if (state.shade === "solid") legend.hidden = true;
  else {
    legend.hidden = false;
    const allow = run.derived.fea.allow;
    legend.innerHTML = state.shade === "stress"
      ? `<span class="label">von Mises / allowable · ${esc(c.name.replaceAll("_", " "))}</span><div class="ramp" style="background:${rampCss}"></div><div class="ends num"><span>0</span><span>${fmt(allow / 2, 0)}</span><span>${fmt(allow, 0)} MPa</span></div>`
      : `<span class="label">Displacement · ${esc(c.name.replaceAll("_", " "))}</span><div class="ramp" style="background:${rampCss}"></div><div class="ends num"><span>0</span><span>${fmt(dc.maxDisp, 3)} mm</span></div>`;
  }
}

function applyVisibility() {
  const xr = state.xray;
  parts.forEach((p) => {
    p.mesh.visible = !state.hidden.has(p.id);
    const isTube = p.kind === "route";
    p.mesh.material.opacity = xr && !isTube ? 0.18 : 1;
    p.mesh.material.depthWrite = !(xr && !isTube);
    const sel = p.id === state.selected;
    p.mesh.material.emissive = new THREE.Color(sel ? css("--accent") : "#000000");
    p.mesh.material.emissiveIntensity = sel ? 0.35 : 0;
  });
  if (world.userData.envGroup) world.userData.envGroup.visible = state.env;
  flowDots.forEach((m) => (m.visible = state.flow));
}

function fit() {
  const box = new THREE.Box3();
  parts.forEach((p) => { if (p.mesh.visible) box.expandByObject(p.mesh); });
  const cfg = RUNS[state.run].config; box.expandByPoint(new THREE.Vector3(0, 0, 0)); box.expandByPoint(new THREE.Vector3(...cfg.workspace.dimensions));
  orbit.target.copy(box.getCenter(new THREE.Vector3()));
  orbit.dist = box.getSize(new THREE.Vector3()).length() * 1.15;
}

/* hotspot pin: peak stress element for the selected load case */
const pinEl = document.createElement("div"); pinEl.className = "pin"; pinEl.innerHTML = "<span></span><i></i>"; $("#pins").append(pinEl);
function updatePin() {
  const d = RUNS[state.run]?.derived;
  if (!d || !d.fea || state.shade !== "stress") { pinEl.hidden = true; return; }
  const c = d.fea.cases[state.caseIdx], p = c.peakAt.clone().project(camera);
  if (p.z > 1) { pinEl.hidden = true; return; }
  pinEl.hidden = false;
  pinEl.style.left = `${(p.x + 1) / 2 * viewport.clientWidth}px`; pinEl.style.top = `${(1 - p.y) / 2 * viewport.clientHeight - 6}px`;
  pinEl.firstChild.textContent = `Peak ${fmt(c.peak, 0)} MPa · ${fmt(c.util * 100, 0)}%`;
}

let t0 = performance.now();
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
function frame(now) {
  placeCamera();
  if (pathCurve && state.flow && flowDots.length) {
    const L = pathCurve.length, smp = pathCurve.samples, speed = reduceMotion ? 0 : 0.09;
    flowDots.forEach((m, i) => {
      const s = ((((now - t0) / 1000) * speed * L + (i / flowDots.length) * L) % L);
      let lo = 0, hi = smp.length - 1; while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (smp[mid].s < s) lo = mid; else hi = mid; }
      const a = smp[lo], b = smp[hi], f = (s - a.s) / Math.max(1e-6, b.s - a.s);
      m.position.copy(a.p).lerp(b.p, f);
    });
  }
  renderer.render(scene, camera); updatePin();
  requestAnimationFrame(frame);
}

/* pointer controls + picking */
let drag = null;
canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, moved: false, pan: e.shiftKey || e.button === 2 }; canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
  if (drag.pan) {
    const s = orbit.dist * 0.0012, right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
    orbit.target.addScaledVector(right, -dx * s).addScaledVector(up, dy * s);
  } else { orbit.yaw -= dx * 0.008; orbit.pitch = Math.max(-1.5, Math.min(1.5, orbit.pitch + dy * 0.008)); }
  drag.x = e.clientX; drag.y = e.clientY;
});
canvas.addEventListener("pointerup", (e) => {
  if (drag && !drag.moved) {
    const r = canvas.getBoundingClientRect(), ray = new THREE.Raycaster();
    ray.setFromCamera({ x: (e.clientX - r.left) / r.width * 2 - 1, y: 1 - (e.clientY - r.top) / r.height * 2 }, camera);
    const hit = ray.intersectObjects(parts.filter((p) => p.mesh.visible).map((p) => p.mesh))[0];
    select(hit ? hit.object.userData.partId : null);
  }
  drag = null;
});
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("wheel", (e) => { e.preventDefault(); orbit.dist = Math.max(40, orbit.dist * Math.exp(e.deltaY * 0.001)); }, { passive: false });

function setView(v) {
  const views = { iso: [-0.9, 0.55], top: [-Math.PI / 2, 1.5], front: [-Math.PI / 2, 0.02], right: [0, 0.02] };
  [orbit.yaw, orbit.pitch] = views[v];
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
}

/* ---------- UI: scenarios, KPIs, tree, tabs ---------- */
function renderScenarios() {
  $("#scenarios").innerHTML = RUNS.map((r, i) => `<button data-run="${i}" aria-pressed="${i === state.run}" title="${esc(r.note)}"><i class="dot ${r.assurance.overall_pass ? "pass" : "fail"}"></i>${esc(r.name)}</button>`).join("");
  document.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", () => setRun(+b.dataset.run)));
}

function delta(v, base, unit, digits = 1, lowerIsBetter = true) {
  if (v == null || base == null || RUNS[state.run] === BASE) return "";
  const d = v - base; if (Math.abs(d) < 1e-9) return `<span>same as Baseline</span>`;
  const good = lowerIsBetter ? d < 0 : d > 0;
  return `<span class="${good ? "delta-good" : "delta-bad"}">${d > 0 ? "+" : "−"}${fmt(Math.abs(d), digits)}${unit} vs Baseline</span>`;
}
function utilColor(u) { return u > 1 + 1e-9 ? "var(--fail)" : u >= 0.8 ? "var(--warn)" : "var(--pass)"; }

function renderKpis() {
  const run = RUNS[state.run], a = run.assurance, d = run.derived, bd = BASE.derived;
  const worst = d.fea?.worst;
  const fea = run.fea?.acceptance;
  const feaText = !run.fea ? "FEA not run" : !run.fea.acceptance ? "FEA not solved" : run.fea.acceptance.overall_pass ? "FEA pass" : "FEA fail";
  const clearMargin = a.minimum_measured_clearance_mm != null ? a.minimum_measured_clearance_mm - a.required_clearance_mm : null;
  const env = d.envelope, ratio = env.volumeL / bd.envelope.volumeL;
  const tiles = [
    `<div class="kpi"><span class="label">Verdict</span><div class="value"><span class="verdict ${runPasses(run) ? "pass" : "fail"}">${runPasses(run) ? "PASS" : "FAIL"}</span></div><div class="sub">Geometry ${a.overall_pass ? "pass" : "fail"} · ${feaText}</div></div>`,
    `<div class="kpi"><span class="label">Main piping length</span><div class="value">${a.route_found ? fmt(a.route_length_mm, 0) : "–"}<small>mm</small></div><div class="sub">${a.route_found ? delta(a.route_length_mm, BASE.assurance.route_length_mm, " mm", 0) || `${d.path.segs.filter((s) => s.kind === "Straight").length} straights, ${a.number_of_bends} bends` : "No route found"}</div></div>`,
    `<div class="kpi"><span class="label">Bends</span><div class="value">${a.route_found ? a.number_of_bends : "–"}</div><div class="sub">R ${fmt(a.required_bend_radius_mm, 0)} mm minimum ${a.minimum_generated_bend_radius_mm != null ? "· met" : ""}</div></div>`,
    `<div class="kpi"><span class="label">Min. clearance</span><div class="value">${fmt(a.minimum_measured_clearance_mm, 1)}<small>/ ${fmt(a.required_clearance_mm, 0)} mm</small></div><div class="sub">${clearMargin == null ? "Not measured" : clearMargin === 0 ? "Exactly at the limit" : `+${fmt(clearMargin, 1)} mm margin`}</div><div class="bar"><i style="width:${clearMargin == null ? 0 : Math.min(100, a.required_clearance_mm / a.minimum_measured_clearance_mm * 100)}%;background:${clearMargin == null ? "var(--line)" : utilColor(a.required_clearance_mm / a.minimum_measured_clearance_mm)}"></i></div></div>`,
    `<div class="kpi"><span class="label">Peak stress</span><div class="value">${worst ? fmt(worst.util * 100, 0) : "–"}<small>${worst ? "% of allowable" : ""}</small></div><div class="sub">${worst ? `${fmt(worst.peak, 1)} MPa · ${esc(worst.name.replaceAll("_", " "))}` : "No FEA for this run"}</div><div class="bar"><i style="width:${worst ? Math.min(100, worst.util * 100) : 0}%;background:${worst ? utilColor(worst.util) : "var(--line)"}"></i></div></div>`,
    `<div class="kpi"><span class="label">Package envelope</span><div class="value">${fmt(env.volumeL, 1)}<small>L</small></div><div class="sub">${fmt(env.size.x, 0)} × ${fmt(env.size.y, 0)} × ${fmt(env.size.z, 0)} mm${run === BASE || !a.route_found ? "" : ` · ratio ${fmt(ratio, 3)}`}</div></div>`,
  ];
  $("#kpis").innerHTML = tiles.join("");
}

function renderTree() {
  $("#tree").innerHTML = parts.map((p) => `<li data-part="${p.id}" aria-selected="${p.id === state.selected}"><input type="checkbox" id="vis-${p.id}" aria-label="Show ${esc(p.name)}" ${state.hidden.has(p.id) ? "" : "checked"}><i class="swatch" style="background:rgb(${p.color.map((v) => Math.round(v * 255)).join(",")})"></i><span><strong>${esc(p.name)}</strong><em>${p.kind === "route" ? "routed line" : p.kind}</em></span></li>`).join("");
  document.querySelectorAll("#tree li").forEach((li) => {
    const id = li.dataset.part, box = li.querySelector("input");
    box.addEventListener("click", (e) => e.stopPropagation());
    box.addEventListener("change", () => { box.checked ? state.hidden.delete(id) : state.hidden.add(id); applyVisibility(); });
    li.addEventListener("click", () => select(id));
  });
}

function select(id) {
  state.selected = id; applyVisibility(); renderTree();
  const p = parts.find((x) => x.id === id), el = $("#props");
  if (!p) { el.innerHTML = `<span class="label">Selection</span><h3>Nothing selected</h3><p class="note">Click a part in the model or in this list.</p>`; return; }
  const rows = Object.entries(p.props || {}).filter(([k, v]) => v != null && typeof v !== "object" || Array.isArray(v)).map(([k, v]) => `<dt>${esc(k.replaceAll("_", " "))}</dt><dd>${esc(Array.isArray(v) ? v.join(", ") : v)}</dd>`).join("");
  const b = p.bounds ? `<dt>bounds min</dt><dd>${p.bounds.min.map((v) => fmt(v, 0)).join(", ")}</dd><dt>bounds max</dt><dd>${p.bounds.max.map((v) => fmt(v, 0)).join(", ")}</dd>` : "";
  el.innerHTML = `<span class="label">Selection · ${esc(p.kind)}</span><h3>${esc(p.name)}</h3><dl class="kv">${rows}${b}</dl>`;
}

/* ---------- tabs ---------- */
const TABS = {
  assurance(run) {
    const a = run.assurance, f = run.fea?.acceptance;
    const items = [
      ["Route found", a.route_found, a.route_found ? `${fmt(a.route_length_mm, 1)} mm, ${a.number_of_bends} bends` : "A* found no feasible path", null],
      ["Collision-free (exact B-rep)", a.collision_free, a.collision_free ? "No solid intersection" : "Not verified", null],
      ["Clearance", a.minimum_measured_clearance_mm != null && a.minimum_measured_clearance_mm >= a.required_clearance_mm, `${fmt(a.minimum_measured_clearance_mm, 1)} mm measured, ${fmt(a.required_clearance_mm, 1)} mm required`, a.minimum_measured_clearance_mm ? a.required_clearance_mm / a.minimum_measured_clearance_mm : null],
      ["Bend radius", a.minimum_generated_bend_radius_mm != null && a.minimum_generated_bend_radius_mm >= a.required_bend_radius_mm, `${fmt(a.minimum_generated_bend_radius_mm, 1)} mm generated, ${fmt(a.required_bend_radius_mm, 1)} mm minimum`, a.minimum_generated_bend_radius_mm ? a.required_bend_radius_mm / a.minimum_generated_bend_radius_mm : null],
      ["Start port alignment", a.start_port_alignment, "Tube leaves along the nozzle axis", null],
      ["End port alignment", a.end_port_alignment, "Tube enters along the nozzle axis", null],
    ];
    if (f) f.load_cases.forEach((c) => items.push([`FEA · ${c.name.replaceAll("_", " ")}`, c.passed, `${fmt(c.maximum_von_mises_mpa, 1)} / ${fmt(c.allowable_von_mises_mpa, 1)} MPa, ${fmt(c.maximum_displacement_mm, 3)} / ${fmt(c.allowed_displacement_mm, 1)} mm`, c.maximum_von_mises_mpa / c.allowable_von_mises_mpa]));
    const reasons = [...a.failure_reasons, ...(f?.failure_reasons || [])];
    return `<div class="section-title"><h3>Hard constraints</h3><span class="note num">${fmt(a.execution_time_seconds, 2)} s run</span></div>
      ${reasons.length ? reasons.map((r) => `<div class="reason">${esc(r[0].toUpperCase() + r.slice(1))}.</div>`).join("") : ""}
      <ul class="checks">${items.map(([name, ok, detail, u]) => `<li class="check ${ok ? "ok" : "bad"}"><span class="ico">${ok ? "✓" : "✕"}</span><span><b>${esc(name)}</b><small>${esc(detail)}</small></span>${u != null ? `<span class="meter" title="utilisation ${fmt(u * 100, 0)}%"><i style="width:${Math.min(100, u * 100)}%;background:${utilColor(u)}"></i></span>` : "<span></span>"}</li>`).join("")}</ul>
      <p class="note">Every check is deterministic and runs on the exact CAD geometry. The preview never counts as evidence.</p>
      ${run.downloads?.length ? `<div class="section-title"><h3>Files</h3><span class="note">latest run only</span></div><div class="row">${run.downloads.map((f) => `<a class="btn" href="${esc(f.url)}" download="${esc(f.name)}">${esc(f.name)}</a>`).join("")}${run.downloads.some((f) => f.name === "layout.dxf") ? `<button class="btn" data-open-layout title="Open the plan and elevations in the drawing viewer">Open layout in Drawings</button>` : ""}</div>` : ""}`;
  },
  fea(run) {
    const d = run.derived.fea;
    if (!d && run.fea) return `<div class="reason">${esc(run.fea.diagnostic || "CalculiX results are not available")}.</div><p class="note">The decks were written to the run's fea folder. Install CalculiX on the server to solve them here, or solve them yourself and run <code>syntera fea-evaluate</code>.</p>`;
    if (!d) return `<div class="reason">No tube was generated, so there is nothing to analyse.</div><p class="note">FEA decks are written only after the route passes assurance.</p>`;
    const m = run.fea.manifest, cfg = run.config.analysis;
    const caseRows = d.cases.map((c, i) => `<tr class="clickable ${i === state.caseIdx ? "hot" : ""}" data-case="${i}"><td>${esc(c.name.replaceAll("_", " "))}</td><td><div class="util"><span class="track"><i style="width:${Math.min(100, c.util * 100)}%;background:${utilColor(c.util)}"></i></span><span class="num">${fmt(c.util * 100, 0)}%</span></div></td><td class="r">${fmt(c.peak, 1)}</td><td class="r">${fmt(c.maxDisp, 3)}</td></tr>`).join("");
    const cur = d.cases[state.caseIdx];
    const regions = cur.regions.slice(0, 6).map((g) => `<tr class="clickable" data-region-elem="${g.elem}"><td>${g.segment.kind} ${g.segment.no}</td><td class="r">${fmt(g.s, 0)}</td><td class="r">${g.count}</td><td class="r">${fmt(g.peak, 1)}</td><td><span class="chip ${g.util > 1 ? "fail" : g.util >= 0.8 ? "warn" : "pass"}">${fmt(g.util * 100, 0)}%</span></td></tr>`).join("");
    return `<div class="section-title"><h3>Load cases</h3><span class="note">allowable ${fmt(d.allow, 1)} MPa</span></div>
      <div class="table-wrap"><table><thead><tr><th>Case</th><th>Utilisation</th><th class="r">σ<sub>vM</sub> MPa</th><th class="r">u mm</th></tr></thead><tbody>${caseRows}</tbody></table></div>
      <div class="section-title"><h3>Critical regions</h3><span class="note">${esc(cur.name.replaceAll("_", " "))}</span></div>
      <p class="note">Elements within 85% of the peak, grouped by route segment. Distance is measured along the centreline from the start port.</p>
      <div class="table-wrap"><table><thead><tr><th>Segment</th><th class="r">at mm</th><th class="r">elems</th><th class="r">peak MPa</th><th>Use</th></tr></thead><tbody>${regions}</tbody></table></div>
      <div class="spec"><section><h4>Model</h4><dl class="kv">
        <dt>Elements</dt><dd>${m.mesh.elements.toLocaleString("en-GB")} S8R shells</dd><dt>Nodes</dt><dd>${m.mesh.nodes.toLocaleString("en-GB")}</dd>
        <dt>Ring × axial</dt><dd>${m.mesh.circumferential_elements} × ${m.mesh.axial_elements}</dd><dt>Max aspect ratio</dt><dd>${fmt(m.mesh.maximum_aspect_ratio, 2)}</dd>
        <dt>Material</dt><dd>${esc(cfg.material.name)}, σy ${cfg.material.yield_strength_mpa} MPa</dd><dt>Safety factor</dt><dd>${cfg.acceptance.stress_safety_factor}</dd>
        <dt>Solver</dt><dd>CalculiX 2.21, both ends clamped</dd><dt>Deck input SHA-256</dt><dd>${m.input_sha256.slice(0, 16)}…</dd></dl></section></div>
      <p class="note">Mesh convergence is not automated yet (roadmap M12), so treat peaks at the clamped ends as conservative.</p>`;
  },
  route(run) {
    const d = run.derived;
    if (!run.route.found) return `<div class="reason">${esc(run.route.diagnostic || run.assurance.failure_reasons.join("; ") || "No route")}</div><p class="note">The dashed red boxes in the model show each obstacle grown by tube radius plus clearance. With ${fmt(run.config.tube.minimum_clearance, 0)} mm clearance they swallow a port, so no path can start.</p>`;
    const rows = d.path.segs.map((s) => `<tr><td>${s.kind} ${s.no}</td><td class="r">${fmt(s.s0, 0)}</td><td class="r">${fmt(s.length, 1)}</td><td class="r">${[s.from, s.to].map((p) => `${fmt(p.x, 0)},${fmt(p.y, 0)},${fmt(p.z, 0)}`).join(" → ")}</td></tr>`).join("");
    const straight = d.path.segs.filter((s) => s.kind === "Straight").reduce((t, s) => t + s.length, 0);
    return `<div class="section-title"><h3>Elevation along the route</h3><span class="note">z vs distance</span></div>${profileSvg(d.path)}
      <div class="spec"><section><dl class="kv"><dt>Total centreline</dt><dd>${fmt(d.path.length, 1)} mm</dd><dt>Straight / bent</dt><dd>${fmt(straight, 0)} / ${fmt(d.path.length - straight, 0)} mm</dd><dt>A* nodes expanded</dt><dd>${run.route.expanded_nodes.toLocaleString("en-GB")}</dd><dt>Voxel size</dt><dd>${run.config.voxel_resolution} mm</dd></dl></section></div>
      <div class="table-wrap"><table><thead><tr><th>Segment</th><th class="r">from mm</th><th class="r">length</th><th class="r">x,y,z → x,y,z</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  },
  compare() {
    const rows = RUNS.map((r, i) => {
      const a = r.assurance, w = r.derived.fea?.worst, ok = runPasses(r);
      const dl = a.route_found && i ? a.route_length_mm - BASE.assurance.route_length_mm : null;
      return `<tr class="clickable ${i === state.run ? "hot" : ""}" data-goto="${i}"><td><b>${esc(r.name)}</b><br><span class="note">${esc(r.note)}</span></td><td><span class="chip ${ok ? "pass" : "fail"}">${ok ? "pass" : "fail"}</span></td><td class="r">${a.route_found ? fmt(a.route_length_mm, 0) : "–"}${dl ? `<br><span class="${dl < 0 ? "delta-good" : "delta-bad"}">${dl > 0 ? "+" : "−"}${fmt(Math.abs(dl), 0)}</span>` : ""}</td><td class="r">${a.route_found ? a.number_of_bends : "–"}</td><td class="r">${fmt(a.minimum_measured_clearance_mm, 0)}</td><td class="r">${w ? fmt(w.util * 100, 0) + "%" : "–"}</td></tr>`;
    }).join("");
    const lens = RUNS.filter((r) => r.route.found), max = Math.max(...lens.map((r) => r.route.length));
    const bars = lens.map((r) => `<div style="display:grid;grid-template-columns:110px 1fr 54px;gap:8px;align-items:center;font-size:12px"><span>${esc(r.name)}</span><span style="height:10px;border-radius:2px;background:var(--line);position:relative"><i style="position:absolute;inset:0 auto 0 0;width:${r.route.length / max * 100}%;background:${r === RUNS[state.run] ? "var(--accent)" : "var(--faint)"};border-radius:2px"></i></span><span class="num" style="text-align:right">${fmt(r.route.length, 0)}</span></div>`).join("");
    return `<div class="section-title"><h3>Main piping length</h3><span class="note">mm</span></div><div style="display:grid;gap:6px">${bars}</div>
      <div class="table-wrap"><table><thead><tr><th>Run</th><th></th><th class="r">Length</th><th class="r">Bends</th><th class="r">Clr</th><th class="r">σ use</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">The envelope is the bounding box of modelled parts. It feeds the KPI 2 size ratio once a vendor baseline skid is loaded (roadmap M2).</p>`;
  },
  spec(run) {
    if (LIVE) return specForm(run);
    const c = run.config, a = c.analysis;
    const dir = (v) => ({ "1,0,0": "+X", "-1,0,0": "−X", "0,1,0": "+Y", "0,-1,0": "−Y", "0,0,1": "+Z", "0,0,-1": "−Z" }[v.join(",")] || v.join(","));
    return `<p class="note">Inputs for this run. Editing and re-routing needs the Syntera server; this page is a recorded snapshot.</p><div class="spec">
      <section><h4>Tube</h4><dl class="kv"><dt>Outer diameter</dt><dd>${c.tube.outer_diameter} mm</dd><dt>Min. bend radius</dt><dd>${c.tube.minimum_bend_radius} mm</dd><dt>Min. clearance</dt><dd>${c.tube.minimum_clearance} mm</dd>${a ? `<dt>Wall</dt><dd>${a.wall_thickness} mm</dd>` : ""}</dl></section>
      <section><h4>Nozzles</h4><dl class="kv"><dt>Start</dt><dd>${c.start_port.position.join(", ")} · ${dir(c.start_port.direction)}</dd><dt>End</dt><dd>${c.end_port.position.join(", ")} · ${dir(c.end_port.direction)}</dd></dl></section>
      <section><h4>Workspace and search</h4><dl class="kv"><dt>Frame</dt><dd>${c.workspace.dimensions.join(" × ")} mm</dd><dt>Voxel</dt><dd>${c.voxel_resolution} mm</dd><dt>Bend weight</dt><dd>${c.routing_cost_weights.bend}</dd><dt>Proximity weight</dt><dd>${c.routing_cost_weights.obstacle_proximity}</dd></dl></section>
      <section><h4>Equipment</h4><dl class="kv">${c.obstacles.map((o) => `<dt>${esc(o.name)}</dt><dd>${o.type === "box" ? `box ${o.size.join("×")}` : o.type === "cylinder" ? `cyl ⌀${o.radius * 2}×${o.height}` : "STEP"} @ ${o.center ? o.center.join(",") : ""}</dd>`).join("")}</dl></section>
      ${a ? `<section><h4>Load cases</h4><dl class="kv">${a.load_cases.map((l) => `<dt>${esc(l.name.replaceAll("_", " "))}</dt><dd>${l.internal_pressure_mpa ? `${l.internal_pressure_mpa} MPa internal` : l.gravity_mm_per_s2 ? "1 g self-weight" : l.temperature_change_k ? `ΔT ${l.temperature_change_k} K` : ""}</dd>`).join("")}</dl></section>` : ""}
    </div>`;
  },
  review(run) {
    const log = store.get("syntera.review", []);
    const running = reviewStart != null;
    return `<div class="section-title"><h3>Review timer</h3><span class="note">feeds KPI 1</span></div>
      <p class="note">KPI 1 counts review and correction time against the manual workflow. Start the timer when you begin checking this run and record your decision.</p>
      <div class="timer"><span class="num" id="timer">${clock(running ? Date.now() - reviewStart : 0)}</span><button class="btn ${running ? "" : "primary"}" id="timer-btn">${running ? "Pause" : "Start review"}</button></div>
      <div class="signoff" style="display:grid;gap:8px"><label class="label" for="rev-name">Reviewer</label><input type="text" id="rev-name" value="${esc(store.get("syntera.reviewer", ""))}" placeholder="Name">
      <label class="label" for="rev-notes">Notes</label><textarea id="rev-notes" placeholder="What did you check or change?"></textarea>
      <div class="row"><button class="btn primary" data-decide="Approved">Approve ${esc(run.name)}</button><button class="btn" data-decide="Changes requested">Request changes</button></div></div>
      <div class="section-title"><h3>Decisions</h3><span class="note">saved in this browser only</span></div>
      <ul class="log">${log.length ? log.slice().reverse().map((l) => `<li><b>${esc(l.decision)}</b> · ${esc(l.run)} · ${clock(l.ms)} review<br><span class="note">${esc(l.reviewer || "Unnamed")} · ${esc(l.at)}${l.notes ? ` · ${esc(l.notes)}` : ""}</span></li>`).join("") : `<li class="note">No decisions recorded yet.</li>`}</ul>`;
  },
};

const AXES = { "+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0], "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1] };
const axisName = (v) => Object.keys(AXES).find((k) => AXES[k].every((x, i) => x === v[i])) || "+X";
function specForm(run) {
  const c = state.draft || run?.config;
  if (!c) return `<p class="note">Loading the server configuration…</p>`;
  const num = (id, label, value, unit) => `<label class="field" for="${id}"><span class="label">${label}</span><span class="input-unit"><input id="${id}" type="number" step="any" value="${value}"><em>${unit}</em></span></label>`;
  const port = (key, title) => `<section><h4>${title}</h4><div class="xyz">${["x", "y", "z"].map((ax, i) => num(`${key}-${ax}`, ax.toUpperCase(), c[`${key}_port`].position[i], "mm")).join("")}</div>
    <label class="field" for="${key}-dir"><span class="label">Direction</span><select id="${key}-dir">${Object.keys(AXES).map((k) => `<option ${k === axisName(c[`${key}_port`].direction) ? "selected" : ""}>${k}</option>`).join("")}</select></label></section>`;
  return `<p class="note">Edit the inputs and generate a new run. Every run stays in the bar at the top for comparison.</p>
    <form class="spec" id="spec-form">
      <section><h4>Tube</h4><div class="fields">${num("od", "Outer diameter", c.tube.outer_diameter, "mm")}${num("bend", "Min. bend radius", c.tube.minimum_bend_radius, "mm")}${num("clear", "Min. clearance", c.tube.minimum_clearance, "mm")}${num("voxel", "Voxel size", c.voxel_resolution, "mm")}</div></section>
      ${port("start", "Start nozzle")}${port("end", "End nozzle")}
      <section><h4>Equipment (JSON)</h4><textarea id="obstacles" spellcheck="false" rows="10">${esc(JSON.stringify(c.obstacles, null, 2))}</textarea></section>
      <div class="row"><button class="btn primary" type="submit" id="spec-run">Generate route</button><button class="btn" type="button" id="spec-reset">Reset to server config</button></div>
      <p class="note" id="spec-error" role="alert"></p>
    </form>`;
}
function readForm() {
  const c = structuredClone(state.draft || RUNS[state.run].config), v = (id) => Number($(`#${id}`).value);
  c.tube.outer_diameter = v("od"); c.tube.minimum_bend_radius = v("bend"); c.tube.minimum_clearance = v("clear"); c.voxel_resolution = v("voxel");
  for (const key of ["start", "end"]) {
    c[`${key}_port`].position = ["x", "y", "z"].map((ax) => v(`${key}-${ax}`));
    c[`${key}_port`].direction = AXES[$(`#${key}-dir`).value];
  }
  c.obstacles = JSON.parse($("#obstacles").value);
  return c;
}
function describeChanges(c, ref) {
  if (!ref) return "Server configuration";
  const out = [];
  if (c.tube.outer_diameter !== ref.tube.outer_diameter) out.push(`OD ${c.tube.outer_diameter} mm`);
  if (c.tube.minimum_clearance !== ref.tube.minimum_clearance) out.push(`clearance ${c.tube.minimum_clearance} mm`);
  if (c.tube.minimum_bend_radius !== ref.tube.minimum_bend_radius) out.push(`bend R ${c.tube.minimum_bend_radius} mm`);
  if (c.voxel_resolution !== ref.voxel_resolution) out.push(`voxel ${c.voxel_resolution} mm`);
  for (const key of ["start", "end"]) if (JSON.stringify(c[`${key}_port`]) !== JSON.stringify(ref[`${key}_port`])) out.push(`${key} nozzle moved`);
  if (JSON.stringify(c.obstacles) !== JSON.stringify(ref.obstacles)) out.push("equipment edited");
  return out.length ? out.join(", ") : "Same inputs as the server configuration";
}
async function api(url, body) {
  const response = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(Array.isArray(payload.detail) ? payload.detail.map((d) => `${d.loc?.slice(1).join(".")}: ${d.msg}`).join("; ") : payload.detail || `Request failed (${response.status})`);
  return payload;
}
let serverConfig = null, busy = false;
async function generate(config) {
  if (busy) return;
  busy = true; setBusy(true);
  try {
    const payload = await api("/api/route", config);
    const run = { name: `Run ${RUNS.length + 1}`, note: describeChanges(config, serverConfig), generated: new Date().toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }), ...payload };
    run.derived = analyse(run);
    RUNS.push(run); BASE = RUNS[0]; state.draft = null;
    setRun(RUNS.length - 1);
  } catch (error) {
    const el = $("#spec-error"); if (el) el.textContent = error.message;
    $("#mode-chip span").textContent = error.message;
  } finally { busy = false; setBusy(false); }
}
function setBusy(on) {
  $("#run-btn").disabled = on; const b = $("#spec-run"); if (b) b.disabled = on;
  $("#busy").hidden = !on;
  if (LIVE) $("#mode-chip span").textContent = on ? "Routing, verifying and solving…" : `Live · ${RUNS.length} run${RUNS.length === 1 ? "" : "s"} this session`;
}

let reviewStart = null, reviewAccum = 0;
const clock = (ms) => { const s = Math.floor(ms / 1000); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
setInterval(() => { const t = $("#timer"); if (t && reviewStart != null) t.textContent = clock(reviewAccum + Date.now() - reviewStart); }, 500);

function profileSvg(path) {
  const W = 340, H = 110, pad = { l: 34, r: 8, t: 8, b: 22 };
  const zs = path.samples.map((s) => s.p.z), zMin = Math.min(...zs, 0), zMax = Math.max(...zs) + 20, L = path.length;
  const x = (s) => pad.l + s / L * (W - pad.l - pad.r), y = (z) => H - pad.b - (z - zMin) / (zMax - zMin) * (H - pad.t - pad.b);
  const d = path.samples.map((s, i) => `${i ? "L" : "M"}${x(s.s).toFixed(1)},${y(s.p.z).toFixed(1)}`).join("");
  const ticksX = [0, L / 2, L].map((s) => `<text x="${x(s)}" y="${H - 6}" text-anchor="middle">${fmt(s, 0)}</text>`).join("");
  const ticksY = [zMin, (zMin + zMax) / 2, zMax].map((z) => `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(z)}" y2="${y(z)}" stroke="var(--line)"/><text x="${pad.l - 4}" y="${y(z) + 3}" text-anchor="end">${fmt(z, 0)}</text>`).join("");
  const bends = path.segs.filter((s) => s.kind === "Bend").map((s) => `<rect x="${x(s.s0)}" y="${pad.t}" width="${Math.max(1, x(s.s0 + s.length) - x(s.s0))}" height="${H - pad.t - pad.b}" fill="var(--accent-soft)"/>`).join("");
  return `<svg class="profile" viewBox="0 0 ${W} ${H}" role="img" aria-label="Route elevation profile" style="font:10px var(--font-mono);fill:var(--muted)">${bends}${ticksY}<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2"/>${ticksX}</svg><p class="note">Shaded bands are bends.</p>`;
}

/* Hand the latest run's layout DXF to the drawing viewer through its ordinary upload route. */
async function openLayout(button) {
  button.disabled = true;
  try {
    const dxf = await fetch("/api/download/layout.dxf");
    if (!dxf.ok) throw new Error(`layout download failed (${dxf.status})`);
    const r = await fetch("/api/drawings?filename=syntera-layout.dxf", {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: await dxf.blob(),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || r.statusText);
    location.href = `/drawings#${body.drawing_id}`;
  } catch (error) {
    button.disabled = false;
    button.textContent = `Could not open layout: ${error.message}`;
  }
}

function renderTab() {
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
  const run = RUNS[state.run];
  if (!run && state.tab !== "spec") { $("#tabpanel").innerHTML = `<p class="note">Waiting for the first run…</p>`; return; }
  $("#tabpanel").innerHTML = TABS[state.tab](run);
  document.querySelectorAll("[data-case]").forEach((r) => r.addEventListener("click", () => setCase(+r.dataset.case)));
  document.querySelectorAll("[data-goto]").forEach((r) => r.addEventListener("click", () => setRun(+r.dataset.goto)));
  document.querySelectorAll("[data-open-layout]").forEach((b) => b.addEventListener("click", () => openLayout(b)));
  document.querySelectorAll("[data-region-elem]").forEach((r) => r.addEventListener("click", () => {
    const f = run.fea, e = f.elements[+r.dataset.regionElem], c = new THREE.Vector3();
    for (let k = 0; k < 4; k++) c.add(new THREE.Vector3(...f.nodes[e[k]]));
    orbit.target.copy(c.multiplyScalar(0.25)); orbit.dist = 260; setShade("stress");
  }));
  const form = $("#spec-form");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      try { state.draft = readForm(); generate(state.draft); } catch (error) { $("#spec-error").textContent = `Equipment JSON is not valid: ${error.message}`; }
    });
    form.addEventListener("input", () => { try { state.draft = readForm(); } catch { /* keep the last valid draft while typing */ } });
    $("#spec-reset").addEventListener("click", () => { state.draft = structuredClone(serverConfig); renderTab(); });
  }
  const tb = $("#timer-btn");
  if (tb) tb.addEventListener("click", () => { if (reviewStart == null) reviewStart = Date.now(); else { reviewAccum += Date.now() - reviewStart; reviewStart = null; } renderTab(); });
  document.querySelectorAll("[data-decide]").forEach((b) => b.addEventListener("click", () => {
    const ms = reviewAccum + (reviewStart != null ? Date.now() - reviewStart : 0), name = $("#rev-name").value.trim();
    store.set("syntera.reviewer", name);
    const log = store.get("syntera.review", []);
    log.push({ decision: b.dataset.decide, run: run.name, ms, reviewer: name, notes: $("#rev-notes").value.trim(), at: new Date().toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) });
    store.set("syntera.review", log.slice(-30));
    reviewStart = null; reviewAccum = 0; renderTab();
  }));
}

function renderCases() {
  const run = RUNS[state.run], sel = $("#case-select");
  const cases = solvedFea(run) ? run.fea.cases : [];
  $("#case-seg").hidden = !cases.length;
  sel.innerHTML = cases.map((c, i) => `<option value="${i}" ${i === state.caseIdx ? "selected" : ""}>${esc(c.name.replaceAll("_", " "))}</option>`).join("");
  document.querySelectorAll("[data-shade]").forEach((b) => { b.disabled = b.dataset.shade !== "solid" && !cases.length; });
}

function renderTitleBlock() {
  const run = RUNS[state.run], m = run.fea?.manifest;
  $("#titleblock").innerHTML = `<div><b>Title</b><span>${esc(run.config.title)}</span></div><div><b>Run</b><span>${esc(run.name)} (${state.run + 1}/${RUNS.length})</span></div><div><b>Units</b><span>mm · N · MPa</span></div><div><b>Deck SHA-256</b><span>${m ? m.input_sha256.slice(0, 12) : "–"}</span></div><div><b>Generated</b><span>${esc(run.generated || DATA.generated)}</span></div><div><b>Data</b><span>Synthetic</span></div>`;
}

function setRun(i) {
  if (!RUNS.length) return;
  state.run = (i + RUNS.length) % RUNS.length; state.selected = null;
  if (!solvedFea(RUNS[state.run]) && state.shade !== "solid") state.shade = "solid";
  const dfea = RUNS[state.run].derived.fea;
  state.caseIdx = dfea ? dfea.cases.indexOf(dfea.worst) : 0;
  document.querySelectorAll("[data-shade]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.shade === state.shade)));
  renderScenarios(); buildScene(); renderCases(); renderKpis(); renderTree(); select(null); renderTab(); renderTitleBlock();
}
function setShade(s) {
  if (s !== "solid" && !solvedFea(RUNS[state.run])) return;
  state.shade = s; document.querySelectorAll("[data-shade]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.shade === s))); applyShading();
}
function setCase(i) { state.caseIdx = i; renderCases(); applyShading(); renderTab(); }
function toggle(key, btn) { state[key] = !state[key]; $(btn).setAttribute("aria-pressed", String(state[key])); applyVisibility(); }

document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.querySelectorAll("[data-shade]").forEach((b) => b.addEventListener("click", () => setShade(b.dataset.shade)));
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => { state.tab = b.dataset.tab; store.set("syntera.tab", state.tab); renderTab(); }));
$("#fit").addEventListener("click", fit);
$("#case-select").addEventListener("change", (e) => setCase(+e.target.value));
$("#tg-xray").addEventListener("click", () => toggle("xray", "#tg-xray"));
$("#tg-env").addEventListener("click", () => toggle("env", "#tg-env"));
$("#tg-flow").addEventListener("click", () => toggle("flow", "#tg-flow"));
$("#show-all").addEventListener("click", () => { state.hidden.clear(); applyVisibility(); renderTree(); });
$("#deform").addEventListener("input", (e) => { state.deform = +e.target.value; $("#deform-val").textContent = state.deform; applyShading(); });
$("#run-btn").addEventListener("click", () => {
  if (LIVE) { let c = state.draft; try { if ($("#spec-form")) c = readForm(); } catch {} generate(c || structuredClone(RUNS[state.run]?.config || serverConfig)); return; }
  const chip = $("#mode-chip span"); chip.textContent = "Routing needs the Syntera server. Pick a recorded run instead.";
  setTimeout(() => (chip.textContent = `Snapshot of ${RUNS.length} recorded runs`), 3500);
});
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select")) return;
  const k = e.key.toLowerCase();
  if (k === "1") setView("iso"); else if (k === "2") setView("top"); else if (k === "3") setView("front"); else if (k === "4") setView("right");
  else if (k === "f") fit(); else if (k === "x") toggle("xray", "#tg-xray"); else if (k === "p") toggle("flow", "#tg-flow");
  else if (k === "s") setShade(state.shade === "stress" ? "solid" : "stress");
  else if (k === "[") setRun(state.run - 1); else if (k === "]") setRun(state.run + 1);
});
const mq = matchMedia("(prefers-color-scheme: dark)");
const retheme = () => { themeColors(); applyShading(); };
mq.addEventListener?.("change", retheme);
new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
if (!LIVE) {
  $("#run-btn").title = "Routing runs on the Syntera server; this page shows recorded runs";
  $("#mode-chip span").textContent = `Snapshot of ${RUNS.length} recorded runs`;
  renderScenarios(); setRun(0);
} else {
  $("#run-btn").title = "Route, verify and solve the current inputs (also in the Spec tab)";
  $("#mode-chip i").style.background = "var(--pass)";
  (async () => {
    try { serverConfig = await api("/api/config"); await generate(structuredClone(serverConfig)); }
    catch (error) { $("#mode-chip span").textContent = `Cannot reach the server: ${error.message}`; }
  })();
}
setView("iso"); resize(); requestAnimationFrame(frame);
})();
