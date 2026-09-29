/* Client-side survey planner — a JavaScript port of gcs/planning/*.py and the
 * /api/plan endpoint, so the Plan tab works standalone on GitHub Pages. */

var Planner = (function () {
  "use strict";

  var M_PER_DEG_LAT = 111320;
  var GRAVITY = 9.81;
  var AIR_DENSITY = 1.225;

  // -- Camera ---------------------------------------------------------------

  var PI_CAMERA_MODULE_3 = {
    name: "Pi Camera Module 3 (standard)",
    image_width_px: 4608,
    image_height_px: 2592,
    pixel_pitch_um: 1.4,
    focal_length_mm: 4.74,
    long_axis_across_track: true,
    min_capture_interval_s: 1.0,
  };

  function cam_pixel_pitch_mm(c) { return c.pixel_pitch_um / 1000; }
  function cam_sensor_width_mm(c) { return c.image_width_px * cam_pixel_pitch_mm(c); }
  function cam_sensor_height_mm(c) { return c.image_height_px * cam_pixel_pitch_mm(c); }
  function cam_across_px(c) { return c.long_axis_across_track ? c.image_width_px : c.image_height_px; }
  function cam_along_px(c) { return c.long_axis_across_track ? c.image_height_px : c.image_width_px; }

  function cam_gsd_m(c, alt) { return alt * cam_pixel_pitch_mm(c) / c.focal_length_mm; }
  function cam_gsd_cm(c, alt) { return cam_gsd_m(c, alt) * 100; }

  function cam_footprint(c, alt) {
    var gsd = cam_gsd_m(c, alt);
    return { across: cam_across_px(c) * gsd, along: cam_along_px(c) * gsd };
  }

  function cam_photo_spacing(c, alt, front) {
    return cam_footprint(c, alt).along * (1 - front);
  }

  function cam_line_spacing(c, alt, side) {
    return cam_footprint(c, alt).across * (1 - side);
  }

  function cam_max_speed(c, alt, front) {
    return cam_photo_spacing(c, alt, front) / c.min_capture_interval_s;
  }

  // -- Geo ------------------------------------------------------------------

  function localFrame(points) {
    var lat = 0, lon = 0;
    for (var i = 0; i < points.length; i++) { lat += points[i][0]; lon += points[i][1]; }
    lat /= points.length; lon /= points.length;
    var mPerDegLon = M_PER_DEG_LAT * Math.cos(lat * Math.PI / 180);
    return {
      lat: lat, lon: lon, mPerDegLon: mPerDegLon,
      toLocal: function (p) {
        return [(p[1] - lon) * mPerDegLon, (p[0] - lat) * M_PER_DEG_LAT];
      },
      toLatLon: function (xy) {
        return [lat + xy[1] / M_PER_DEG_LAT, lon + xy[0] / mPerDegLon];
      }
    };
  }

  function rotate(xy, angle) {
    var c = Math.cos(angle), s = Math.sin(angle);
    return [xy[0] * c - xy[1] * s, xy[0] * s + xy[1] * c];
  }

  function polygonArea(pts) {
    if (pts.length < 3) return 0;
    var t = 0;
    for (var i = 0; i < pts.length; i++) {
      var j = (i + 1) % pts.length;
      t += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1];
    }
    return Math.abs(t) / 2;
  }

  // -- Power ----------------------------------------------------------------

  var DEADCAT_7IN = {
    all_up_weight_kg: 1.6,
    rotor_count: 4,
    rotor_diameter_m: 0.1778,
    figure_of_merit: 0.65,
    drag_area_m2: 0.025,
    drivetrain_efficiency: 0.80,
    avionics_power_w: 8.0,
    calibration: 1.0,
  };

  var CNHL_5000_6S = { capacity_mah: 5000, cells: 6, nominal_cell_v: 3.7, usable_fraction: 0.75 };

  function batt_voltage(b) { return b.cells * b.nominal_cell_v; }
  function batt_energy(b) { return b.capacity_mah / 1000 * batt_voltage(b); }
  function batt_usable(b) { return batt_energy(b) * b.usable_fraction; }

  function af_weight_n(a) { return a.all_up_weight_kg * GRAVITY; }
  function af_disc_area(a) { return a.rotor_count * Math.PI * Math.pow(a.rotor_diameter_m / 2, 2); }
  function af_drag_n(a, v) { return 0.5 * AIR_DENSITY * a.drag_area_m2 * v * v; }
  function af_thrust_n(a, v) { return Math.hypot(af_weight_n(a), af_drag_n(a, v)); }

  function af_induced_v(a, v) {
    var thrust = af_thrust_n(a, v);
    var hisq = thrust / (2 * AIR_DENSITY * af_disc_area(a));
    var vi = Math.sqrt(hisq);
    for (var i = 0; i < 100; i++) {
      var prev = vi;
      vi = hisq / Math.sqrt(v * v + vi * vi);
      vi = (vi + prev) / 2;
      if (Math.abs(vi - prev) < 1e-9) break;
    }
    return vi;
  }

  function af_power(a, v) {
    var induced = af_thrust_n(a, v) * af_induced_v(a, v);
    var parasitic = af_drag_n(a, v) * v;
    var mechanical = induced / a.figure_of_merit + parasitic;
    var propulsion = mechanical / a.drivetrain_efficiency;
    return propulsion * a.calibration + a.avionics_power_w;
  }

  function af_endurance(a, b, v) { return batt_usable(b) / af_power(a, v) * 60; }
  function af_range_km(a, b, v) { return v <= 0 ? 0 : v * af_endurance(a, b, v) * 60 / 1000; }

  function goldenMin(fn, lo, hi) {
    var invphi = (Math.sqrt(5) - 1) / 2;
    var a = lo, b = hi;
    var c = b - invphi * (b - a), d = a + invphi * (b - a);
    for (var i = 0; i < 200; i++) {
      if (fn(c) < fn(d)) { b = d; d = c; c = b - invphi * (b - a); }
      else { a = c; c = d; d = a + invphi * (b - a); }
      if (Math.abs(b - a) < 1e-4) break;
    }
    return (a + b) / 2;
  }

  function af_best_range_speed(a) {
    return goldenMin(function (v) { return af_power(a, v) / v; }, 0.5, 30);
  }

  function af_calibrate(a, measured_hover_min, battery) {
    var uncal = Object.assign({}, a, { calibration: 1 });
    var propulsion = af_power(uncal, 0) - a.avionics_power_w;
    var target_total = batt_usable(battery) / measured_hover_min * 60;
    var target_propulsion = target_total - a.avionics_power_w;
    if (target_propulsion <= 0) return a;
    return Object.assign({}, a, { calibration: target_propulsion / propulsion });
  }

  // -- Grid -----------------------------------------------------------------

  function scanlineSpans(polygon, y) {
    var crossings = [];
    var n = polygon.length;
    for (var i = 0; i < n; i++) {
      var x1 = polygon[i][0], y1 = polygon[i][1];
      var x2 = polygon[(i + 1) % n][0], y2 = polygon[(i + 1) % n][1];
      if (y1 === y2) continue;
      if ((y1 <= y && y < y2) || (y2 <= y && y < y1)) {
        crossings.push(x1 + (y - y1) / (y2 - y1) * (x2 - x1));
      }
    }
    crossings.sort(function (a, b) { return a - b; });
    var spans = [];
    for (var i = 0; i + 1 < crossings.length; i += 2) {
      if (crossings[i + 1] > crossings[i]) spans.push([crossings[i], crossings[i + 1]]);
    }
    return spans;
  }

  function flightLines(polygon, headingDeg, spacing, margin) {
    var angle = headingDeg * Math.PI / 180;
    var rotated = polygon.map(function (p) { return rotate(p, -angle); });
    var ys = rotated.map(function (p) { return p[1]; });
    var yMin = Math.min.apply(null, ys), yMax = Math.max.apply(null, ys);
    var lines = [];
    var y = yMin + spacing / 2;
    var flip = false;
    while (y <= yMax) {
      var spans = scanlineSpans(rotated, y);
      for (var s = 0; s < spans.length; s++) {
        var start = spans[s][0] - margin, end = spans[s][1] + margin;
        if (end - start < 1) continue;
        var a = [start, y], b = [end, y];
        if (flip) { var tmp = a; a = b; b = tmp; }
        lines.push([rotate(a, angle), rotate(b, angle)]);
      }
      flip = !flip;
      y += spacing;
    }
    return lines;
  }

  function pointsAlong(start, end, spacing) {
    var dx = end[0] - start[0], dy = end[1] - start[1];
    var length = Math.hypot(dx, dy);
    if (length === 0 || spacing <= 0) return [start];
    var steps = Math.ceil(length / spacing);
    var bearing = Math.atan2(dy, dx);
    var sx = Math.cos(bearing) * spacing, sy = Math.sin(bearing) * spacing;
    var pts = [];
    for (var i = 0; i <= steps; i++) pts.push([start[0] + sx * i, start[1] + sy * i]);
    return pts;
  }

  function pathLength(lines) {
    var total = 0, prev = null;
    for (var i = 0; i < lines.length; i++) {
      var s = lines[i][0], e = lines[i][1];
      if (prev) total += Math.hypot(s[0] - prev[0], s[1] - prev[1]);
      total += Math.hypot(e[0] - s[0], e[1] - s[1]);
      prev = e;
    }
    return total;
  }

  function optimalHeading(local, lineSpacing, margin) {
    var best = 0, bestLen = -1;
    for (var deg = 0; deg < 180; deg++) {
      var lines = flightLines(local, deg, lineSpacing, margin);
      var total = 0;
      for (var i = 0; i < lines.length; i++) {
        total += Math.hypot(lines[i][1][0] - lines[i][0][0], lines[i][1][1] - lines[i][0][1]);
      }
      if (total > bestLen) { bestLen = total; best = deg; }
    }
    return best;
  }

  // -- Fence ----------------------------------------------------------------

  function convexHull(points) {
    var sorted = points.slice().sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    if (sorted.length <= 1) return sorted;
    function cross(o, a, b) { return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]); }
    var lower = [];
    for (var i = 0; i < sorted.length; i++) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], sorted[i]) <= 0) lower.pop();
      lower.push(sorted[i]);
    }
    var upper = [];
    for (var i = sorted.length - 1; i >= 0; i--) {
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], sorted[i]) <= 0) upper.pop();
      upper.push(sorted[i]);
    }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  function fenceContains(vertices, point, frame) {
    var polygon = vertices.map(function (v) { return frame.toLocal(v); });
    var xy = frame.toLocal(point);
    var x = xy[0], y = xy[1], inside = false, n = polygon.length;
    for (var i = 0; i < n; i++) {
      var x1 = polygon[i][0], y1 = polygon[i][1];
      var x2 = polygon[(i + 1) % n][0], y2 = polygon[(i + 1) % n][1];
      if ((y1 > y) !== (y2 > y)) {
        if (x < x1 + (y - y1) / (y2 - y1) * (x2 - x1)) inside = !inside;
      }
    }
    return inside;
  }

  // -- Main plan function ---------------------------------------------------

  function planSurvey(req) {
    var camera = PI_CAMERA_MODULE_3;
    var polygon = req.polygon;
    if (polygon.length < 3) return null;

    var frame = localFrame(polygon);
    var local = polygon.map(function (p) { return frame.toLocal(p); });

    var lineSpacing = cam_line_spacing(camera, req.altitude_m, req.side_overlap);
    var photoSpacing = cam_photo_spacing(camera, req.altitude_m, req.front_overlap);

    var turnAccel = 2.5, wpRadius = 2.0, turnPenalty = 6.0;
    var overshoot = Math.pow(req.ground_speed_ms, 2) / (2 * turnAccel) + wpRadius;
    var margin = req.keep_inside ? -overshoot : 15;

    var heading = req.auto_heading
      ? optimalHeading(local, lineSpacing, margin)
      : req.heading_deg;

    var headings = [heading];
    if (req.pattern === "crosshatch") headings.push(heading + 90);

    var allLines = [];
    for (var h = 0; h < headings.length; h++) {
      allLines = allLines.concat(flightLines(local, headings[h], lineSpacing, margin));
    }

    var waypoints = [];
    var photoPoints = [];
    for (var i = 0; i < allLines.length; i++) {
      var s = allLines[i][0], e = allLines[i][1];
      waypoints.push(frame.toLatLon(s));
      waypoints.push(frame.toLatLon(e));
      var pts = pointsAlong(s, e, photoSpacing);
      for (var j = 0; j < pts.length; j++) photoPoints.push(frame.toLatLon(pts[j]));
    }

    var pLen = pathLength(allLines);
    var duration = pLen / req.ground_speed_ms + allLines.length * turnPenalty;

    var area = polygonArea(local);

    // Warnings
    var warnings = [];
    var maxSpeed = cam_max_speed(camera, req.altitude_m, req.front_overlap);
    if (req.ground_speed_ms > maxSpeed) {
      warnings.push("Ground speed " + req.ground_speed_ms.toFixed(1) + " m/s outruns the shutter (max " +
        maxSpeed.toFixed(1) + " m/s at this altitude and overlap). Front overlap will fall below the requested " +
        Math.round(req.front_overlap * 100) + "%.");
    }
    if (req.pattern === "nadir") {
      warnings.push("Nadir-only grid: good orthomosaic, weak 3D reconstruction of vertical surfaces. Use crosshatch if a 3D model is the goal.");
    }
    if (req.front_overlap < 0.60 || req.side_overlap < 0.60) {
      warnings.push("Overlap below 60% frequently causes reconstruction holes over low-texture ground such as grass, water, or fresh asphalt.");
    }
    if (req.keep_inside) {
      warnings.push("Staying inside the boundary: flight lines stop " + overshoot.toFixed(0) +
        " m short of the edge so the turn overshoot lands inside it. Coverage thins at the very perimeter.");
      if (overshoot > 40) {
        warnings.push("At " + req.ground_speed_ms.toFixed(0) + " m/s the aircraft needs " +
          overshoot.toFixed(0) + " m to turn around, so a wide strip of the boundary goes unflown. Slow down to reclaim it.");
      }
    }

    // Power / endurance
    var airframe = Object.assign({}, DEADCAT_7IN, { all_up_weight_kg: req.all_up_weight_kg });
    var battery = CNHL_5000_6S;

    if (req.measured_hover_min) {
      airframe = af_calibrate(airframe, req.measured_hover_min, battery);
    }

    var endurance = af_endurance(airframe, battery, req.ground_speed_ms);
    var batteries = (duration / 60) / endurance;
    var shutter_limit = cam_max_speed(camera, req.altitude_m, req.front_overlap);
    var bestSpeed = Math.min(af_best_range_speed(airframe), shutter_limit);

    if (batteries > 1) {
      warnings.push("Mission needs about " + batteries.toFixed(1) + " batteries: " +
        req.ground_speed_ms.toFixed(0) + " m/s gives " + endurance.toFixed(0) + " min of flight and the survey takes longer. " +
        "Shrink the area, raise the altitude, or plan a pack change.");
    } else if (batteries > 0.8) {
      warnings.push("Little margin: the survey uses about " + Math.round(batteries * 100) +
        "% of a battery. Wind or a cold pack could leave you short.");
    }

    var gain = af_range_km(airframe, battery, bestSpeed) / Math.max(af_range_km(airframe, battery, req.ground_speed_ms), 1e-6);
    if (gain > 1.10) {
      warnings.push("Flying at " + bestSpeed.toFixed(0) + " m/s instead of " +
        req.ground_speed_ms.toFixed(0) + " m/s would cover about " + Math.round((gain - 1) * 100) +
        "% more ground per battery.");
    }

    // Wind
    var windEndurance = null, windBatteries = null;
    if (req.wind_speed_ms > 0) {
      var gs = req.ground_speed_ms, w = req.wind_speed_ms;
      var avgPower = (af_power(airframe, gs + w) + af_power(airframe, Math.max(0.1, gs - w))) / 2;
      windEndurance = batt_usable(battery) / avgPower * 60;
      windBatteries = (duration / 60) / windEndurance;
    }

    // Fence preview
    var fencePreview = null;
    try {
      var fenceVerts, fenceMargin;
      if (req.keep_inside) {
        fenceVerts = polygon.slice();
        fenceMargin = 0;
      } else {
        var wpFrame = localFrame(waypoints);
        var wpLocal = waypoints.map(function (w) { return wpFrame.toLocal(w); });
        var hull = convexHull(wpLocal);
        var cx = 0, cy = 0;
        for (var i = 0; i < hull.length; i++) { cx += hull[i][0]; cy += hull[i][1]; }
        cx /= hull.length; cy /= hull.length;
        var expanded = hull.map(function (p) {
          var dx = p[0] - cx, dy = p[1] - cy;
          var dist = Math.hypot(dx, dy);
          if (dist === 0) return p;
          var scale = (dist + 30) / dist;
          return [cx + dx * scale, cy + dy * scale];
        });
        fenceVerts = expanded.map(function (p) { return wpFrame.toLatLon(p); });
        fenceMargin = 30;
      }

      var ceiling = req.altitude_m + 30;
      var fenceFrame = localFrame(fenceVerts);
      var outside = 0;
      for (var i = 0; i < waypoints.length; i++) {
        if (!fenceContains(fenceVerts, waypoints[i], fenceFrame)) outside++;
      }
      fencePreview = {
        vertices: fenceVerts,
        margin_m: fenceMargin,
        max_altitude_m: ceiling,
        encloses_flight: outside === 0,
        waypoints_outside: outside,
      };
    } catch (e) { /* not fatal */ }

    var fp = cam_footprint(camera, req.altitude_m);

    return {
      waypoints: waypoints,
      photo_points: photoPoints,
      fence: fencePreview,
      stats: {
        area_acres: Math.round(area / 4046.856 * 100) / 100,
        gsd_cm_per_px: Math.round(cam_gsd_cm(camera, req.altitude_m) * 100) / 100,
        line_count: allLines.length,
        photo_count: photoPoints.length,
        path_length_m: Math.round(pLen),
        duration_min: Math.round(duration / 60 * 10) / 10,
        photo_spacing_m: Math.round(photoSpacing * 10) / 10,
        line_spacing_m: Math.round(lineSpacing * 10) / 10,
        max_ground_speed_ms: Math.round(maxSpeed * 10) / 10,
        endurance_min: Math.round(endurance * 10) / 10,
        batteries_needed: Math.round(batteries * 100) / 100,
        best_survey_speed_ms: Math.round(bestSpeed * 10) / 10,
        calibrated: !!req.measured_hover_min,
        resolved_heading_deg: Math.round(heading * 10) / 10,
        wind_speed_ms: Math.round(req.wind_speed_ms * 10) / 10,
        wind_endurance_min: windEndurance != null ? Math.round(windEndurance * 10) / 10 : null,
        wind_batteries_needed: windBatteries != null ? Math.round(windBatteries * 100) / 100 : null,
        footprint_across_m: Math.round(fp.across * 100) / 100,
        footprint_along_m: Math.round(fp.along * 100) / 100,
      },
      warnings: warnings,
    };
  }

  return { planSurvey: planSurvey };
})();
