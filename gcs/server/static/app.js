import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";

const state = { project: null, view: "model", scene: null };

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
    setLayer(project, null);
    return;
  }

  bar.classList.remove("hidden");
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

loadProjects();
