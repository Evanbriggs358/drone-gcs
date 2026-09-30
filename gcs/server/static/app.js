import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";

const state = { project: null, view: "replay", scene: null, stats: null };

// -- demo data for static deployment --------------------------------------

function buildDemoData() {
  const centreLat = 47.6300, centreLon = -122.3175;
  const alt = 80, speed = 4.5, lineSpacing = 0.00012, photoSpacing = 0.00008;
  const lines = 8, photosPerLine = 14;
  const shots = [];
  let t = Date.now() / 1000 - 600;
  const startLon = centreLon - (lines * lineSpacing) / 2;
  const startLat = centreLat - (photosPerLine * photoSpacing) / 2;

  for (let line = 0; line < lines; line++) {
    const lon = startLon + line * lineSpacing;
    for (let p = 0; p < photosPerLine; p++) {
      const idx = line % 2 === 0 ? p : photosPerLine - 1 - p;
      const lat = startLat + idx * photoSpacing;
      const jitter = (Math.sin(line * 7 + p * 13) * 0.3);
      shots.push({
        geometry: { coordinates: [lon, lat, alt + jitter], type: "Point" },
        properties: {
          filename: `DJI_${String(shots.length + 1).padStart(4, "0")}.jpg`,
          capture_time: t,
        },
      });
      t += photoSpacing * 111320 / speed;
    }
    t += 6;
  }

  const project = {
    name: "Volunteer Park — demo",
    photos: shots.length,
    has_map: false,
    has_3d_model: false,
    layers: {},
    products: {},
  };

  const lats = shots.map(s => s.geometry.coordinates[1]);
  const lons = shots.map(s => s.geometry.coordinates[0]);
  const alts = shots.map(s => s.geometry.coordinates[2]);
  const spacings = [];
  for (let i = 1; i < shots.length; i++) {
    const dlat = (lats[i] - lats[i - 1]) * 111320;
    const dlon = (lons[i] - lons[i - 1]) * 111320 * Math.cos(lats[i] * Math.PI / 180);
    spacings.push(Math.sqrt(dlat * dlat + dlon * dlon));
  }
  spacings.sort((a, b) => a - b);

  const widthM = (Math.max(...lons) - Math.min(...lons)) * 111320 * Math.cos(centreLat * Math.PI / 180);
  const heightM = (Math.max(...lats) - Math.min(...lats)) * 111320;

  const stats = {
    photos: shots.length,
    geotagged: shots.length,
    with_heading: shots.length,
    centre: { lat: centreLat, lon: centreLon },
    bounds: {
      north: Math.max(...lats), south: Math.min(...lats),
      east: Math.max(...lons), west: Math.min(...lons),
    },
    extent_m: { width: Math.round(widthM), height: Math.round(heightM) },
    altitude: {
      min: Math.min(...alts), max: Math.max(...alts),
      mean: alts.reduce((a, b) => a + b) / alts.length,
    },
    spacing_m: {
      min: spacings[0], max: spacings[spacings.length - 1],
      median: spacings[Math.floor(spacings.length / 2)],
    },
  };

  return { project, shots, stats };
}

// -- project list ---------------------------------------------------------

async function loadProjects() {
  const list = document.getElementById("project-list");
  let projects;
  try {
    const resp = await fetch("api/projects");
    if (!resp.ok) throw new Error(resp.status);
    projects = await resp.json();
  } catch (err) {
    try {
      const demoResp = await fetch("demo/flight_data.json");
      const demoData = await demoResp.json();
      state.demo = demoData;
      projects = [demoData.project];
    } catch {
      projects = [];
    }
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
    demoRender: true,
    caption:
      "Every photo reprojected as though shot from directly overhead, then " +
      "stitched. Distances and areas measured on this are true to the ground.",
  },
  {
    key: "elevation",
    label: "Elevation",
    legend: "elevation_legend",
    demoRender: true,
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
  {
    key: "coverage",
    label: "Coverage analysis",
    computed: true,
    caption:
      "Computed from photo positions and altitude. Each cell shows how many " +
      "photos cover it. Blue is well-covered; yellow and red mark thin overlap " +
      "where reconstruction quality may suffer.",
  },
];

function renderCoverageHeatmap(shots) {
  if (shots.length < 2) return null;

  const coords = shots.map((s) => ({
    lat: s.geometry.coordinates[1],
    lon: s.geometry.coordinates[0],
    alt: s.geometry.coordinates[2] || 80,
  }));

  const lats = coords.map((c) => c.lat);
  const lons = coords.map((c) => c.lon);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);

  const midLat = (minLat + maxLat) / 2;
  const mPerDegLat = 111320;
  const mPerDegLon = 111320 * Math.cos((midLat * Math.PI) / 180);

  const hfovRad = (73 * Math.PI) / 180;
  const vfovRad = (56 * Math.PI) / 180;

  const pad = 0.3;
  const spanLat = (maxLat - minLat) * (1 + pad) || 0.001;
  const spanLon = (maxLon - minLon) * (1 + pad) || 0.001;
  const originLat = minLat - (spanLat * pad) / 2;
  const originLon = minLon - (spanLon * pad) / 2;

  const RES = 400;
  const aspectRatio = (spanLon * mPerDegLon) / (spanLat * mPerDegLat);
  const W = Math.round(RES * Math.max(1, aspectRatio));
  const H = Math.round(RES / Math.min(1, aspectRatio));
  const grid = new Uint8Array(W * H);

  for (const c of coords) {
    const halfW_m = c.alt * Math.tan(hfovRad / 2);
    const halfH_m = c.alt * Math.tan(vfovRad / 2);
    const halfW_deg = halfW_m / mPerDegLon;
    const halfH_deg = halfH_m / mPerDegLat;

    const x0 = Math.max(0, Math.floor(((c.lon - halfW_deg - originLon) / spanLon) * W));
    const x1 = Math.min(W - 1, Math.ceil(((c.lon + halfW_deg - originLon) / spanLon) * W));
    const y0 = Math.max(0, Math.floor(((c.lat - halfH_deg - originLat) / spanLat) * H));
    const y1 = Math.min(H - 1, Math.ceil(((c.lat + halfH_deg - originLat) / spanLat) * H));

    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        const idx = (H - 1 - y) * W + x;
        if (grid[idx] < 255) grid[idx]++;
      }
    }
  }

  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  const imageData = ctx.createImageData(W, H);
  const d = imageData.data;

  let maxCount = 0;
  for (let i = 0; i < grid.length; i++) if (grid[i] > maxCount) maxCount = grid[i];

  for (let i = 0; i < grid.length; i++) {
    const count = grid[i];
    const pi = i * 4;
    if (count === 0) {
      d[pi] = 20; d[pi + 1] = 22; d[pi + 2] = 30; d[pi + 3] = 255;
    } else {
      const t = count / Math.max(maxCount, 1);
      if (t > 0.5) {
        d[pi] = Math.round(30 + (1 - t) * 200);
        d[pi + 1] = Math.round(100 + t * 155);
        d[pi + 2] = Math.round(255 * (1 - t) * 0.4);
      } else {
        d[pi] = Math.round(255 * (1 - t * 2) + 30 * t * 2);
        d[pi + 1] = Math.round(80 * t * 2 + 50);
        d[pi + 2] = 20;
      }
      d[pi + 3] = 255;
    }
  }

  ctx.putImageData(imageData, 0, 0);

  ctx.fillStyle = "rgba(0,0,0,0.6)";
  ctx.fillRect(W - 110, H - 80, 106, 76);
  ctx.font = "bold 11px sans-serif";
  ctx.fillStyle = "#e6edf3";
  ctx.fillText("Coverage", W - 104, H - 63);
  ctx.font = "10px sans-serif";
  const steps = [
    [maxCount + "+", "#64ff96"],
    [Math.round(maxCount * 0.5) + "", "#d4a030"],
    ["1", "#ff5040"],
    ["0", "#161b22"],
  ];
  steps.forEach(([label, color], i) => {
    const y = H - 50 + i * 14;
    ctx.fillStyle = color;
    ctx.fillRect(W - 104, y, 10, 10);
    ctx.fillStyle = "#e6edf3";
    ctx.fillText(label + " photos", W - 90, y + 9);
  });

  return canvas.toDataURL("image/png");
}

function terrainNoise(px, py) {
  return (Math.sin(px * 0.8 + py * 0.6) + 1) * 0.2 +
         (Math.sin(px * 2.1 - py * 1.7 + 3.0) + 1) * 0.15 +
         (Math.sin(px * 5.3 + py * 4.1 + 7.0) + 1) * 0.1 +
         (Math.sin(px * 11.0 - py * 9.5 + 1.5) + 1) * 0.05;
}

function renderDemoOrtho(shots) {
  if (shots.length < 2) return null;
  const coords = shots.map(s => ({ lat: s.geometry.coordinates[1], lon: s.geometry.coordinates[0] }));
  const lats = coords.map(c => c.lat), lons = coords.map(c => c.lon);
  const pad = 0.3;
  const spanLat = (Math.max(...lats) - Math.min(...lats)) * (1 + pad) || 0.001;
  const spanLon = (Math.max(...lons) - Math.min(...lons)) * (1 + pad) || 0.001;
  const originLat = Math.min(...lats) - spanLat * pad / 2;
  const originLon = Math.min(...lons) - spanLon * pad / 2;

  const W = 800, H = 800;
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  const imageData = ctx.createImageData(W, H);
  const d = imageData.data;

  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const px = x * 0.04, py = y * 0.04;
      const zone = (Math.sin(px * 0.7 + py * 0.5 + 1) + Math.sin(px * 0.3 - py * 0.8 + 2)) * 0.5;
      const detail = Math.sin(px * 5.7 + py * 3.3) * 0.1 + Math.sin(px * 11 - py * 8.5) * 0.05;
      const v = zone + detail;
      const i = (y * W + x) * 4;
      if (v > 0.5) {
        d[i] = 20 + (v - 0.5) * 50 + detail * 40;
        d[i + 1] = 50 + (v - 0.5) * 60 + detail * 30;
        d[i + 2] = 15 + (v - 0.5) * 20;
      } else if (v > -0.2) {
        const t = (v + 0.2) / 0.7;
        d[i] = 65 + 40 * t + detail * 50;
        d[i + 1] = 115 + 30 * t + detail * 40;
        d[i + 2] = 40 + 20 * t;
      } else {
        d[i] = 155 + detail * 30;
        d[i + 1] = 148 + detail * 25;
        d[i + 2] = 130 + detail * 20;
      }
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(imageData, 0, 0);

  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 8; i++) {
    const lx = W * (i + 1) / 9;
    ctx.beginPath(); ctx.moveTo(lx, 0); ctx.lineTo(lx, H); ctx.stroke();
  }
  for (let j = 0; j < 14; j++) {
    const ly = H * (j + 1) / 15;
    ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(W, ly); ctx.stroke();
  }

  ctx.fillStyle = "rgba(255,100,100,0.6)";
  for (const c of coords) {
    const cx = ((c.lon - originLon) / spanLon) * W;
    const cy = (1 - (c.lat - originLat) / spanLat) * H;
    ctx.beginPath(); ctx.arc(cx, cy, 2, 0, Math.PI * 2); ctx.fill();
  }

  ctx.fillStyle = "rgba(0,0,0,0.5)";
  ctx.fillRect(10, H - 30, 200, 22);
  ctx.fillStyle = "#e6edf3";
  ctx.font = "11px sans-serif";
  ctx.fillText("Simulated orthomosaic (demo)", 16, H - 14);
  return canvas.toDataURL("image/png");
}

function renderDemoElevation(shots) {
  if (shots.length < 2) return null;
  const W = 800, H = 800;
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  const imageData = ctx.createImageData(W, H);
  const d = imageData.data;

  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const px = x * 0.04, py = y * 0.04;
      const n = terrainNoise(px, py);
      const i = (y * W + x) * 4;
      if (n < 0.3) {
        const t = n / 0.3;
        d[i] = Math.round(30 * t);
        d[i + 1] = Math.round(50 + 100 * t);
        d[i + 2] = Math.round(150 + 50 * t);
      } else if (n < 0.6) {
        const t = (n - 0.3) / 0.3;
        d[i] = Math.round(30 + 180 * t);
        d[i + 1] = Math.round(150 + 55 * t);
        d[i + 2] = Math.round(200 - 165 * t);
      } else {
        const t = (n - 0.6) / 0.4;
        d[i] = Math.round(210 + 45 * t);
        d[i + 1] = Math.round(205 - 85 * t);
        d[i + 2] = Math.round(35 + 45 * t);
      }
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(imageData, 0, 0);

  const lx = W - 230, ly = H - 50;
  ctx.fillStyle = "rgba(0,0,0,0.6)";
  ctx.fillRect(lx - 6, ly - 22, 222, 46);
  ctx.fillStyle = "#e6edf3";
  ctx.font = "bold 11px sans-serif";
  ctx.fillText("Elevation", lx, ly - 8);
  const grad = ctx.createLinearGradient(lx, 0, lx + 200, 0);
  grad.addColorStop(0, "rgb(0,50,150)");
  grad.addColorStop(0.3, "rgb(30,150,200)");
  grad.addColorStop(0.5, "rgb(100,200,40)");
  grad.addColorStop(0.7, "rgb(210,205,35)");
  grad.addColorStop(1, "rgb(255,120,80)");
  ctx.fillStyle = grad;
  ctx.fillRect(lx, ly, 200, 16);
  ctx.fillStyle = "#e6edf3";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("Low", lx, ly + 16 + 12);
  ctx.textAlign = "right";
  ctx.fillText("High", lx + 200, ly + 16 + 12);
  ctx.textAlign = "left";
  return canvas.toDataURL("image/png");
}

function buildDemoTerrain() {
  const size = 120, segs = 100;
  const geometry = new THREE.PlaneGeometry(size, size, segs, segs);
  const pos = geometry.attributes.position;
  const colors = new Float32Array(pos.count * 3);
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i), y = pos.getY(i);
    const px = (x / size + 0.5) * 20, py = (y / size + 0.5) * 20;
    const n = terrainNoise(px, py);
    pos.setZ(i, n * 15);
    if (n < 0.3) {
      colors[i * 3] = 0.5; colors[i * 3 + 1] = 0.55; colors[i * 3 + 2] = 0.4;
    } else if (n < 0.6) {
      colors[i * 3] = 0.2 + n * 0.3;
      colors[i * 3 + 1] = 0.4 + n * 0.3;
      colors[i * 3 + 2] = 0.1 + n * 0.1;
    } else {
      colors[i * 3] = 0.1 + n * 0.15;
      colors[i * 3 + 1] = 0.25 + n * 0.15;
      colors[i * 3 + 2] = 0.05 + n * 0.1;
    }
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.computeVertexNormals();
  return new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide }));
}

function showMaps(project) {
  const bar = document.getElementById("layer-bar");
  const buttons = document.getElementById("layer-buttons");
  const available = LAYERS.filter((layer) =>
    layer.computed ? replay.shots.length > 0
    : state.demo && layer.demoRender ? replay.shots.length > 0
    : project.layers[layer.key]
  );

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

  if (layer.computed && layer.key === "coverage") {
    status.classList.remove("hidden");
    status.textContent = "Computing coverage…";
    frame.classList.remove("ready");
    const dataUrl = renderCoverageHeatmap(replay.shots);
    if (dataUrl) {
      img.onload = () => { status.classList.add("hidden"); frame.classList.add("ready"); };
      img.onerror = () => { status.textContent = "Coverage render failed"; };
      img.src = dataUrl;
    } else {
      status.textContent = "Not enough data to compute coverage";
    }
    return;
  }

  if (state.demo && layer.demoRender) {
    status.classList.remove("hidden");
    status.textContent = `Loading ${layer.label.toLowerCase()}…`;
    frame.classList.remove("ready");
    img.onload = () => { status.classList.add("hidden"); frame.classList.add("ready"); };
    img.onerror = () => { status.textContent = `${layer.label} failed to load`; };
    img.src = layer.key === "ortho" ? "demo/orthomosaic.jpg" : "demo/elevation.jpg";
    return;
  }

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
  if (state.demo && state.demo.project.name === project.name) {
    stats = state.demo.stats;
  } else {
    try {
      stats = await (await fetch(`api/projects/${project.name}/stats`)).json();
    } catch {
      body.textContent = "Could not load survey data";
      return;
    }
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
    ${files ? `<h3>Products</h3><div class="downloads">${files}</div>` : ""}`;

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
    if (state.demo) {
      status.innerHTML = `Loading 3D model&hellip;<div class="bar"><div id="model-bar"></div></div>`;
      const onProgress = (event) => {
        if (!event.lengthComputable) return;
        const bar = document.getElementById("model-bar");
        if (bar) bar.style.width = `${(event.loaded / event.total) * 100}%`;
      };
      new MTLLoader().setPath("demo/").load("model.mtl", (materials) => {
        materials.preload();
        new OBJLoader().setMaterials(materials).setPath("demo/").load("model.obj", (object) => {
          status.classList.add("hidden");
          canvas.classList.add("ready");
          hint.classList.remove("hidden");
          state.scene = buildScene(canvas, object);
        }, onProgress, () => { status.textContent = "3D model failed to load"; });
      }, undefined, () => { status.textContent = "Material file failed to load"; });
      return;
    }
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
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1116);

  // ODM's georeferenced model uses UTM coordinates, so vertices sit hundreds of
  // thousands of units from the origin. Recentre it or the camera never finds
  // it. ODM is also Z-up while three.js is Y-up, hence the rotation.
  object.rotation.x = -Math.PI / 2;
  object.traverse((child) => {
    if (child.isMesh && child.material) {
      const swap = (m) => {
        if (m.map) {
          m.map.colorSpace = THREE.SRGBColorSpace;
          m.map.flipY = false;
          m.map.needsUpdate = true;
          return new THREE.MeshBasicMaterial({ map: m.map, side: THREE.DoubleSide });
        }
        m.side = THREE.DoubleSide;
        return m;
      };
      child.material = Array.isArray(child.material) ? child.material.map(swap) : swap(child.material);
    }
  });
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
  } else if (measure.type === "area" && pts.length >= 3) {
    const a = computeArea(pts);
    const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
    const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
    html += `<text x="${cx}" y="${cy}" fill="#4aa3ff" font-size="13" font-weight="600" text-anchor="middle">${formatArea(a)}</text>`;
    readout.textContent = formatArea(a);
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
  if (state.demo && state.demo.project.name === project.name) {
    shots = state.demo.shots;
  } else {
    try {
      shots = await (await fetch(`api/projects/${project.name}/shots`)).json();
    } catch {
      status.textContent = "Could not load flight data";
      return;
    }
  }

  if (!shots.length) {
    status.textContent = "No flight data available for this reconstruction";
    return;
  }

  replay.shots = shots;
  status.classList.add("hidden");
  container.classList.add("ready");

  showMaps(project);

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

  buildReplayGraphs(shots);

  document.getElementById("replay-pip").classList.remove("hidden");
  updateReplayPosition(0);
}

function buildReplayGraphs(shots) {
  const altitudes = shots.map((s) => s.geometry.coordinates[2] ?? null);
  const speeds = shots.map((s) => {
    if (!s.properties.capture_time) return null;
    return null;
  });

  const t0 = shots[0].properties.capture_time || 0;
  for (let i = 1; i < shots.length; i++) {
    const dt =
      ((shots[i].properties.capture_time || 0) - (shots[i - 1].properties.capture_time || 0));
    if (dt > 0) {
      const c0 = shots[i - 1].geometry.coordinates;
      const c1 = shots[i].geometry.coordinates;
      const dlat = (c1[1] - c0[1]) * 111320;
      const dlon = (c1[0] - c0[0]) * 111320 * Math.cos((c1[1] * Math.PI) / 180);
      const dist = Math.sqrt(dlat * dlat + dlon * dlon);
      speeds[i] = dist / dt;
    }
  }

  drawSparkline("graph-altitude", altitudes, "#4aa3ff", "Altitude (m)");
  drawSparkline("graph-speed", speeds, "#3fb950", "Speed (m/s)");
  document.getElementById("replay-graphs").classList.remove("hidden");
  replay.graphAltitudes = altitudes;
  replay.graphSpeeds = speeds;
}

function drawSparkline(canvasId, values, color, label) {
  const canvas = document.getElementById(canvasId);
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const valid = values.filter((v) => v != null);
  if (!valid.length) return;
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const range = max - min || 1;
  const pad = 14;

  ctx.beginPath();
  let started = false;
  for (let i = 0; i < values.length; i++) {
    if (values[i] == null) continue;
    const x = (i / (values.length - 1)) * w;
    const y = pad + (1 - (values[i] - min) / range) * (h - pad * 2);
    if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.fillStyle = "rgba(139,152,169,0.7)";
  ctx.font = "10px sans-serif";
  ctx.fillText(label, 4, 10);
  ctx.textAlign = "right";
  ctx.fillText(`${max.toFixed(1)}`, w - 4, 10);
  ctx.fillText(`${min.toFixed(1)}`, w - 4, h - 3);
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
      document.getElementById("replay-photo").src = state.demo
        ? `demo/images/${filename}`
        : `/files/${state.project.name}/images/${filename}`;
    }
  }

  const props = shot.properties;
  const time = props.capture_time ? new Date(props.capture_time * 1000).toLocaleString() : "";
  const alt = coords[2] != null ? coords[2].toFixed(1) + " m" : "";
  const spd = replay.graphSpeeds && replay.graphSpeeds[idx] != null
    ? replay.graphSpeeds[idx].toFixed(1) + " m/s" : "";
  document.getElementById("replay-info").innerHTML =
    `<span>${props.filename || ""}</span><span>${alt}</span><span>${spd}</span><span>${time}</span>`;

  updateGraphPlayhead(idx);
}

function updateGraphPlayhead(idx) {
  if (!replay.graphAltitudes) return;
  const total = replay.shots.length;
  for (const id of ["graph-altitude", "graph-speed"]) {
    const canvas = document.getElementById(id);
    const values = id === "graph-altitude" ? replay.graphAltitudes : replay.graphSpeeds;
    const color = id === "graph-altitude" ? "#4aa3ff" : "#3fb950";
    const label = id === "graph-altitude" ? "Altitude (m)" : "Speed (m/s)";
    drawSparkline(id, values, color, label);
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const x = (idx / (total - 1)) * w * dpr;
    ctx.strokeStyle = "rgba(255,255,255,0.5)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h * dpr);
    ctx.stroke();
  }
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

document.querySelector('.page-tab[data-page="results"]').addEventListener("click", () => {
  setTimeout(() => {
    if (replay.shots.length && !replay.initialized) initReplayMap();
    if (replay.map) {
      replay.map.invalidateSize();
      const latlngs = replay.shots.map(s => [s.geometry.coordinates[1], s.geometry.coordinates[0]]);
      replay.map.fitBounds(L.latLngBounds(latlngs).pad(0.1));
    }
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
  const base = `${location.origin}/files/${project.name}/`;
  const L = project.layers;
  const allTagged = stats.geotagged === stats.photos;
  const date = new Date().toLocaleDateString(undefined, {
    year: "numeric", month: "long", day: "numeric",
  });

  const modelCanvas = document.getElementById("model-canvas");
  let modelSnap = "";
  if (modelCanvas && modelCanvas.width > 0) {
    const tmp = document.createElement("canvas");
    const tmpCtx = tmp.getContext("2d");
    tmp.width = modelCanvas.width;
    tmp.height = modelCanvas.height;
    tmpCtx.drawImage(modelCanvas, 0, 0);
    const px = tmpCtx.getImageData(0, 0, tmp.width, tmp.height).data;
    let t = tmp.height, b = 0, l = tmp.width, r = 0;
    for (let y = 0; y < tmp.height; y++) {
      for (let x = 0; x < tmp.width; x++) {
        const i = (y * tmp.width + x) * 4;
        if (px[i] > 10 || px[i + 1] > 10 || px[i + 2] > 10) {
          if (y < t) t = y; if (y > b) b = y;
          if (x < l) l = x; if (x > r) r = x;
        }
      }
    }
    if (r > l && b > t) {
      const pad = 20;
      l = Math.max(0, l - pad); t = Math.max(0, t - pad);
      r = Math.min(tmp.width - 1, r + pad); b = Math.min(tmp.height - 1, b + pad);
      const crop = document.createElement("canvas");
      crop.width = r - l + 1; crop.height = b - t + 1;
      const cc = crop.getContext("2d");
      cc.fillStyle = "#111";
      cc.fillRect(0, 0, crop.width, crop.height);
      cc.drawImage(tmp, l, t, crop.width, crop.height, 0, 0, crop.width, crop.height);
      modelSnap = crop.toDataURL("image/png");
    } else {
      modelSnap = modelCanvas.toDataURL("image/png");
    }
  }

  function layerImg(key, alt, cls) {
    return L[key] ? `<img class="${cls}" src="${base}${L[key]}" alt="${alt}">` : "";
  }

  function stat(label, value, sub) {
    return `<div class="stat"><div class="stat-val">${value}</div><div class="stat-label">${label}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`;
  }

  const html = `<!doctype html>
<html><head><meta charset="utf-8">
<title>Survey Report — ${name}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:#1a1a1a;max-width:900px;margin:0 auto;padding:0 36px 40px}
@media print{body{padding:0 20px 20px}
  .page-break{page-break-before:always}
  .no-break{page-break-inside:avoid}}

/* header */
.header{padding:32px 0 24px;border-bottom:3px solid #2563eb;margin-bottom:28px}
.header h1{font-size:28px;font-weight:700;color:#111}
.header .sub{display:flex;gap:24px;margin-top:6px;font-size:14px;color:#666}

/* stat cards */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:32px}
.stat{background:#f7f8fa;border-radius:8px;padding:16px 18px;border:1px solid #e5e7eb}
.stat-val{font-size:22px;font-weight:700;color:#111}
.stat-label{font-size:12px;text-transform:uppercase;letter-spacing:0.5px;color:#888;margin-top:2px}
.stat-sub{font-size:12px;color:#666;margin-top:4px}

/* sections */
h2{font-size:18px;font-weight:600;color:#111;margin:28px 0 14px;padding-bottom:8px;border-bottom:2px solid #e5e7eb}
.section-note{font-size:13px;color:#666;margin:-8px 0 14px;line-height:1.5}

/* images */
.layer{margin-bottom:28px}
.layer img.full{width:100%;border-radius:6px;border:1px solid #e5e7eb}
.layer-with-legend{display:flex;flex-direction:column;gap:12px}
.layer-with-legend img.full{width:100%;min-width:0}
.layer-with-legend .legend-wrap{display:flex;align-items:center;gap:10px}
.layer-with-legend .legend-wrap img{height:20px;width:100%;max-width:400px;border-radius:4px;border:1px solid #e5e7eb}
.layer-with-legend .legend-wrap span{font-size:11px;color:#888;white-space:nowrap}
.layer .caption{font-size:13px;color:#666;margin-top:8px;font-style:italic}

/* model snapshot */
.model-snap{width:100%;max-height:400px;object-fit:contain;border-radius:6px;border:1px solid #e5e7eb;background:#111}

/* tables */
table{border-collapse:collapse;width:100%;margin:0 0 20px}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;color:#888;padding:6px 14px;border-bottom:2px solid #e5e7eb}
td{padding:10px 14px;border-bottom:1px solid #f0f0f0;font-size:14px}
td:first-child{color:#555;font-weight:500}
tr:last-child td{border-bottom:none}

/* products */
.products{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 28px}
.product{display:inline-block;padding:8px 16px;background:#f0f4ff;color:#2563eb;border-radius:6px;font-size:13px;font-weight:500;text-decoration:none;border:1px solid #dbeafe}

/* footer */
.footer{margin-top:40px;padding-top:16px;border-top:2px solid #e5e7eb;color:#aaa;font-size:12px;display:flex;justify-content:space-between}
</style></head><body>

<div class="header">
  <h1>Survey Report — ${name}</h1>
  <div class="sub"><span>${date}</span><span>${stats.photos} photos</span><span>${Math.round(stats.extent_m.width)} × ${Math.round(stats.extent_m.height)} m</span></div>
</div>

<div class="stats">
  ${stat("Photos captured", stats.photos)}
  ${stat("Geotagged", `${stats.geotagged} / ${stats.photos}`, allTagged ? "All photos have GPS" : "Some photos lack GPS")}
  ${stat("Camera heading", `${Math.round((stats.with_heading / stats.geotagged) * 100)}%`, stats.with_heading > 0 ? "Recorded" : "Not recorded by camera")}
  ${stat("Area covered", `${Math.round(stats.extent_m.width)} × ${Math.round(stats.extent_m.height)} m`)}
  ${stat("Mean altitude", `${stats.altitude.mean.toFixed(0)} m`, "Above sea level")}
  ${stat("Photo spacing", stats.spacing_m.median ? stats.spacing_m.median.toFixed(1) + " m" : "—", "Median between shots")}
</div>

<h2>Orthomosaic</h2>
<p class="section-note">Every photo reprojected as though shot from directly overhead, then stitched. Distances and areas measured on this image are true to the ground.</p>
<div class="layer">
  ${layerImg("ortho", "Orthomosaic", "full")}
</div>

${L.elevation ? `
<h2>Elevation model</h2>
<p class="section-note">Digital surface model — the height of the ground and everything standing on it. Colour runs from low to high.</p>
<div class="layer page-break no-break">
  <div class="layer-with-legend">
    ${layerImg("elevation", "Elevation map", "full")}
    ${L.elevation_legend ? `<div class="legend-wrap"><img src="${base}${L.elevation_legend}" alt="Legend"><span>Elevation scale</span></div>` : ""}
  </div>
</div>
` : ""}

${L.overlap ? `
<h2>Photo overlap</h2>
<p class="section-note">How many photos see each point. Green is well-covered; yellow and red mark thin coverage where reconstruction degrades.</p>
<div class="layer no-break">
  <div class="layer-with-legend">
    ${layerImg("overlap", "Photo overlap", "full")}
    ${L.overlap_legend ? `<div class="legend-wrap"><img src="${base}${L.overlap_legend}" alt="Legend"><span>Overlap count</span></div>` : ""}
  </div>
</div>
` : ""}

${L.cameras ? `
<h2>Camera positions</h2>
<p class="section-note">Where each photo was taken. Positions are the solved camera locations from the reconstruction, joined in capture order.</p>
<div class="layer no-break">
  ${layerImg("cameras", "Camera positions", "full")}
</div>
` : ""}

${modelSnap ? `
<h2>3D reconstruction</h2>
<p class="section-note">Textured mesh built from overlapping photo coverage.</p>
<div class="layer page-break no-break">
  <img class="model-snap" src="${modelSnap}" alt="3D model">
</div>
` : ""}

<h2 class="page-break">Survey extent</h2>
<table>
  <tr><th colspan="2">Location</th></tr>
  <tr><td>Centre</td><td>${stats.centre.lat.toFixed(6)}, ${stats.centre.lon.toFixed(6)}</td></tr>
  <tr><td>North / South</td><td>${stats.bounds.north.toFixed(6)} / ${stats.bounds.south.toFixed(6)}</td></tr>
  <tr><td>East / West</td><td>${stats.bounds.east.toFixed(6)} / ${stats.bounds.west.toFixed(6)}</td></tr>
</table>
<table>
  <tr><th colspan="2">Flight parameters</th></tr>
  <tr><td>Altitude range</td><td>${stats.altitude.min.toFixed(1)} – ${stats.altitude.max.toFixed(1)} m</td></tr>
  <tr><td>Mean altitude</td><td>${stats.altitude.mean.toFixed(1)} m</td></tr>
  <tr><td>Spacing (median)</td><td>${stats.spacing_m.median != null ? stats.spacing_m.median.toFixed(1) + " m" : "—"}</td></tr>
  <tr><td>Spacing range</td><td>${stats.spacing_m.min != null ? stats.spacing_m.min.toFixed(1) + " – " + stats.spacing_m.max.toFixed(1) + " m" : "—"}</td></tr>
</table>

<h2>Data products</h2>
<div class="products">${Object.entries(project.products).filter(([, p]) => p).map(([k]) =>
  `<span class="product">${k.replace(/_/g, " ")}</span>`).join("")}
</div>

<div class="footer">
  <span>Generated by drone-gcs</span>
  <span>${name} — ${date}</span>
</div>
<script>window.onload=()=>setTimeout(()=>window.print(),800)<\/script>
</body></html>`;

  const w = window.open("", "_blank");
  w.document.write(html);
  w.document.close();
}

document.getElementById("btn-export").addEventListener("click", exportReport);

loadProjects();
