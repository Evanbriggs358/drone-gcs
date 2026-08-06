import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";

const state = { project: null, view: "model", scene: null, stats: null };

// -- project list ---------------------------------------------------------

async function loadProjects() {
  const list = document.getElementById("project-list");
  let projects;
  try {
    projects = await (await fetch("/api/projects")).json();
  } catch (err) {
    list.innerHTML = `<li class="empty">Could not reach the server</li>`;
    return;
  }

  if (!projects.length) {
    list.innerHTML = `<li class="empty">No reconstructions found</li>`;
    return;
  }

  list.innerHTML = "";
  for (const project of projects) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.dataset.name = project.name;
    button.innerHTML = `
      <span class="name">${project.name}</span>
      <span class="meta">${project.photos} photos</span>
      <span class="badges">
        <span class="badge ${project.has_map ? "on" : ""}">map</span>
        <span class="badge ${project.has_3d_model ? "on" : ""}">3D</span>
      </span>`;
    button.addEventListener("click", () => selectProject(project));
    li.appendChild(button);
    list.appendChild(li);
  }

  selectProject(projects[0]);
}

function selectProject(project) {
  state.project = project;
  document.getElementById("project-name").textContent = project.name;
  document.querySelectorAll("#project-list button").forEach((b) =>
    b.classList.toggle("selected", b.dataset.name === project.name)
  );

  showMaps(project);
  showStats(project);
  showModel(project);
  showReplay(project);
}

// -- tabs -----------------------------------------------------------------

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.view = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("active", v.id === `view-${state.view}`)
    );
    if (state.view === "model" && state.scene) state.scene.resize();
  });
});

// -- map layers -----------------------------------------------------------

const LAYERS = [
  {
    key: "ortho",
    label: "Orthomosaic",
    caption:
      "Every photo reprojected as though shot from directly overhead, then " +
      "stitched. Distances and areas measured on this are true to the ground.",
  },
  {
    key: "elevation",
    label: "Elevation",
    legend: "elevation_legend",
    caption:
      "Digital surface model — the height of the ground and everything standing " +
      "on it. Colour runs low to high, so the tree canopy separates clearly from " +
      "the road beside it.",
  },
  {
    key: "overlap",
    label: "Photo overlap",
    legend: "overlap_legend",
    caption:
      "How many photos see each point. Green is well covered; yellow and red " +
      "mark thin coverage, where reconstruction degrades and the survey would " +
      "need re-flying.",
  },
  {
    key: "cameras",
    label: "Camera positions",
    caption:
      "Where each photo was taken. Red triangles are the solved camera " +
      "positions, cyan the recorded GPS fix, joined in capture order.",
  },
];

function showMaps(project) {
  const bar = document.getElementById("layer-bar");
  const buttons = document.getElementById("layer-buttons");
  const available = LAYERS.filter((layer) => project.layers[layer.key]);

  buttons.innerHTML = "";
  if (!available.length) {
    bar.classList.add("hidden");
    document.getElementById("measure-bar").classList.add("hidden");
    setLayer(project, null);
    return;
  }

  bar.classList.remove("hidden");
  document.getElementById("measure-bar").classList.remove("hidden");
  clearMeasure();
  for (const layer of available) {
    const button = document.createElement("button");
    button.textContent = layer.label;
    button.dataset.key = layer.key;
    button.addEventListener("click", () => setLayer(project, layer));
    buttons.appendChild(button);
  }

  setLayer(project, available[0]);
}

function setLayer(project, layer) {
  const status = document.getElementById("map-status");
  const frame = document.getElementById("map-frame");
  const img = document.getElementById("ortho-image");
  const legend = document.getElementById("layer-legend");
  const caption = document.getElementById("layer-caption");

  document.querySelectorAll("#layer-buttons button").forEach((b) =>
    b.classList.toggle("active", !!layer && b.dataset.key === layer.key)
  );

  if (!layer) {
    frame.classList.remove("ready");
    status.classList.remove("hidden");
    status.textContent = "No map layers in this reconstruction";
    return;
  }

  caption.textContent = layer.caption;
  img.alt = layer.label;

  const legendPath = layer.legend && project.layers[layer.legend];
  legend.classList.toggle("hidden", !legendPath);
  if (legendPath) legend.src = `/files/${project.name}/${legendPath}`;

  status.classList.remove("hidden");
  status.textContent = `Loading ${layer.label.toLowerCase()}…`;
  frame.classList.remove("ready");

  img.onload = () => {
    status.classList.add("hidden");
    frame.classList.add("ready");
  };
  img.onerror = () => {
    status.textContent = `${layer.label} failed to load`;
  };
  img.src = `/files/${project.name}/${project.layers[layer.key]}`;
}

// -- survey statistics ----------------------------------------------------

async function showStats(project) {
  const body = document.getElementById("stats-body");
  body.textContent = "Loading…";
  document.getElementById("stats-toolbar").classList.add("hidden");

  let stats;
  try {
    stats = await (await fetch(`/api/projects/${project.name}/stats`)).json();
  } catch {
    body.textContent = "Could not load survey data";
    return;
  }

  if (!stats.geotagged) {
    body.innerHTML = `<p>No geotagged photos found. Without GPS in the EXIF, a
      reconstruction cannot be placed on Earth or scaled correctly.</p>`;
    return;
  }

  const allTagged = stats.geotagged === stats.photos;
  const headingPct = Math.round((stats.with_heading / stats.geotagged) * 100);

  const cards = [
    card("Photos", stats.photos, ""),
    card(
      "Geotagged",
      `${stats.geotagged}/${stats.photos}`,
      allTagged ? "every photo has GPS" : "some photos lack GPS",
      allTagged ? "good" : "warn"
    ),
    card(
      "Camera heading",
      `${headingPct}%`,
      headingPct === 0 ? "not recorded by this camera" : "recorded",
      headingPct > 0 ? "good" : "warn"
    ),
    card(
      "Area covered",
      `${Math.round(stats.extent_m.width)}×${Math.round(stats.extent_m.height)} m`,
      ""
    ),
    card("Mean altitude", `${stats.altitude.mean.toFixed(0)} m`, "above sea level"),
    card(
      "Photo spacing",
      stats.spacing_m.median ? `${stats.spacing_m.median.toFixed(1)} m` : "—",
      "median between shots"
    ),
  ].join("");

  const rows = [
    ["Centre", `${stats.centre.lat.toFixed(6)}, ${stats.centre.lon.toFixed(6)}`],
    ["North / south", `${stats.bounds.north.toFixed(6)} / ${stats.bounds.south.toFixed(6)}`],
    ["East / west", `${stats.bounds.east.toFixed(6)} / ${stats.bounds.west.toFixed(6)}`],
    ["Altitude range", `${stats.altitude.min.toFixed(1)} – ${stats.altitude.max.toFixed(1)} m`],
    [
      "Spacing range",
      stats.spacing_m.min != null
        ? `${stats.spacing_m.min.toFixed(1)} – ${stats.spacing_m.max.toFixed(1)} m`
        : "—",
    ],
  ]
    .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`)
    .join("");

  const files = Object.entries(project.products)
    .filter(([, path]) => path)
    .map(
      ([kind, path]) =>
        `<a href="/files/${project.name}/${path}" download>${kind.replace(/_/g, " ")}</a>`
    )
    .join("");

  body.innerHTML = `
    <div class="cards">${cards}</div>
    <h3>Survey extent</h3>
    <table>${rows}</table>
    <h3>Products</h3>
    <div class="downloads">${files}</div>`;

  state.stats = stats;
  document.getElementById("stats-toolbar").classList.remove("hidden");
}

function card(label, value, sub, tone = "") {
  return `<div class="card">
    <div class="label">${label}</div>
    <div class="value ${tone}">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ""}
  </div>`;
}

// -- 3d model -------------------------------------------------------------

function showModel(project) {
  const status = document.getElementById("model-status");
  const canvas = document.getElementById("model-canvas");
  const hint = document.getElementById("model-hint");
  const model = project.products.textured_model;

  if (state.scene) {
    state.scene.dispose();
    state.scene = null;
  }
  canvas.classList.remove("ready");
  hint.classList.add("hidden");
  status.classList.remove("hidden");

  if (!model) {
    status.textContent = "No 3D model in this reconstruction";
    return;
  }

  status.innerHTML = `Loading 3D model&hellip;<div class="bar"><div id="model-bar"></div></div>`;

  const dir = model.substring(0, model.lastIndexOf("/") + 1);
  const file = model.substring(model.lastIndexOf("/") + 1);
  const base = `/files/${project.name}/${dir}`;

  const onProgress = (event) => {
    if (!event.lengthComputable) return;
    const bar = document.getElementById("model-bar");
    if (bar) bar.style.width = `${(event.loaded / event.total) * 100}%`;
  };

  new MTLLoader()
    .setPath(base)
    .load(file.replace(/\.obj$/, ".mtl"), (materials) => {
      materials.preload();
      new OBJLoader()
        .setMaterials(materials)
        .setPath(base)
        .load(
          file,
          (object) => {
            status.classList.add("hidden");
            canvas.classList.add("ready");
            hint.classList.remove("hidden");
            state.scene = buildScene(canvas, object);
          },
          onProgress,
          () => {
            status.textContent = "3D model failed to load";
          }
        );
    },
    undefined,
    () => { status.textContent = "Material file failed to load"; });
}

function buildScene(canvas, object) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1116);

  // ODM's georeferenced model uses UTM coordinates, so vertices sit hundreds of
  // thousands of units from the origin. Recentre it or the camera never finds
  // it. ODM is also Z-up while three.js is Y-up, hence the rotation.
  object.rotation.x = -Math.PI / 2;
  object.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(object);
  const centre = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  object.position.sub(centre);
  scene.add(object);

  const span = Math.max(size.x, size.y, size.z) || 1;

  // Frame from the bounding sphere rather than the box. Reconstructions grow
  // thin spikes at their edges where few photos overlapped, which inflate the
  // box and would push the camera far enough back to leave the model tiny.
  const radius = box.getBoundingSphere(new THREE.Sphere()).radius || span / 2;

  const fov = 50;
  const camera = new THREE.PerspectiveCamera(fov, 1, radius / 500, radius * 200);

  // Raised three-quarter view, and the camera basis for fitting.
  const direction = new THREE.Vector3(0.55, 0.62, 0.56).normalize();
  const right = new THREE.Vector3()
    .crossVectors(direction, new THREE.Vector3(0, 1, 0))
    .normalize();
  const up = new THREE.Vector3().crossVectors(right, direction).normalize();

  // Corners of the (now origin-centred) bounding box.
  const corners = [];
  for (const x of [box.min.x, box.max.x])
    for (const y of [box.min.y, box.max.y])
      for (const z of [box.min.z, box.max.z])
        corners.push(new THREE.Vector3(x, y, z).sub(centre));

  // Fitting a bounding *sphere* wastes most of the frame on a flat, elongated
  // terrain sheet. Instead solve for the smallest distance at which every box
  // corner still falls inside both the vertical and horizontal fields of view.
  function frame(aspect) {
    const halfV = (fov / 2) * (Math.PI / 180);
    const halfH = Math.atan(Math.tan(halfV) * aspect);
    let distance = 0;
    for (const corner of corners) {
      const depth = corner.dot(direction);
      distance = Math.max(
        distance,
        depth + Math.abs(corner.dot(right)) / Math.tan(halfH),
        depth + Math.abs(corner.dot(up)) / Math.tan(halfV)
      );
    }
    camera.position.copy(direction).multiplyScalar(distance * 1.06);
    camera.updateProjectionMatrix();
  }

  scene.add(new THREE.AmbientLight(0xffffff, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(1, 2, 1.5);
  scene.add(sun);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxDistance = radius * 12;

  let running = true;
  let framed = false;

  function resize() {
    const { clientWidth: w, clientHeight: h } = canvas;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    // Aspect ratio is unknown until the element has been laid out, and it
    // decides the fit, so frame once the first real size arrives.
    if (!framed) {
      frame(camera.aspect);
      framed = true;
    }
  }

  function tick() {
    if (!running) return;
    requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  }

  // Watch the canvas itself rather than the window. The element can gain or
  // change size without a window resize event — during initial layout, when
  // switching tabs, or when a hidden pane becomes visible — and a renderer
  // sized from a zero-height element stays stuck at WebGL's 300x150 default.
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);

  resize();
  tick();

  return {
    resize,
    dispose() {
      running = false;
      observer.disconnect();
      controls.dispose();
      renderer.dispose();
    },
  };
}

// -- measurement tools ----------------------------------------------------

const measure = { active: false, type: null, points: [] };

function syncMeasureSvg() {
  const img = document.getElementById("ortho-image");
  const svg = document.getElementById("measure-svg");
  const frame = document.getElementById("map-frame");
  if (!img.naturalWidth || !frame) return;
  const imgRect = img.getBoundingClientRect();
  const frameRect = frame.getBoundingClientRect();
  svg.style.left = (imgRect.left - frameRect.left + frame.scrollLeft) + "px";
  svg.style.top = (imgRect.top - frameRect.top + frame.scrollTop) + "px";
  svg.style.width = imgRect.width + "px";
  svg.style.height = imgRect.height + "px";
  svg.setAttribute("viewBox", `0 0 ${imgRect.width} ${imgRect.height}`);
}

function startMeasure(type) {
  clearMeasure();
  measure.active = true;
  measure.type = type;
  const svg = document.getElementById("measure-svg");
  syncMeasureSvg();
  svg.classList.remove("hidden");
  svg.classList.add("active");
  document.getElementById(type === "distance" ? "btn-measure-dist" : "btn-measure-area")
    .classList.add("active");
  document.getElementById("measure-readout").textContent = "Click to place points";
}

function clearMeasure() {
  measure.active = false;
  measure.type = null;
  measure.points = [];
  const svg = document.getElementById("measure-svg");
  if (svg) {
    svg.innerHTML = "";
    svg.classList.add("hidden");
    svg.classList.remove("active");
  }
  const dist = document.getElementById("btn-measure-dist");
  const area = document.getElementById("btn-measure-area");
  if (dist) dist.classList.remove("active");
  if (area) area.classList.remove("active");
  const readout = document.getElementById("measure-readout");
  if (readout) readout.textContent = "";
}

function finishMeasure() {
  measure.active = false;
  const svg = document.getElementById("measure-svg");
  svg.classList.remove("active");
  document.getElementById("btn-measure-dist").classList.remove("active");
  document.getElementById("btn-measure-area").classList.remove("active");
  drawMeasure();
}

function getMeterScale() {
  const img = document.getElementById("ortho-image");
  const svgEl = document.getElementById("measure-svg");
  if (!state.stats || !state.stats.extent_m || !img.naturalWidth) return null;
  const rect = svgEl.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    mx: state.stats.extent_m.width / rect.width,
    my: state.stats.extent_m.height / rect.height,
  };
}

function computeDistance(points) {
  const scale = getMeterScale();
  if (!scale) return 0;
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    const dx = (points[i].x - points[i - 1].x) * scale.mx;
    const dy = (points[i].y - points[i - 1].y) * scale.my;
    total += Math.sqrt(dx * dx + dy * dy);
  }
  return total;
}

function computeArea(points) {
  const scale = getMeterScale();
  if (!scale || points.length < 3) return 0;
  let area = 0;
  for (let i = 0; i < points.length; i++) {
    const j = (i + 1) % points.length;
    area += (points[i].x * scale.mx) * (points[j].y * scale.my);
    area -= (points[j].x * scale.mx) * (points[i].y * scale.my);
  }
  return Math.abs(area) / 2;
}

function formatDist(m) {
  return m >= 1000 ? (m / 1000).toFixed(2) + " km" : m.toFixed(1) + " m";
}

function formatArea(m2) {
  if (m2 >= 1000) return m2.toFixed(0) + " m² (" + (m2 / 4046.86).toFixed(2) + " acres)";
  return m2.toFixed(1) + " m²";
}

function drawMeasure() {
  const svg = document.getElementById("measure-svg");
  const readout = document.getElementById("measure-readout");
  const pts = measure.points;

  if (!pts.length) {
    svg.innerHTML = "";
    readout.textContent = measure.active ? "Click to place points" : "";
    return;
  }

  let html = "";

  if (pts.length >= 2) {
    const pointStr = pts.map(p => `${p.x},${p.y}`).join(" ");
    if (measure.type === "area") {
      html += `<polygon points="${pointStr}" fill="rgba(74,163,255,0.15)" stroke="#4aa3ff" stroke-width="2"/>`;
    } else {
      html += `<polyline points="${pointStr}" fill="none" stroke="#4aa3ff" stroke-width="2"/>`;
    }
  }

  for (const p of pts) {
    html += `<circle cx="${p.x}" cy="${p.y}" r="4" fill="#4aa3ff" stroke="#0e1116" stroke-width="1.5"/>`;
  }

  if (measure.type === "distance" && pts.length >= 2) {
    const dist = computeDistance(pts);
    const last = pts[pts.length - 1];
    html += `<text x="${last.x + 10}" y="${last.y - 10}" fill="#4aa3ff" font-size="13" font-weight="600">${formatDist(dist)}</text>`;
    readout.textContent = formatDist(dist);
  } else if (measure.type === "area") {
    if (pts.length >= 3 && !measure.active) {
      const a = computeArea(pts);
      const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
      const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
      html += `<text x="${cx}" y="${cy}" fill="#4aa3ff" font-size="13" font-weight="600" text-anchor="middle">${formatArea(a)}</text>`;
      readout.textContent = formatArea(a);
    } else if (pts.length >= 2) {
      readout.textContent = "Perimeter: " + formatDist(computeDistance(pts));
    }
  }

  svg.innerHTML = html;
}

document.getElementById("measure-svg").addEventListener("click", (e) => {
  if (!measure.active) return;
  const rect = e.currentTarget.getBoundingClientRect();
  measure.points.push({ x: e.clientX - rect.left, y: e.clientY - rect.top });
  drawMeasure();
});

document.getElementById("measure-svg").addEventListener("dblclick", (e) => {
  e.preventDefault();
  if (!measure.active) return;
  if (measure.points.length > 1) measure.points.pop();
  if (measure.type === "distance" && measure.points.length >= 2) finishMeasure();
  else if (measure.type === "area" && measure.points.length >= 3) finishMeasure();
});

document.getElementById("btn-measure-dist").addEventListener("click", () => startMeasure("distance"));
document.getElementById("btn-measure-area").addEventListener("click", () => startMeasure("area"));
document.getElementById("btn-measure-clear").addEventListener("click", clearMeasure);

document.getElementById("ortho-image").addEventListener("load", syncMeasureSvg);
new ResizeObserver(syncMeasureSvg).observe(document.getElementById("map-frame"));

// -- flight replay --------------------------------------------------------

const replay = {
  map: null,
  shots: [],
  marker: null,
  playing: false,
  animFrame: null,
  currentIndex: 0,
  speed: 1,
  initialized: false,
};

async function showReplay(project) {
  const status = document.getElementById("replay-status");
  const container = document.getElementById("replay-container");

  if (replay.map) { replay.map.remove(); replay.map = null; }
  replay.shots = [];
  replay.initialized = false;
  replay.lastPhotoIdx = -1;
  pauseReplay();
  document.getElementById("replay-pip").classList.add("hidden");

  container.classList.remove("ready");
  status.classList.remove("hidden");
  status.textContent = "Loading flight data…";

  let shots;
  try {
    shots = await (await fetch(`/api/projects/${project.name}/shots`)).json();
  } catch {
    status.textContent = "Could not load flight data";
    return;
  }

  if (!shots.length) {
    status.textContent = "No flight data available for this reconstruction";
    return;
  }

  replay.shots = shots;
  status.classList.add("hidden");
  container.classList.add("ready");

  if (state.view === "replay") setTimeout(() => initReplayMap(), 50);
}

function initReplayMap() {
  if (replay.initialized || !replay.shots.length) return;
  replay.initialized = true;
  buildReplayMap();
}

function buildReplayMap() {
  const shots = replay.shots;
  const map = L.map("replay-map", { preferCanvas: true });

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { attribution: "Esri", maxZoom: 19 }
  ).addTo(map);

  const latlngs = shots.map(s => [s.geometry.coordinates[1], s.geometry.coordinates[0]]);
  map.fitBounds(L.latLngBounds(latlngs).pad(0.1));

  const t0 = shots[0].properties.capture_time || 0;
  const t1 = shots[shots.length - 1].properties.capture_time || 1;
  const tRange = t1 - t0 || 1;

  for (let i = 0; i < shots.length; i++) {
    const shot = shots[i];
    const t = ((shot.properties.capture_time || 0) - t0) / tRange;
    const r = Math.round(50 + t * 205);
    const b = Math.round(255 - t * 205);
    const color = `rgb(${r},50,${b})`;
    const cm = L.circleMarker([shot.geometry.coordinates[1], shot.geometry.coordinates[0]], {
      radius: 5, color, fillColor: color, fillOpacity: 0.8, weight: 1,
    }).addTo(map);
    const idx = i;
    cm.on("click", () => {
      pauseReplay();
      replay.currentIndex = idx;
      updateReplayPosition(idx);
    });
  }

  L.polyline(latlngs, { color: "rgba(255,255,255,0.3)", weight: 1.5 }).addTo(map);

  replay.marker = L.circleMarker(latlngs[0], {
    radius: 8, color: "#fff", fillColor: "#4aa3ff", fillOpacity: 1, weight: 2,
  }).addTo(map);

  replay.map = map;
  replay.lastPhotoIdx = -1;

  const scrubber = document.getElementById("replay-scrubber");
  scrubber.max = shots.length - 1;
  scrubber.value = 0;
  replay.currentIndex = 0;

  document.getElementById("replay-pip").classList.remove("hidden");
  updateReplayPosition(0);
}

function updateReplayPosition(index) {
  const shots = replay.shots;
  if (!shots.length) return;
  const idx = Math.min(Math.max(Math.floor(index), 0), shots.length - 1);
  const shot = shots[idx];
  const coords = shot.geometry.coordinates;

  if (replay.marker) replay.marker.setLatLng([coords[1], coords[0]]);
  document.getElementById("replay-scrubber").value = idx;

  if (idx !== replay.lastPhotoIdx) {
    replay.lastPhotoIdx = idx;
    const filename = shot.properties.filename;
    if (filename && state.project) {
      document.getElementById("replay-photo").src =
        `/files/${state.project.name}/images/${filename}`;
    }
  }

  const props = shot.properties;
  const time = props.capture_time ? new Date(props.capture_time * 1000).toLocaleString() : "";
  const alt = coords[2] != null ? coords[2].toFixed(1) + " m" : "";
  document.getElementById("replay-info").innerHTML =
    `<span>${props.filename || ""}</span><span>${alt}</span><span>${time}</span>`;
}

function playReplay() {
  if (!replay.shots.length) return;
  replay.playing = true;
  document.getElementById("btn-replay-play").textContent = "Pause";
  let last = performance.now();
  function step(now) {
    if (!replay.playing) return;
    const dt = (now - last) / 1000;
    last = now;
    replay.currentIndex += dt * replay.speed;
    if (replay.currentIndex >= replay.shots.length - 1) {
      replay.currentIndex = 0;
      updateReplayPosition(0);
      pauseReplay();
      return;
    }
    updateReplayPosition(replay.currentIndex);
    if (replay.playing) replay.animFrame = requestAnimationFrame(step);
  }
  replay.animFrame = requestAnimationFrame(step);
}

function pauseReplay() {
  replay.playing = false;
  const btn = document.getElementById("btn-replay-play");
  if (btn) btn.textContent = "Play";
  if (replay.animFrame) { cancelAnimationFrame(replay.animFrame); replay.animFrame = null; }
}

document.getElementById("btn-replay-play").addEventListener("click", () => {
  replay.playing ? pauseReplay() : playReplay();
});

document.getElementById("replay-scrubber").addEventListener("input", (e) => {
  pauseReplay();
  replay.currentIndex = parseInt(e.target.value);
  updateReplayPosition(replay.currentIndex);
});

document.getElementById("replay-speed").addEventListener("change", (e) => {
  replay.speed = parseFloat(e.target.value);
});

document.querySelector('[data-view="replay"]').addEventListener("click", () => {
  setTimeout(() => {
    if (replay.shots.length && !replay.initialized) initReplayMap();
    if (replay.map) replay.map.invalidateSize();
  }, 100);
});

// -- photo lightbox -------------------------------------------------------

const lightbox = document.getElementById("photo-lightbox");
const lightboxImg = document.getElementById("lightbox-img");

document.getElementById("replay-photo").addEventListener("click", () => {
  lightboxImg.src = document.getElementById("replay-photo").src;
  lightbox.classList.remove("hidden");
});

document.getElementById("lightbox-close").addEventListener("click", () => {
  lightbox.classList.add("hidden");
});

lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) lightbox.classList.add("hidden");
});

// -- report export --------------------------------------------------------

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function exportReport() {
  const project = state.project;
  const stats = state.stats;
  if (!project || !stats || !stats.geotagged) return;

  const name = escHtml(project.name);
  const orthoSrc = project.layers.ortho
    ? `${location.origin}/files/${project.name}/${project.layers.ortho}` : "";
  const allTagged = stats.geotagged === stats.photos;
  const date = new Date().toLocaleDateString();

  const html = `<!doctype html>
<html><head><meta charset="utf-8">
<title>Survey Report — ${name}</title>
<style>
body{font-family:"Segoe UI",system-ui,sans-serif;color:#1a1a1a;max-width:800px;margin:0 auto;padding:40px 30px}
h1{font-size:22px;margin:0 0 4px}
.date{color:#666;font-size:14px;margin-bottom:24px}
.ortho{max-width:600px;border-radius:6px;margin-bottom:24px}
h2{font-size:15px;color:#444;border-bottom:1px solid #ddd;padding-bottom:6px;margin:24px 0 12px}
table{border-collapse:collapse;width:100%;margin-bottom:20px}
td{padding:8px 12px;border-bottom:1px solid #eee;font-size:14px}
td:first-child{color:#666;width:45%}
.footer{margin-top:40px;padding-top:16px;border-top:1px solid #ddd;color:#999;font-size:12px}
@media print{body{padding:20px}}
</style></head><body>
<h1>${name}</h1>
<p class="date">${date}</p>
${orthoSrc ? `<img class="ortho" src="${orthoSrc}" alt="Orthomosaic">` : ""}
<h2>Summary</h2>
<table>
<tr><td>Photos</td><td>${stats.photos}</td></tr>
<tr><td>Geotagged</td><td>${stats.geotagged} / ${stats.photos}${allTagged ? " (all)" : ""}</td></tr>
<tr><td>Area covered</td><td>${Math.round(stats.extent_m.width)} × ${Math.round(stats.extent_m.height)} m</td></tr>
<tr><td>Mean altitude</td><td>${stats.altitude.mean.toFixed(1)} m</td></tr>
<tr><td>Photo spacing (median)</td><td>${stats.spacing_m.median ? stats.spacing_m.median.toFixed(1) + " m" : "—"}</td></tr>
</table>
<h2>Survey extent</h2>
<table>
<tr><td>Centre</td><td>${stats.centre.lat.toFixed(6)}, ${stats.centre.lon.toFixed(6)}</td></tr>
<tr><td>North / South</td><td>${stats.bounds.north.toFixed(6)} / ${stats.bounds.south.toFixed(6)}</td></tr>
<tr><td>East / West</td><td>${stats.bounds.east.toFixed(6)} / ${stats.bounds.west.toFixed(6)}</td></tr>
<tr><td>Altitude range</td><td>${stats.altitude.min.toFixed(1)} – ${stats.altitude.max.toFixed(1)} m</td></tr>
<tr><td>Spacing range</td><td>${stats.spacing_m.min != null ? stats.spacing_m.min.toFixed(1) + " – " + stats.spacing_m.max.toFixed(1) + " m" : "—"}</td></tr>
</table>
<div class="footer">Generated by drone-gcs</div>
<script>window.onload=()=>setTimeout(()=>window.print(),500)<\/script>
</body></html>`;

  const w = window.open("", "_blank");
  w.document.write(html);
  w.document.close();
}

document.getElementById("btn-export").addEventListener("click", exportReport);

loadProjects();
