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
    inputs.heading.disabled = inputs.autoHeading.checked;
  }

  Object.values(inputs).forEach((input) => {
    input.addEventListener("input", () => {
      syncLabels();
      replan();
    });
  });
  syncLabels();

  // -- planning -----------------------------------------------------------

  function replan() {
    if (state.vertices.length < 3) {
      clearPlan();
      document.getElementById("plan-stats").className = "plan-stats muted";
      document.getElementById("plan-stats").textContent =
        "Draw an area to see the flight plan.";
      document.getElementById("plan-warnings").innerHTML = "";
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
      measured_hover_min: inputs.hover.value ? Number(inputs.hover.value) : null,
      keep_inside: inputs.keepInside.checked,
    };

    let plan;
    try {
      const response = await fetch("/api/plan", {
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

    state.lastPlan = plan;
    window.planner.lastPlan = plan;   // the fly panel uploads whatever is shown

    if (inputs.autoHeading.checked && plan.stats.resolved_heading_deg != null) {
      inputs.heading.value = plan.stats.resolved_heading_deg;
      outputs.heading.textContent = `${plan.stats.resolved_heading_deg}°`;
    }

    drawPlan(plan);
    showStats(plan);
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

  function showStats(plan) {
    const s = plan.stats;
    const overSpeed = Number(inputs.speed.value) > s.max_ground_speed_ms;

    // Batteries is the number that decides whether a survey is flyable at all,
    // so it gets the strongest treatment: red over one pack, amber when tight.
    const batteryTone =
      s.batteries_needed > 1 ? "bad" : s.batteries_needed > 0.8 ? "warn" : "good";

    document.getElementById("plan-stats").className = "plan-stats";
    document.getElementById("plan-stats").innerHTML = `
      <div class="stat-grid">
        ${stat("Area", `${s.area_acres} ac`)}
        ${stat("Detail", `${s.gsd_cm_per_px} cm/px`)}
        ${stat("Photos", s.photo_count)}
        ${stat("Flight time", `${s.duration_min} min`)}
        ${stat("Batteries", s.batteries_needed.toFixed(2), batteryTone)}
        ${stat("Endurance", `${s.endurance_min} min`)}
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

  // The fly panel needs the map and the current plan; the rest is exposed for
  // debugging from the browser console.
  window.planner = { map, state, replan, lastPlan: null };
})();
