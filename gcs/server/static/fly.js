/* Aircraft link, pre-flight gating, mission upload, and the live map.
 *
 * The laptop has no wire to the flight controller — everything here travels
 * through the companion computer over WiFi. That link is expected to drop out
 * over the far end of a survey, so a dropout is reported and recovered from
 * rather than treated as a failure. The aircraft is flying the mission itself. */

(function () {
  "use strict";

  const POLL_MS = 2000;          // faster than the 5 s the brief asked for
  const STALE_AFTER_MS = 12000;  // after this, say so plainly
  const MAX_TRACK_POINTS = 5000; // roughly three hours of movement at this rate

  const fly = {
    connected: false,
    lastFix: 0,
    marker: null,
    track: null,
    trackPoints: [],
    timer: null,
    polling: false,
  };

  const el = (id) => document.getElementById(id);

  // -- connection ---------------------------------------------------------

  el("btn-connect").addEventListener("click", async () => {
    const url = el("in-companion").value.trim();
    setLink("Connecting…", "muted");
    try {
      const response = await fetch("/api/companion/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url || null }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "connection failed");

      fly.connected = true;
      el("in-companion").value = data.url;
      const h = data.health;
      setSimulatorBanner(h.simulated, h.simulated_reasons, h.fc_endpoint);
      setLink(
        `Connected to ${data.url} — ${h.camera}, ${h.free_disk_gb} GB free, ` +
        `flight controller ${h.link.connected ? "up" : "DOWN"}.`,
        h.link.connected ? "good" : "warn"
      );
      startPolling();
    } catch (error) {
      setLink(String(error.message || error), "bad");
    }
  });

  el("btn-disconnect").addEventListener("click", async () => {
    await fetch("/api/companion/disconnect", { method: "POST" });
    fly.connected = false;
    stopPolling();
    setLink("Not connected.", "muted");
    setSimulatorBanner(false);
    el("telemetry").classList.add("hidden");
    el("preflight").innerHTML = "";
    el("btn-upload").disabled = true;
    clearAircraft();
  });

  function setLink(text, tone) {
    const node = el("link-status");
    node.textContent = text;
    node.className = `link-status ${tone}`;
  }

  function setSimulatorBanner(simulated, reasons, endpoint) {
    const banner = el("sim-banner");
    if (!simulated) {
      banner.classList.add("hidden");
      return;
    }
    banner.classList.remove("hidden");
    banner.innerHTML =
      `<b>SIMULATION — THIS IS NOT A REAL AIRCRAFT.</b> ` +
      `<span>Every reading below describes a simulation: ${reasons.join("; ")}. ` +
      `Connected to <code>${endpoint}</code>.</span>`;
  }

  // -- telemetry polling --------------------------------------------------

  function startPolling() {
    stopPolling();
    poll();
    fly.timer = setInterval(poll, POLL_MS);
  }

  function stopPolling() {
    if (fly.timer) clearInterval(fly.timer);
    fly.timer = null;
  }

  async function poll() {
    // A slow or hanging request must not let the next tick start another. On a
    // degraded link requests take longer than the interval, and without this
    // they pile up until the browser is doing nothing else.
    if (fly.polling) return;
    fly.polling = true;

    let data;
    try {
      data = await (await fetch("/api/companion/status")).json();
    } catch {
      return showStale();
    } finally {
      fly.polling = false;
    }

    if (!data.connected) return showStale(data.error);

    fly.lastFix = Date.now();
    showTelemetry(data);
    updateAircraft(data.vehicle);
  }

  function showStale(detail) {
    const age = fly.lastFix ? (Date.now() - fly.lastFix) / 1000 : null;
    if (age === null || age * 1000 > STALE_AFTER_MS) {
      setLink(
        `Link lost${detail ? " — " + detail : ""}. The aircraft is flying the ` +
        "mission from its own memory and will return on its own.",
        "warn"
      );
    }
  }

  function showTelemetry(data) {
    const v = data.vehicle;
    const s = data.session;
    const panel = el("telemetry");
    panel.classList.remove("hidden");

    const cells = [
      ["Mode", v.mode + (v.armed ? " · ARMED" : "")],
      ["Altitude", v.relative_alt_m == null ? "—" : `${v.relative_alt_m.toFixed(1)} m`],
      ["Speed", v.ground_speed_ms == null ? "—" : `${v.ground_speed_ms.toFixed(1)} m/s`],
      ["Battery", v.battery_v == null ? "—" : `${v.battery_v.toFixed(1)} V`],
      ["GPS", `${v.gps_fix} · ${v.satellites} sats`],
      ["Waypoint", v.mission_seq == null ? "—" : v.mission_seq],
      ["Photos", s ? `${s.captured}${s.failed ? ` (${s.failed} failed)` : ""}` : "—"],
      ["HDOP", v.hdop == null ? "—" : v.hdop.toFixed(1)],
    ];

    panel.innerHTML = cells
      .map(([k, val]) => `<div class="tele"><span>${k}</span><b>${val}</b></div>`)
      .join("");
  }

  // -- live map -----------------------------------------------------------

  function updateAircraft(v) {
    if (v.lat == null || v.lon == null) return;
    const map = window.planner.map;
    const position = [v.lat, v.lon];

    if (!fly.marker) {
      fly.marker = L.marker(position, {
        // The arrow lives in a child element. Leaflet owns the marker's own
        // transform for positioning, so rotating that fights it — and writing
        // to it repeatedly appends rather than replaces, which grows the style
        // string without limit until the renderer stalls.
        icon: L.divIcon({
          className: "aircraft",
          html: '<div class="aircraft-arrow"></div>',
          iconSize: [18, 18],
          iconAnchor: [9, 9],
        }),
        zIndexOffset: 2000,
      }).addTo(map);
      fly.track = L.polyline([], { color: "#3fb950", weight: 2 }).addTo(map);
      map.setView(position, Math.max(map.getZoom(), 17));
    }

    fly.marker.setLatLng(position);
    if (v.heading_deg != null) {
      const arrow = fly.marker.getElement()?.querySelector(".aircraft-arrow");
      if (arrow) arrow.style.transform = `rotate(${v.heading_deg}deg)`;
    }

    // Only record real movement, so a stationary aircraft does not accumulate
    // thousands of identical points.
    const last = fly.trackPoints[fly.trackPoints.length - 1];
    if (!last || Math.abs(last[0] - v.lat) > 1e-6 || Math.abs(last[1] - v.lon) > 1e-6) {
      fly.trackPoints.push(position);
      // Bound the track. A long survey would otherwise grow it without limit,
      // and redrawing a polyline of many thousands of points every couple of
      // seconds gets expensive.
      if (fly.trackPoints.length > MAX_TRACK_POINTS) {
        fly.trackPoints.splice(0, fly.trackPoints.length - MAX_TRACK_POINTS);
      }
      fly.track.setLatLngs(fly.trackPoints);
    }
  }

  function clearAircraft() {
    const map = window.planner.map;
    if (fly.marker) map.removeLayer(fly.marker);
    if (fly.track) map.removeLayer(fly.track);
    fly.marker = fly.track = null;
    fly.trackPoints = [];
  }

  // -- pre-flight ---------------------------------------------------------

  el("btn-preflight").addEventListener("click", runPreflight);

  async function runPreflight() {
    const plan = window.planner.lastPlan;
    const waypoints = plan ? plan.waypoints.length : 0;
    const photos = plan ? plan.stats.photo_count : 0;
    const panel = el("preflight");
    panel.innerHTML = '<p class="muted">Checking…</p>';

    let report;
    try {
      const response = await fetch(
        `/api/companion/preflight?mission_waypoints=${waypoints}&estimated_photos=${photos}`
      );
      report = await response.json();
      if (!response.ok) throw new Error(report.detail || "check failed");
    } catch (error) {
      panel.innerHTML = `<p class="check bad">${error.message || error}</p>`;
      el("btn-upload").disabled = true;
      return;
    }

    setSimulatorBanner(report.simulated, report.simulated_reasons, "");

    panel.innerHTML =
      (report.simulated
        ? '<p class="check warn"><b>Simulated aircraft</b><span>These results ' +
          'describe a simulation, not your drone.</span></p>'
        : "") +
      `<p class="preflight-summary ${report.ready_to_arm ? "good" : "bad"}">
         ${report.summary}
       </p>` +
      report.checks
        .map(
          (c) => `<div class="check ${c.passed ? "good" : c.blocking ? "bad" : "warn"}">
            <b>${c.name}</b><span>${c.detail}</span></div>`
        )
        .join("");

    // Upload is gated on the plan existing, not on the checklist passing: a
    // mission can legitimately be loaded before GPS has settled. Arming is what
    // the checklist actually guards, and that happens on the aircraft.
    el("btn-upload").disabled = !plan;
  }

  // -- mission upload -----------------------------------------------------

  el("btn-upload").addEventListener("click", async () => {
    const plan = window.planner.lastPlan;
    if (!plan) return;

    const result = el("upload-result");
    result.innerHTML = '<p class="muted">Uploading and verifying…</p>';

    try {
      const response = await fetch("/api/companion/mission", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          waypoints: plan.waypoints,
          altitude_m: Number(document.getElementById("in-altitude").value),
          trigger_distance_m: plan.stats.photo_spacing_m,
          home: plan.waypoints[0],
          // The drawn boundary becomes the fence exactly, so obstacles routed
          // around on the map are routed around in the air.
          boundary: window.planner.state.vertices,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "upload failed");

      const fence = data.fence
        ? data.fence.enabled
          ? `<p class="check good"><b>Geofence active</b><span>${data.fence.vertices}
             vertices, ${data.fence.margin_m} m beyond the flight path, ceiling
             ${data.fence.max_altitude_m} m. The autopilot enforces this itself —
             no link required.</span></p>`
          : `<p class="check warn"><b>Fence stored but not fully enabled</b>
             <span>Unconfirmed: ${data.fence.unconfirmed_parameters.join(", ")}.
             Check these on the aircraft before flying.</span></p>`
        : `<p class="check warn"><b>No geofence</b><span>The mission was uploaded
           without a boundary.</span></p>`;

      result.innerHTML = `<p class="check good"><b>Mission uploaded</b>
        <span>${data.items} items, verified by readback. Camera every
        ${data.trigger_distance_m} m.</span></p>` + fence;
    } catch (error) {
      result.innerHTML = `<p class="check bad"><b>Upload failed</b>
        <span>${error.message || error}</span></p>`;
    }
  });
})();
