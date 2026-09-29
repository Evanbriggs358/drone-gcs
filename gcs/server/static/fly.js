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
    lastBatteryWarn: null,
    seenMessages: 0,
    staleLevel: 0,
  };

  const el = (id) => document.getElementById(id);

  const TELE_LABELS = ["Mode", "Altitude", "Speed", "Battery", "GPS", "Wind", "Photos", "HDOP"];

  function renderSkeleton() {
    const panel = el("telemetry");
    panel.innerHTML = TELE_LABELS
      .map((k) => `<div class="tele"><span>${k}</span><b>&mdash;</b></div>`)
      .join("");
  }
  renderSkeleton();

  // -- connection ---------------------------------------------------------

  el("btn-connect").addEventListener("click", async () => {
    const url = el("in-companion").value.trim();
    setLink("Connecting…", "muted");
    try {
      const response = await fetch("api/companion/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url || null }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "connection failed");

      fly.connected = true;
      if (window.planner) {
        window.planner.flyState = window.planner.flyState || {};
        window.planner.flyState.connected = true;
        window.planner.updateSteps();
      }
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
    await fetch("api/companion/disconnect", { method: "POST" });
    fly.connected = false;
    if (window.planner) {
      window.planner.flyState = window.planner.flyState || {};
      window.planner.flyState.connected = false;
      window.planner.flyState.missionUploaded = false;
      window.planner.updateSteps();
    }
    stopPolling();
    setLink("Not connected.", "muted");
    setSimulatorBanner(false);
    el("telemetry").classList.add("skeleton");
    renderSkeleton();
    el("preflight").innerHTML = "";
    el("btn-upload").disabled = true;
    el("diag-link").innerHTML = '<span class="muted">No data yet</span>';
    el("diag-log").innerHTML = "";
    el("flight-controls").classList.add("hidden");
    stopPreview();
    el("camera-preview-container").classList.add("hidden");
    fly.seenMessages = 0;
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
      data = await (await fetch("api/companion/status")).json();
    } catch {
      return showStale();
    } finally {
      fly.polling = false;
    }

    if (!data.connected) return showStale(data.error, data.reconnecting);

    fly.lastFix = Date.now();
    fly.staleLevel = 0;
    showTelemetry(data);
    updateDiagnostics(data);
    updateAircraft(data.vehicle);
    updateFlightControls(data.vehicle.mode, data.vehicle.armed);
  }

  function showStale(detail, reconnecting) {
    const age = fly.lastFix ? (Date.now() - fly.lastFix) / 1000 : null;
    if (age === null || age * 1000 > STALE_AFTER_MS) {
      const suffix = reconnecting
        ? " Reconnecting…"
        : " The aircraft is flying the mission from its own memory and will return on its own.";
      setLink(
        `Link lost${age != null ? " (" + Math.round(age) + "s)" : ""}${detail ? " — " + detail : ""}.${suffix}`,
        age != null && age > 60 ? "bad" : "warn"
      );

      if (age != null && age > 15 && fly.staleLevel < 1) {
        fly.staleLevel = 1;
        toast("No telemetry for 15 seconds — link may be down", "warn");
      }
      if (age != null && age > 60 && fly.staleLevel < 2) {
        fly.staleLevel = 2;
        toast("No telemetry for 60 seconds — consider RTL", "bad");
      }
    }
  }

  function batteryGauge(pct, voltage) {
    if (pct == null && voltage == null) return "—";
    const level = pct != null ? pct : null;
    const tone = level == null ? "muted" : level > 40 ? "good" : level > 20 ? "warn" : "bad";
    const fill = level != null ? Math.max(0, Math.min(100, level)) : 0;
    const vStr = voltage != null ? voltage.toFixed(1) + " V" : "";
    const pStr = level != null ? level + "%" : "";
    const label = [pStr, vStr].filter(Boolean).join(" · ");
    return `<span class="batt-gauge"><span class="batt-shell"><span class="batt-fill ${tone}" style="width:${fill}%"></span></span></span>${label}`;
  }

  function checkBatteryWarning(v) {
    const pct = v.battery_remaining_pct;
    if (pct == null) return;
    if (pct <= 15 && fly.lastBatteryWarn !== "critical") {
      fly.lastBatteryWarn = "critical";
      toast("BATTERY CRITICAL — " + pct + "% — LAND NOW", "bad");
    } else if (pct <= 30 && pct > 15 && fly.lastBatteryWarn == null) {
      fly.lastBatteryWarn = "low";
      toast("Battery low — " + pct + "% remaining", "warn");
    } else if (pct > 40) {
      fly.lastBatteryWarn = null;
    }
  }

  function showTelemetry(data) {
    const v = data.vehicle;
    const s = data.session;
    const panel = el("telemetry");
    panel.classList.remove("skeleton");

    checkBatteryWarning(v);

    const cells = [
      ["Mode", v.mode + (v.armed ? " · ARMED" : "")],
      ["Altitude", v.relative_alt_m == null ? "—" : `${v.relative_alt_m.toFixed(1)} m`],
      ["Speed", v.ground_speed_ms == null ? "—" : `${v.ground_speed_ms.toFixed(1)} m/s`],
      ["Battery", batteryGauge(v.battery_remaining_pct, v.battery_v)],
      ["GPS", `${v.gps_fix} · ${v.satellites} sats`],
      ["Wind", v.wind_speed_ms != null ? `${v.wind_speed_ms.toFixed(1)} m/s` : "—"],
      ["Photos", s ? `${s.captured}${s.failed ? ` (${s.failed} failed)` : ""}` : "—"],
      ["HDOP", v.hdop == null ? "—" : v.hdop.toFixed(1)],
    ];

    panel.innerHTML = cells
      .map(([k, val]) => `<div class="tele"><span>${k}</span><b>${val}</b></div>`)
      .join("");
  }

  function updateDiagnostics(data) {
    const v = data.vehicle;
    const link = el("diag-link");
    const log = el("diag-log");

    const current = v.battery_current_a != null ? v.battery_current_a.toFixed(1) + " A" : "—";
    const pct = v.battery_remaining_pct != null ? v.battery_remaining_pct + "%" : "—";
    const voltage = v.battery_v != null ? v.battery_v.toFixed(2) + " V" : "—";
    link.innerHTML =
      `<b>Power</b> ${voltage} · ${current} · ${pct}` +
      (v.hdop != null ? ` &nbsp; <b>HDOP</b> ${v.hdop.toFixed(2)}` : "") +
      (data.session ? ` &nbsp; <b>Session</b> ${data.session.elapsed_s}s` : "");

    const messages = v.messages || [];
    if (messages.length > fly.seenMessages) {
      const newMsgs = messages.slice(fly.seenMessages);
      for (const msg of newMsgs) {
        const div = document.createElement("div");
        const lower = msg.toLowerCase();
        div.className = "msg" + (lower.includes("error") || lower.includes("fail") ? " err"
          : lower.includes("warn") || lower.includes("bad") ? " warn" : "");
        div.textContent = msg;
        log.appendChild(div);
      }
      log.scrollTop = log.scrollHeight;
      fly.seenMessages = messages.length;
    }
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

  // -- flight controls ----------------------------------------------------

  async function sendMode(mode) {
    try {
      const response = await fetch("api/companion/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "mode change failed");
      toast(data.confirmed ? mode + " confirmed" : mode + " sent (unconfirmed)", data.confirmed ? "good" : "warn");
    } catch (error) {
      toast("Mode change failed: " + (error.message || error), "bad");
    }
  }

  el("btn-fc-pause").addEventListener("click", () => sendMode("LOITER"));
  el("btn-fc-resume").addEventListener("click", () => sendMode("AUTO"));
  el("btn-fc-rtl").addEventListener("click", () => sendMode("RTL"));
  el("btn-fc-land").addEventListener("click", () => sendMode("LAND"));

  el("btn-fc-arm").addEventListener("click", async () => {
    if (!confirm("Arm the motors? Propellers will spin.\n\nStand clear of the aircraft.")) return;
    try {
      const response = await fetch("api/companion/arm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ arm: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "arm failed");
      toast(data.accepted ? "Armed" : "Arm rejected — check pre-arm", data.accepted ? "good" : "warn");
    } catch (error) {
      toast("Arm failed: " + (error.message || error), "bad");
    }
  });

  el("btn-fc-disarm").addEventListener("click", async () => {
    if (!confirm("Disarm the motors?\n\nIf the aircraft is airborne, it WILL FALL.")) return;
    if (!confirm("CONFIRM DISARM\n\nThis immediately cuts all motor power.\nAre you absolutely sure?")) return;
    try {
      const response = await fetch("api/companion/arm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ arm: false }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "disarm failed");
      toast(data.accepted ? "Disarmed" : "Disarm rejected", data.accepted ? "good" : "warn");
    } catch (error) {
      toast("Disarm failed: " + (error.message || error), "bad");
    }
  });

  function updateFlightControls(mode, armed) {
    var panel = el("flight-controls");
    if (!fly.connected) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");

    var map = { LOITER: "btn-fc-pause", AUTO: "btn-fc-resume", RTL: "btn-fc-rtl", LAND: "btn-fc-land" };
    document.querySelectorAll(".fc-btn.pause,.fc-btn.resume,.fc-btn.rtl,.fc-btn.land").forEach(function (btn) { btn.classList.remove("active-mode"); });
    var active = map[mode];
    if (active) el(active).classList.add("active-mode");

    el("btn-fc-arm").classList.toggle("active-mode", !!armed);
    el("btn-fc-disarm").classList.toggle("active-mode", !armed);
  }

  // -- camera preview -----------------------------------------------------

  var previewTimer = null;

  el("btn-preview-start").addEventListener("click", function () {
    el("camera-preview-container").classList.remove("hidden");
    el("btn-preview-start").classList.add("hidden");
    el("btn-preview-stop").classList.remove("hidden");
    pollPreview();
    previewTimer = setInterval(pollPreview, 1000);
  });

  el("btn-preview-stop").addEventListener("click", stopPreview);

  function stopPreview() {
    if (previewTimer) clearInterval(previewTimer);
    previewTimer = null;
    el("btn-preview-stop").classList.add("hidden");
    el("btn-preview-start").classList.remove("hidden");
  }

  function pollPreview() {
    el("camera-preview").src = "api/companion/preview?" + Date.now();
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
        `api/companion/preflight?mission_waypoints=${waypoints}&estimated_photos=${photos}`
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
      const response = await fetch("api/companion/mission", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          waypoints: plan.waypoints,
          altitude_m: Number(document.getElementById("in-altitude").value),
          trigger_distance_m: plan.stats.photo_spacing_m,
          ground_speed_ms: Number(document.getElementById("in-speed").value),
          terrain: document.getElementById("in-terrain").checked,
          home: plan.waypoints[0],
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
      if (window.planner) {
        window.planner.flyState = window.planner.flyState || {};
        window.planner.flyState.missionUploaded = true;
        window.planner.updateSteps();
      }
    } catch (error) {
      result.innerHTML = `<p class="check bad"><b>Upload failed</b>
        <span>${error.message || error}</span></p>`;
    }
  });
  // -- post-flight: offload -------------------------------------------------

  el("btn-offload").addEventListener("click", async () => {
    if (!fly.connected) { toast("Connect to the companion first.", "warn"); return; }

    let sessions;
    try {
      const r = await fetch("api/companion/status");
      const status = await r.json();
      if (!status.connected) { toast("Companion not connected.", "warn"); return; }
    } catch { toast("Could not reach the server.", "bad"); return; }

    const sessionName = prompt("Session name to offload (leave blank for latest):");
    if (sessionName === null) return;

    const body = { session: sessionName || "latest" };
    const panel = el("offload-status");
    panel.innerHTML = '<span>Starting offload&hellip;</span>';
    el("btn-offload").disabled = true;

    try {
      const r = await fetch("api/offload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json();
        throw new Error(err.detail || "offload failed");
      }
      pollOffload(panel);
    } catch (e) {
      panel.innerHTML = `<span style="color:var(--bad)">${e.message}</span>`;
      el("btn-offload").disabled = false;
    }
  });

  function pollOffload(panel) {
    const poll = async () => {
      try {
        const r = await fetch("api/offload/status");
        const s = await r.json();
        if (s.running) {
          const pct = s.total ? Math.round((s.downloaded + s.skipped) / s.total * 100) : 0;
          panel.innerHTML = `<span>Offloading: ${s.downloaded + s.skipped}/${s.total} photos (${pct}%)</span>` +
            `<div class="bar"><div style="width:${pct}%"></div></div>`;
          setTimeout(poll, 1000);
        } else if (s.error) {
          panel.innerHTML = `<span style="color:var(--bad)">Offload failed: ${s.error}</span>`;
          el("btn-offload").disabled = false;
          toast("Offload failed.", "bad");
        } else {
          panel.innerHTML = `<span style="color:var(--good)">${s.summary || "Offload complete."}</span>`;
          el("btn-offload").disabled = false;
          toast("Photo offload complete.", "good");
        }
      } catch {
        panel.innerHTML = '<span style="color:var(--bad)">Lost connection during offload.</span>';
        el("btn-offload").disabled = false;
      }
    };
    poll();
  }

  // -- post-flight: reconstruct ---------------------------------------------

  el("btn-reconstruct").addEventListener("click", async () => {
    const projectName = prompt("Project name to reconstruct:");
    if (!projectName) return;

    const panel = el("reconstruct-status");
    panel.innerHTML = '<span>Starting reconstruction&hellip;</span>';
    el("btn-reconstruct").disabled = true;

    try {
      const r = await fetch("api/reconstruct", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: projectName }),
      });
      if (!r.ok) {
        const err = await r.json();
        throw new Error(err.detail || "reconstruction failed");
      }
      pollReconstruct(panel);
    } catch (e) {
      panel.innerHTML = `<span style="color:var(--bad)">${e.message}</span>`;
      el("btn-reconstruct").disabled = false;
    }
  });

  function pollReconstruct(panel) {
    const poll = async () => {
      try {
        const r = await fetch("api/reconstruct/status");
        const s = await r.json();
        if (s.running) {
          panel.innerHTML = `<span>${s.description || "Processing"}  (${s.percent}%)</span>` +
            `<div class="bar"><div style="width:${s.percent}%"></div></div>`;
          setTimeout(poll, 2000);
        } else if (s.error) {
          panel.innerHTML = `<span style="color:var(--bad)">Reconstruction failed: ${s.error}</span>`;
          el("btn-reconstruct").disabled = false;
          toast("Reconstruction failed.", "bad");
        } else if (s.complete) {
          panel.innerHTML = `<span style="color:var(--good)">Reconstruction complete.</span>`;
          el("btn-reconstruct").disabled = false;
          toast("Reconstruction complete.", "good");
        } else {
          panel.innerHTML = '';
          el("btn-reconstruct").disabled = false;
        }
      } catch {
        panel.innerHTML = '<span style="color:var(--bad)">Lost connection.</span>';
        el("btn-reconstruct").disabled = false;
      }
    };
    poll();
  }
})();
