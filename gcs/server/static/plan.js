/* Survey planning: draw an area on the map, see the flight plan appear.
 *
 * Every parameter change re-plans on the server, so ground sample distance,
 * photo count, and flight time update while a slider is moving rather than
 * being discovered after upload. */

(function () {
  "use strict";

  const state = {
    vertices: [],
    polygon: null,
    grid: null,
    fence: null,
    photoLayer: null,
    markers: [],
    pending: null,
    settingsCustomized: false,
    aircraftCustomized: false,
  };

  // Esri World Imagery: satellite basemap with no API key, which matters for a
  // tool that has to work from a field laptop.
  // preferCanvas matters here: a large survey produces thousands of photo
  // positions, and Leaflet's default SVG renderer makes one DOM element per
  // marker. Canvas keeps panning smooth when the count runs into the thousands.
  const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView(
    [47.6062, -122.3321],
    16
  );

  if ("geolocation" in navigator) {
    navigator.geolocation.getCurrentPosition(function (pos) {
      if (state.vertices.length === 0) {
        map.setView([pos.coords.latitude, pos.coords.longitude], 17);
      }
    });
  }

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 21, attribution: "Imagery &copy; Esri" }
  ).addTo(map);

  // -- drawing ------------------------------------------------------------

  map.on("click", (event) => {
    state.vertices.push([event.latlng.lat, event.latlng.lng]);
    redrawArea();
    replan();
  });

  document.getElementById("btn-undo").addEventListener("click", () => {
    state.vertices.pop();
    redrawArea();
    replan();
  });

  document.getElementById("btn-clear").addEventListener("click", () => {
    state.vertices = [];
    redrawArea();
    replan();
  });

  // Corner handles are real markers rather than circleMarkers: only markers can
  // be dragged, and canvas-rendered vectors are not DOM elements at all, so they
  // cannot be hovered or grabbed.
  const handleIcon = L.divIcon({
    className: "vertex-handle",
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });

  function renderMarkers() {
    state.markers.forEach((m) => map.removeLayer(m));
    state.markers = state.vertices.map((point, index) => {
      const marker = L.marker(point, {
        icon: handleIcon,
        draggable: true,
        keyboard: false,
        // Keep a dragged corner above its neighbours so it stays grabbable.
        zIndexOffset: 1000,
      }).addTo(map);

      marker.bindTooltip(`Corner ${index + 1} — drag to move`, { direction: "top" });

      marker.on("drag", (event) => {
        const { lat, lng } = event.target.getLatLng();
        state.vertices[index] = [lat, lng];
        // Only the outline follows the cursor. Re-planning the whole grid on
        // every drag frame would stutter on a large survey, so that waits for
        // the drag to finish.
        renderPolygon();
      });

      marker.on("dragend", () => {
        renderPolygon();
        replan();
      });

      // A click that ends a drag must not also drop a new corner on the map.
      marker.on("click", (event) => L.DomEvent.stopPropagation(event));

      return marker;
    });
  }

  function renderPolygon() {
    if (state.polygon) map.removeLayer(state.polygon);
    state.polygon = null;

    if (state.vertices.length >= 3) {
      state.polygon = L.polygon(state.vertices, {
        color: "#4aa3ff",
        weight: 2,
        fillOpacity: 0.08,
      }).addTo(map);
    } else if (state.vertices.length === 2) {
      state.polygon = L.polyline(state.vertices, {
        color: "#4aa3ff",
        weight: 2,
        dashArray: "4 4",
      }).addTo(map);
    }
  }

  function updateHint() {
    const count = state.vertices.length;
    document.getElementById("draw-hint").textContent =
      count === 0
        ? "Click on the map to place corners. Three or more makes a survey area."
        : `${count} corner${count === 1 ? "" : "s"} placed. Drag any corner to adjust it.`;
  }

  function redrawArea() {
    renderMarkers();
    renderPolygon();
    updateHint();
    updateSteps();
  }

  // -- parameters ---------------------------------------------------------

  const inputs = {
    altitude: document.getElementById("in-altitude"),
    speed: document.getElementById("in-speed"),
    front: document.getElementById("in-front"),
    side: document.getElementById("in-side"),
    heading: document.getElementById("in-heading"),
    pattern: document.getElementById("in-pattern"),
    weight: document.getElementById("in-weight"),
    hover: document.getElementById("in-hover"),
    keepInside: document.getElementById("in-keep-inside"),
    autoHeading: document.getElementById("in-auto-heading"),
    wind: document.getElementById("in-wind"),
    terrain: document.getElementById("in-terrain"),
  };

  const outputs = {
    altitude: document.getElementById("out-altitude"),
    speed: document.getElementById("out-speed"),
    front: document.getElementById("out-front"),
    side: document.getElementById("out-side"),
    heading: document.getElementById("out-heading"),
    weight: document.getElementById("out-weight"),
  };

  function syncLabels() {
    outputs.altitude.textContent = `${inputs.altitude.value} m`;
    outputs.speed.textContent = `${inputs.speed.value} m/s`;
    outputs.front.textContent = `${inputs.front.value}%`;
    outputs.side.textContent = `${inputs.side.value}%`;
    outputs.heading.textContent = inputs.autoHeading.checked
      ? `auto`
      : `${inputs.heading.value}°`;
    outputs.weight.textContent = `${Number(inputs.weight.value).toFixed(2)} kg`;
    document.getElementById("out-wind").textContent = `${inputs.wind.value} m/s`;
    inputs.heading.disabled = inputs.autoHeading.checked;
    document.getElementById("terrain-note").classList.toggle("hidden", !inputs.terrain.checked);
  }

  Object.values(inputs).forEach((input) => {
    input.addEventListener("input", () => {
      syncLabels();
      replan();
      updateCoverage(state.lastPlan);
    });
  });

  [inputs.altitude, inputs.speed, inputs.front, inputs.side, inputs.heading,
   inputs.autoHeading, inputs.pattern, inputs.keepInside, inputs.wind, inputs.terrain].forEach((inp) => {
    inp.addEventListener("input", () => { state.settingsCustomized = true; updateSteps(); });
  });
  [inputs.weight, inputs.hover].forEach((inp) => {
    inp.addEventListener("input", () => { state.aircraftCustomized = true; updateSteps(); });
  });

  syncLabels();

  // -- planning -----------------------------------------------------------

  function replan() {
    if (state.vertices.length < 3) {
      clearPlan();
      state.lastPlan = null;
      if (window.planner) window.planner.lastPlan = null;
      document.getElementById("plan-stats").className = "plan-stats muted";
      document.getElementById("plan-stats").textContent =
        "Draw an area to see the flight plan.";
      document.getElementById("plan-warnings").innerHTML = "";
      updateSteps();
      return;
    }

    // Sliders fire continuously; only the last request matters.
    clearTimeout(state.pending);
    state.pending = setTimeout(sendPlan, 120);
  }

  async function sendPlan() {
    const body = {
      polygon: state.vertices,
      altitude_m: Number(inputs.altitude.value),
      ground_speed_ms: Number(inputs.speed.value),
      front_overlap: Number(inputs.front.value) / 100,
      side_overlap: Number(inputs.side.value) / 100,
      heading_deg: Number(inputs.heading.value),
      auto_heading: inputs.autoHeading.checked,
      pattern: inputs.pattern.value,
      all_up_weight_kg: Number(inputs.weight.value),
      wind_speed_ms: Number(inputs.wind.value),
      measured_hover_min: inputs.hover.value ? Number(inputs.hover.value) : null,
      keep_inside: inputs.keepInside.checked,
      terrain: inputs.terrain.checked,
    };

    let plan;
    if (typeof Planner !== "undefined") {
      try { plan = Planner.planSurvey(body); } catch (e) { plan = null; }
    }
    if (!plan) {
      try {
        const response = await fetch("api/plan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) throw new Error(await response.text());
        plan = await response.json();
      } catch (error) {
        document.getElementById("plan-stats").textContent = "Could not build a plan.";
        return;
      }
    }

    state.lastPlan = plan;
    window.planner.lastPlan = plan;   // the fly panel uploads whatever is shown

    if (inputs.autoHeading.checked && plan.stats.resolved_heading_deg != null) {
      inputs.heading.value = plan.stats.resolved_heading_deg;
      outputs.heading.textContent = `${plan.stats.resolved_heading_deg}°`;
    }

    drawPlan(plan);
    showStats(plan);
    updateCoverage(plan);
    var stepPlan = document.getElementById("step-plan");
    if (stepPlan) stepPlan.open = true;
    updateSteps();
  }

  function clearPlan() {
    if (state.grid) map.removeLayer(state.grid);
    if (state.photoLayer) map.removeLayer(state.photoLayer);
    if (state.fence) map.removeLayer(state.fence);
    state.grid = state.photoLayer = state.fence = null;
  }

  function drawPlan(plan) {
    clearPlan();

    // Flight lines: waypoints arrive as consecutive pairs, one pair per line.
    const lines = [];
    for (let i = 0; i + 1 < plan.waypoints.length; i += 2) {
      lines.push([plan.waypoints[i], plan.waypoints[i + 1]]);
    }
    state.grid = L.polyline(lines, { color: "#ffcc44", weight: 2, opacity: 0.9 }).addTo(map);

    // The geofence the aircraft will be given. Drawn dashed and red so it reads
    // as a limit rather than part of the plan, and sitting outside the flight
    // lines because turns overshoot the last waypoint.
    if (plan.fence) {
      const enclosed = plan.fence.encloses_flight;
      state.fence = L.polygon(plan.fence.vertices, {
        // Green when the flight genuinely fits inside; red when waypoints spill
        // out and the fence would abort the mission.
        color: enclosed ? "#3fb950" : "#f85149",
        weight: 2,
        dashArray: "8 6",
        fill: false,
      })
        .addTo(map)
        .bindTooltip(
          enclosed
            ? `Geofence — the whole flight stays inside. Ceiling ${plan.fence.max_altitude_m} m.`
            : `Geofence — ${plan.fence.waypoints_outside} waypoints fall OUTSIDE. ` +
              "This would abort the mission.",
          { sticky: true }
        );
    }

    // Photo positions, drawn small so a few hundred stay legible.
    state.photoLayer = L.layerGroup(
      plan.photo_points.map((point) =>
        L.circleMarker(point, {
          radius: 1.6,
          color: "#ff5f56",
          weight: 0,
          fillOpacity: 0.85,
        })
      )
    ).addTo(map);
  }

  // -- coverage quality ----------------------------------------------------

  function updateCoverage(plan) {
    var front = Number(inputs.front.value) / 100;
    var side = Number(inputs.side.value) / 100;
    var avgViews = 1 / ((1 - front) * (1 - side));
    if (inputs.pattern.value === "crosshatch") avgViews *= 2;
    var rounded = Math.round(avgViews * 10) / 10;

    var numEl = document.getElementById("coverage-number");
    var qualEl = document.getElementById("coverage-quality");
    numEl.textContent = rounded;

    var label, cls;
    if (avgViews >= 9) { label = "Excellent"; cls = "q-great"; }
    else if (avgViews >= 5) { label = "Good"; cls = "q-good"; }
    else if (avgViews >= 3) { label = "Marginal"; cls = "q-ok"; }
    else { label = "Insufficient"; cls = "q-bad"; }
    qualEl.textContent = label;
    qualEl.className = "coverage-quality " + cls;

    if (plan && plan.photo_points && plan.photo_points.length > 0 && plan.stats) {
      renderHeatmap(plan);
    } else {
      var canvas = document.getElementById("coverage-canvas");
      var ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  }

  var HEAT_COLORS = [
    [30, 30, 30],       // 0 views — dark
    [248, 81, 73],      // 1–2 — red
    [210, 153, 34],     // 3–4 — yellow
    [63, 185, 80],      // 5–8 — green
    [46, 160, 67],      // 9+  — deep green
  ];

  function heatColor(views) {
    if (views === 0) return HEAT_COLORS[0];
    if (views <= 2)  return HEAT_COLORS[1];
    if (views <= 4)  return HEAT_COLORS[2];
    if (views <= 8)  return HEAT_COLORS[3];
    return HEAT_COLORS[4];
  }

  function renderHeatmap(plan) {
    var canvas = document.getElementById("coverage-canvas");
    var ctx = canvas.getContext("2d");
    var w = canvas.width;
    var h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    var photos = plan.photo_points;
    var acrossM = plan.stats.footprint_across_m;
    var alongM = plan.stats.footprint_along_m;
    if (!acrossM || !alongM) return;

    var halfAcross = acrossM / 2;
    var halfAlong = alongM / 2;

    // Build bounding box from the survey polygon in state.vertices
    var verts = state.vertices;
    if (verts.length < 3) return;

    var minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    for (var i = 0; i < verts.length; i++) {
      if (verts[i][0] < minLat) minLat = verts[i][0];
      if (verts[i][0] > maxLat) maxLat = verts[i][0];
      if (verts[i][1] < minLon) minLon = verts[i][1];
      if (verts[i][1] > maxLon) maxLon = verts[i][1];
    }

    // Convert footprint from metres to degrees (approximate)
    var mPerDegLat = 111320;
    var mPerDegLon = 111320 * Math.cos((minLat + maxLat) / 2 * Math.PI / 180);
    var dLat = halfAlong / mPerDegLat;
    var dLon = halfAcross / mPerDegLon;

    // Add padding for photos near edges
    minLat -= dLat; maxLat += dLat;
    minLon -= dLon; maxLon += dLon;

    var spanLat = maxLat - minLat;
    var spanLon = maxLon - minLon;
    if (spanLat === 0 || spanLon === 0) return;

    // Grid resolution — fit within canvas, keep cells square-ish
    var cellsX = w;
    var cellsY = h;

    // Build count grid
    var grid = new Uint8Array(cellsX * cellsY);

    for (var p = 0; p < photos.length; p++) {
      var pLat = photos[p][0];
      var pLon = photos[p][1];

      // Photo footprint bounds in grid coords
      var r0 = Math.floor(((pLat - dLat) - minLat) / spanLat * cellsY);
      var r1 = Math.ceil(((pLat + dLat) - minLat) / spanLat * cellsY);
      var c0 = Math.floor(((pLon - dLon) - minLon) / spanLon * cellsX);
      var c1 = Math.ceil(((pLon + dLon) - minLon) / spanLon * cellsX);

      r0 = Math.max(0, r0); r1 = Math.min(cellsY, r1);
      c0 = Math.max(0, c0); c1 = Math.min(cellsX, c1);

      for (var r = r0; r < r1; r++) {
        for (var c = c0; c < c1; c++) {
          var idx = (cellsY - 1 - r) * cellsX + c;
          if (grid[idx] < 255) grid[idx]++;
        }
      }
    }

    // Render to canvas
    var img = ctx.createImageData(w, h);
    var data = img.data;
    for (var i = 0; i < grid.length; i++) {
      var col = heatColor(grid[i]);
      var off = i * 4;
      data[off] = col[0];
      data[off + 1] = col[1];
      data[off + 2] = col[2];
      data[off + 3] = grid[i] === 0 ? 40 : 200;
    }
    ctx.putImageData(img, 0, 0);
  }

  function showStats(plan) {
    const s = plan.stats;
    const overSpeed = Number(inputs.speed.value) > s.max_ground_speed_ms;

    // Batteries is the number that decides whether a survey is flyable at all,
    // so it gets the strongest treatment: red over one pack, amber when tight.
    const effectiveBatt = s.wind_batteries_needed != null ? s.wind_batteries_needed : s.batteries_needed;
    const batteryTone =
      effectiveBatt > 1 ? "bad" : effectiveBatt > 0.8 ? "warn" : "good";

    const enduranceStr = s.wind_endurance_min != null
      ? `${s.wind_endurance_min} min <small style="color:var(--muted)">(${s.endurance_min} calm)</small>`
      : `${s.endurance_min} min`;
    const batteriesStr = s.wind_batteries_needed != null
      ? `${s.wind_batteries_needed.toFixed(2)} <small style="color:var(--muted)">(${s.batteries_needed.toFixed(2)} calm)</small>`
      : s.batteries_needed.toFixed(2);

    document.getElementById("plan-stats").className = "plan-stats";
    document.getElementById("plan-stats").innerHTML = `
      <div class="stat-grid">
        ${stat("Area", `${s.area_acres} ac`)}
        ${stat("Detail", `${s.gsd_cm_per_px} cm/px`)}
        ${stat("Photos", s.photo_count)}
        ${stat("Flight time", `${s.duration_min} min`)}
        ${stat("Batteries", batteriesStr, batteryTone)}
        ${stat("Endurance", enduranceStr)}
      </div>
      <table class="detail-table">
        <tr><td>Lines</td><td>${s.line_count}</td></tr>
        <tr><td>Distance</td><td>${(s.path_length_m / 1000).toFixed(1)} km</td></tr>
        <tr><td>Photo every</td><td>${s.photo_spacing_m} m</td></tr>
        <tr><td>Line spacing</td><td>${s.line_spacing_m} m</td></tr>
        <tr><td>Grid heading</td><td>${s.resolved_heading_deg}°</td></tr>
        <tr><td>Max shutter speed</td>
            <td class="${overSpeed ? "bad" : ""}">${s.max_ground_speed_ms} m/s</td></tr>
        <tr><td>Best survey speed</td><td>${s.best_survey_speed_ms} m/s</td></tr>
        ${s.wind_speed_ms > 0 ? `<tr><td>Wind</td><td>${s.wind_speed_ms} m/s</td></tr>` : ""}
      </table>`;

    document.getElementById("calibration-note").textContent = s.calibrated
      ? "Endurance calibrated to your measured hover time."
      : "Endurance is estimated from component figures. Fly a timed hover at real " +
        "weight and enter it here to replace the estimate with measurement.";

    document.getElementById("plan-warnings").innerHTML = plan.warnings
      .map((w) => `<p class="warning">${w}</p>`)
      .join("");
  }

  function stat(label, value, tone) {
    return `<div class="stat">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${tone || ""}">${value}</div>
    </div>`;
  }

  // -- page switching -----------------------------------------------------

  document.querySelectorAll(".page-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".page-tab").forEach((t) =>
        t.classList.toggle("active", t === tab)
      );
      document.querySelectorAll(".page").forEach((page) =>
        page.classList.toggle("active", page.id === `page-${tab.dataset.page}`)
      );
      // Leaflet renders nothing if it was sized while hidden.
      if (tab.dataset.page === "plan") setTimeout(() => map.invalidateSize(), 0);
    });
  });

  redrawArea();

  // -- flight mode toggle ---------------------------------------------------

  const modeNew = document.getElementById("btn-mode-new");
  const modeSaved = document.getElementById("btn-mode-saved");
  const savedPicker = document.getElementById("saved-flight-picker");
  const siteSelect = document.getElementById("site-select");
  const siteNameInput = document.getElementById("in-site-name");

  modeNew.addEventListener("click", () => {
    modeNew.classList.add("active");
    modeSaved.classList.remove("active");
    savedPicker.classList.add("hidden");

    state.vertices = [];
    state.settingsCustomized = false;
    state.aircraftCustomized = false;

    inputs.altitude.value = 20;
    inputs.speed.value = 2;
    inputs.front.value = 50;
    inputs.side.value = 50;
    inputs.heading.value = 0;
    inputs.autoHeading.checked = true;
    inputs.pattern.value = "nadir";
    inputs.keepInside.checked = true;
    inputs.wind.value = 0;
    inputs.weight.value = 0.8;
    inputs.hover.value = "";

    document.getElementById("in-companion").value = "";
    siteNameInput.value = "";

    syncLabels();
    redrawArea();
    replan();
  });

  modeSaved.addEventListener("click", () => {
    modeSaved.classList.add("active");
    modeNew.classList.remove("active");
    savedPicker.classList.remove("hidden");
  });

  // -- saved flights -------------------------------------------------------

  async function loadSiteList() {
    try {
      const sites = await (await fetch("api/sites")).json();
      siteSelect.innerHTML = '<option value="">— choose a saved flight —</option>';
      for (const site of sites) {
        const opt = document.createElement("option");
        opt.value = site.name;
        opt.textContent = site.name;
        opt.dataset.site = JSON.stringify(site);
        siteSelect.appendChild(opt);
      }
    } catch { /* not fatal */ }
  }

  function loadFlight(flight) {
    state.vertices = flight.polygon.map((p) => [p[0], p[1]]);
    state.settingsCustomized = true;
    state.aircraftCustomized = true;

    inputs.altitude.value = flight.altitude_m;
    inputs.speed.value = flight.ground_speed_ms;
    inputs.front.value = Math.round(flight.front_overlap * 100);
    inputs.side.value = Math.round(flight.side_overlap * 100);
    inputs.heading.value = flight.heading_deg;
    inputs.autoHeading.checked = flight.auto_heading;
    inputs.pattern.value = flight.pattern;
    inputs.keepInside.checked = flight.keep_inside;
    inputs.weight.value = flight.all_up_weight_kg;
    inputs.hover.value = flight.measured_hover_min != null ? flight.measured_hover_min : "";
    inputs.wind.value = flight.wind_speed_ms || 0;

    if (flight.companion_url) {
      document.getElementById("in-companion").value = flight.companion_url;
    }

    siteNameInput.value = flight.name;

    syncLabels();
    redrawArea();
    replan();

    if (state.vertices.length >= 3) {
      map.fitBounds(L.latLngBounds(state.vertices), { padding: [40, 40] });
    }

    document.querySelectorAll("#plan-panel > .step").forEach(function (s) {
      s.open = true;
    });

    toast("Flight loaded: " + flight.name, "good");
  }

  siteSelect.addEventListener("change", () => {
    const opt = siteSelect.selectedOptions[0];
    if (!opt || !opt.dataset.site) return;
    loadFlight(JSON.parse(opt.dataset.site));
  });

  document.getElementById("btn-site-save").addEventListener("click", async () => {
    const name = siteNameInput.value.trim();
    if (!name) { siteNameInput.focus(); toast("Enter a flight name first.", "warn"); return; }
    if (state.vertices.length < 3) { toast("Draw a survey area first (at least 3 points).", "warn"); return; }

    const body = {
      name,
      polygon: state.vertices,
      altitude_m: Number(inputs.altitude.value),
      front_overlap: Number(inputs.front.value) / 100,
      side_overlap: Number(inputs.side.value) / 100,
      ground_speed_ms: Number(inputs.speed.value),
      heading_deg: Number(inputs.heading.value),
      auto_heading: inputs.autoHeading.checked,
      pattern: inputs.pattern.value,
      keep_inside: inputs.keepInside.checked,
      all_up_weight_kg: Number(inputs.weight.value),
      measured_hover_min: inputs.hover.value ? Number(inputs.hover.value) : null,
      wind_speed_ms: Number(inputs.wind.value),
      companion_url: document.getElementById("in-companion").value.trim(),
    };

    try {
      const r = await fetch("api/sites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error((await r.json()).detail || "save failed");
      await loadSiteList();
      siteSelect.value = name;
      toast("Flight saved: " + name, "good");
    } catch (e) { toast("Could not save: " + e.message, "bad"); }
  });

  document.getElementById("btn-site-delete").addEventListener("click", async () => {
    const name = siteSelect.value;
    if (!name) return;
    if (!confirm('Delete flight "' + name + '"?')) return;
    try {
      const r = await fetch("api/sites/" + encodeURIComponent(name), { method: "DELETE" });
      if (!r.ok) throw new Error((await r.json()).detail || "delete failed");
      await loadSiteList();
      toast("Flight deleted.", "good");
    } catch (e) { toast("Could not delete: " + e.message, "bad"); }
  });

  loadSiteList();

  function updateSteps() {
    var steps = document.querySelectorAll("#plan-panel > .step");
    if (steps.length < 6) return;
    steps[0].classList.toggle("done", state.vertices.length >= 3);
    steps[1].classList.toggle("done", state.settingsCustomized);
    steps[2].classList.toggle("done", state.aircraftCustomized);
    var feasible = false;
    if (state.lastPlan && state.settingsCustomized && state.aircraftCustomized) {
      var s = state.lastPlan.stats;
      var batt = s.wind_batteries_needed != null ? s.wind_batteries_needed : s.batteries_needed;
      feasible = batt <= 1;
    }
    steps[3].classList.toggle("done", feasible);
    var fs = (window.planner && window.planner.flyState) || {};
    steps[4].classList.toggle("done", !!fs.connected);
    steps[5].classList.toggle("done", !!fs.missionUploaded);
  }

  // The fly panel needs the map and the current plan; the rest is exposed for
  // debugging from the browser console.
  window.planner = { map, state, replan, lastPlan: null, updateSteps };
  updateSteps();
  updateCoverage(null);
})();
