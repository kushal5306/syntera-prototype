const canvas = document.querySelector("#cad-canvas");
const gl = canvas.getContext("webgl", { antialias: true });
if (!gl) throw new Error("WebGL is required to display CAD geometry.");

const vertexSource = `
  attribute vec3 aPosition;
  attribute vec3 aNormal;
  uniform mat4 uMvp;
  varying float vLight;
  void main() {
    vec3 light = normalize(vec3(0.45, -0.55, 0.72));
    vLight = 0.32 + 0.68 * abs(dot(normalize(aNormal), light));
    gl_Position = uMvp * vec4(aPosition, 1.0);
  }
`;
const fragmentSource = `
  precision mediump float;
  uniform vec3 uColor;
  varying float vLight;
  void main() { gl_FragColor = vec4(uColor * vLight, 1.0); }
`;

function shader(type, source) {
  const result = gl.createShader(type);
  gl.shaderSource(result, source);
  gl.compileShader(result);
  if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(result));
  }
  return result;
}

const program = gl.createProgram();
gl.attachShader(program, shader(gl.VERTEX_SHADER, vertexSource));
gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragmentSource));
gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
const locations = {
  position: gl.getAttribLocation(program, "aPosition"),
  normal: gl.getAttribLocation(program, "aNormal"),
  mvp: gl.getUniformLocation(program, "uMvp"),
  color: gl.getUniformLocation(program, "uColor"),
};

function multiply(a, b) {
  const out = new Float32Array(16);
  for (let column = 0; column < 4; column++) {
    for (let row = 0; row < 4; row++) {
      let value = 0;
      for (let index = 0; index < 4; index++) {
        value += a[index * 4 + row] * b[column * 4 + index];
      }
      out[column * 4 + row] = value;
    }
  }
  return out;
}

function perspective(fov, aspect, near, far) {
  const f = 1 / Math.tan(fov / 2);
  const range = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * range, -1,
    0, 0, 2 * far * near * range, 0,
  ]);
}

function normalize(vector) {
  const length = Math.hypot(...vector) || 1;
  return vector.map((value) => value / length);
}

function cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function lookAt(eye, center, up) {
  const z = normalize(eye.map((value, index) => value - center[index]));
  const x = normalize(cross(up, z));
  const y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
    -x.reduce((sum, value, index) => sum + value * eye[index], 0),
    -y.reduce((sum, value, index) => sum + value * eye[index], 0),
    -z.reduce((sum, value, index) => sum + value * eye[index], 0), 1,
  ]);
}

const camera = { target: [300, 200, 150], yaw: 0.78, pitch: 0.48, distance: 900, fitted: 900 };
let meshes = [];
let selectedId = null;
let currentMvp = new Float32Array(16);
let initialConfig;
let busy = false;

function cameraEye() {
  const horizontal = camera.distance * Math.cos(camera.pitch);
  return [
    camera.target[0] + horizontal * Math.cos(camera.yaw),
    camera.target[1] + horizontal * Math.sin(camera.yaw),
    camera.target[2] + camera.distance * Math.sin(camera.pitch),
  ];
}

function resize() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function render() {
  resize();
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0.035, 0.065, 0.08, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);
  const projection = perspective(Math.PI / 4, canvas.width / canvas.height, 0.5, 10000);
  currentMvp = multiply(projection, lookAt(cameraEye(), camera.target, [0, 0, 1]));
  gl.useProgram(program);
  gl.uniformMatrix4fv(locations.mvp, false, currentMvp);
  for (const mesh of meshes) {
    if (!mesh.visible) continue;
    gl.bindBuffer(gl.ARRAY_BUFFER, mesh.positionBuffer);
    gl.enableVertexAttribArray(locations.position);
    gl.vertexAttribPointer(locations.position, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, mesh.normalBuffer);
    gl.enableVertexAttribArray(locations.normal);
    gl.vertexAttribPointer(locations.normal, 3, gl.FLOAT, false, 0, 0);
    const color = mesh.id === selectedId
      ? mesh.color.map((value) => Math.min(1, value * 1.35 + 0.12))
      : mesh.color;
    gl.uniform3fv(locations.color, color);
    gl.drawArrays(gl.TRIANGLES, 0, mesh.vertexCount);
  }
  requestAnimationFrame(render);
}

function makeBuffer(values) {
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.STATIC_DRAW);
  return buffer;
}

function setScene(payload, fit = true) {
  for (const mesh of meshes) {
    gl.deleteBuffer(mesh.positionBuffer);
    gl.deleteBuffer(mesh.normalBuffer);
  }
  meshes = payload.meshes.map((mesh) => ({
    ...mesh,
    visible: true,
    vertexCount: mesh.positions.length / 3,
    positionBuffer: makeBuffer(mesh.positions),
    normalBuffer: makeBuffer(mesh.normals),
  }));
  selectedId = null;
  buildObjectList();
  document.querySelector("#triangle-count").textContent =
    `${meshes.reduce((sum, mesh) => sum + mesh.triangle_count, 0).toLocaleString()} triangles`;
  if (fit) fitScene();
}

function fitScene() {
  if (!meshes.length) return;
  const minimum = [Infinity, Infinity, Infinity];
  const maximum = [-Infinity, -Infinity, -Infinity];
  for (const mesh of meshes) {
    for (let axis = 0; axis < 3; axis++) {
      minimum[axis] = Math.min(minimum[axis], mesh.bounds.min[axis]);
      maximum[axis] = Math.max(maximum[axis], mesh.bounds.max[axis]);
    }
  }
  camera.target = minimum.map((value, axis) => (value + maximum[axis]) / 2);
  camera.fitted = Math.max(80, Math.hypot(...maximum.map((value, axis) => value - minimum[axis])) * 1.25);
  camera.distance = camera.fitted;
}

function colorCss(color) {
  return `rgb(${color.map((value) => Math.round(value * 255)).join(",")})`;
}

function buildObjectList() {
  const list = document.querySelector("#object-list");
  list.replaceChildren();
  for (const mesh of meshes) {
    const row = document.createElement("div");
    row.className = "object-row" + (mesh.id === selectedId ? " selected" : "");
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = mesh.visible;
    toggle.addEventListener("click", (event) => event.stopPropagation());
    toggle.addEventListener("change", () => { mesh.visible = toggle.checked; });
    const icon = document.createElement("i");
    icon.className = `object-icon ${mesh.kind}`;
    icon.style.color = colorCss(mesh.color);
    const copy = document.createElement("span");
    copy.className = "object-copy";
    const name = document.createElement("strong");
    name.textContent = mesh.name;
    const kind = document.createElement("span");
    kind.textContent = mesh.kind;
    copy.append(name, kind);
    row.append(toggle, icon, copy);
    row.addEventListener("click", () => selectMesh(mesh.id));
    list.append(row);
  }
}

function friendly(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function selectMesh(identifier) {
  selectedId = identifier;
  buildObjectList();
  const mesh = meshes.find((item) => item.id === identifier);
  const card = document.querySelector("#selection-card");
  if (!mesh) {
    card.innerHTML = '<span class="eyebrow">SELECTION</span><strong>No component selected</strong>';
    return;
  }
  card.replaceChildren();
  const eyebrow = document.createElement("span");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "SELECTION";
  const title = document.createElement("strong");
  title.textContent = mesh.name;
  const properties = document.createElement("div");
  properties.className = "property-list";
  const entries = { Type: mesh.kind, Triangles: mesh.triangle_count, ...mesh.properties };
  for (const [key, value] of Object.entries(entries)) {
    const label = document.createElement("span");
    label.textContent = key.replaceAll("_", " ");
    const content = document.createElement("span");
    content.textContent = friendly(value);
    properties.append(label, content);
  }
  card.append(eyebrow, title, properties);
}

function project(point) {
  const m = currentMvp;
  const w = m[3] * point[0] + m[7] * point[1] + m[11] * point[2] + m[15];
  if (w <= 0) return null;
  return [
    (m[0] * point[0] + m[4] * point[1] + m[8] * point[2] + m[12]) / w,
    (m[1] * point[0] + m[5] * point[1] + m[9] * point[2] + m[13]) / w,
  ];
}

let pointer = null;
let moved = false;
canvas.addEventListener("pointerdown", (event) => {
  pointer = [event.clientX, event.clientY];
  moved = false;
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointermove", (event) => {
  if (!pointer) return;
  const dx = event.clientX - pointer[0];
  const dy = event.clientY - pointer[1];
  if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
  if (event.shiftKey || event.buttons === 2) {
    const scale = camera.distance * 0.0015;
    camera.target[0] += scale * (dx * Math.sin(camera.yaw) + dy * Math.cos(camera.yaw));
    camera.target[1] += scale * (-dx * Math.cos(camera.yaw) + dy * Math.sin(camera.yaw));
    camera.target[2] += scale * dy;
  } else {
    camera.yaw -= dx * 0.007;
    camera.pitch = Math.max(-1.45, Math.min(1.45, camera.pitch + dy * 0.007));
  }
  pointer = [event.clientX, event.clientY];
});
canvas.addEventListener("pointerup", (event) => {
  if (!moved) {
    const rect = canvas.getBoundingClientRect();
    const mouse = [(event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2];
    let best = null;
    for (const mesh of meshes.filter((item) => item.visible)) {
      const center = mesh.bounds.min.map((value, axis) => (value + mesh.bounds.max[axis]) / 2);
      const screen = project(center);
      if (!screen) continue;
      const distance = Math.hypot((screen[0] - mouse[0]) * rect.width, (screen[1] - mouse[1]) * rect.height);
      if (distance < 70 && (!best || distance < best.distance)) best = { id: mesh.id, distance };
    }
    if (best) selectMesh(best.id);
  }
  pointer = null;
});
canvas.addEventListener("contextmenu", (event) => event.preventDefault());
canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  camera.distance = Math.max(20, camera.distance * Math.exp(event.deltaY * 0.001));
}, { passive: false });

const directions = {
  "+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0],
  "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1],
};
for (const id of ["start-direction", "end-direction"]) {
  const select = document.querySelector(`#${id}`);
  for (const name of Object.keys(directions)) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.append(option);
  }
}
function directionName(vector) {
  return Object.entries(directions).find(([, value]) => value.every((item, index) => item === vector[index]))?.[0] || "+X";
}

function number(id) {
  return Number(document.querySelector(`#${id}`).value);
}

function fillForm(config) {
  document.querySelector("#outer-diameter").value = config.tube.outer_diameter;
  document.querySelector("#clearance").value = config.tube.minimum_clearance;
  document.querySelector("#bend-radius").value = config.tube.minimum_bend_radius;
  document.querySelector("#resolution").value = config.voxel_resolution;
  ["start", "end"].forEach((label) => {
    const port = config[`${label}_port`];
    ["x", "y", "z"].forEach((axis, index) => {
      document.querySelector(`#${label}-${axis}`).value = port.position[index];
    });
    document.querySelector(`#${label}-direction`).value = directionName(port.direction);
  });
  document.querySelector("#obstacles").value = JSON.stringify(config.obstacles, null, 2);
}

function readConfig() {
  const config = structuredClone(initialConfig);
  config.tube.outer_diameter = number("outer-diameter");
  config.tube.minimum_clearance = number("clearance");
  config.tube.minimum_bend_radius = number("bend-radius");
  config.voxel_resolution = number("resolution");
  for (const label of ["start", "end"]) {
    config[`${label}_port`].position = ["x", "y", "z"].map((axis) => number(`${label}-${axis}`));
    config[`${label}_port`].direction = directions[document.querySelector(`#${label}-direction`).value];
  }
  config.obstacles = JSON.parse(document.querySelector("#obstacles").value);
  return config;
}

async function request(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join("; ")
      : payload.detail || "Request failed";
    throw new Error(detail);
  }
  return payload;
}

function setBusy(value, message) {
  busy = value;
  document.querySelector("#generate-button").disabled = value;
  document.querySelector("#update-button").disabled = value;
  document.querySelector("#viewport-message").classList.toggle("hidden", !value);
  document.querySelector("#viewport-message").textContent = message;
  document.querySelector("#status-text").textContent = message;
}

function showError(error) {
  document.querySelector("#status-text").textContent = error.message;
  const message = document.querySelector("#viewport-message");
  message.textContent = error.message;
  message.classList.remove("hidden");
  setTimeout(() => { if (!busy) message.classList.add("hidden"); }, 3500);
}

function showAssurance(assurance, downloads) {
  const badge = document.querySelector("#result-badge");
  badge.className = `result-badge ${assurance.overall_pass ? "pass" : "fail"}`;
  badge.textContent = assurance.overall_pass ? "PASS" : "FAIL";
  const clearance = assurance.minimum_measured_clearance_mm;
  const values = [
    ["Route", assurance.route_found ? `${assurance.route_length_mm.toFixed(1)} mm` : "Not found"],
    ["Collision", assurance.collision_free ? "Clear" : "Detected"],
    ["Clearance", clearance == null ? "-" : `${clearance.toFixed(1)} / ${assurance.required_clearance_mm} mm`],
    ["Bends", String(assurance.number_of_bends)],
  ];
  const grid = document.querySelector("#metric-grid");
  grid.replaceChildren();
  for (const [label, value] of values) {
    const item = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(caption, strong);
    grid.append(item);
  }
  const reasons = document.querySelector("#failure-reasons");
  reasons.classList.toggle("hidden", !assurance.failure_reasons.length);
  reasons.textContent = assurance.failure_reasons.join("; ");
  const links = document.querySelector("#downloads");
  links.replaceChildren();
  for (const download of downloads) {
    const link = document.createElement("a");
    link.href = download.url;
    link.textContent = download.name;
    link.setAttribute("download", download.name);
    links.append(link);
  }
}

async function updatePreview() {
  try {
    setBusy(true, "Tessellating assembly...");
    const payload = await request("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(readConfig()),
    });
    setScene(payload);
    document.querySelector("#status-text").textContent = "Assembly preview updated";
  } catch (error) { showError(error); }
  finally { setBusy(false, document.querySelector("#status-text").textContent); }
}

async function generateRoute() {
  try {
    setBusy(true, "Routing and verifying exact CAD...");
    const payload = await request("/api/route", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(readConfig()),
    });
    setScene(payload);
    showAssurance(payload.assurance, payload.downloads);
    document.querySelector("#status-text").textContent = payload.assurance.overall_pass
      ? "Verified solution generated" : "Solution failed assurance";
  } catch (error) { showError(error); }
  finally { setBusy(false, document.querySelector("#status-text").textContent); }
}

document.querySelector("#update-button").addEventListener("click", updatePreview);
document.querySelector("#generate-button").addEventListener("click", generateRoute);
document.querySelector("#reset-button").addEventListener("click", () => { fillForm(initialConfig); updatePreview(); });
document.querySelector("#fit-button").addEventListener("click", fitScene);
document.querySelector("#show-all-button").addEventListener("click", () => {
  meshes.forEach((mesh) => { mesh.visible = true; });
  buildObjectList();
});
for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-view]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    const view = button.dataset.view;
    if (view === "iso") [camera.yaw, camera.pitch] = [0.78, 0.48];
    if (view === "top") [camera.yaw, camera.pitch] = [0, 1.45];
    if (view === "front") [camera.yaw, camera.pitch] = [-Math.PI / 2, 0];
    if (view === "right") [camera.yaw, camera.pitch] = [0, 0];
  });
}

async function initialize() {
  try {
    initialConfig = await request("/api/config");
    fillForm(initialConfig);
    await updatePreview();
    document.querySelector("#status-text").textContent = "Assembly ready";
  } catch (error) { showError(error); }
}

render();
initialize();
