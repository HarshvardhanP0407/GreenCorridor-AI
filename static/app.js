/**
 * GreenCorridor AI — Frontend Application Logic
 * Vanilla JavaScript (No build step, no Node required)
 */

(function () {
  "use strict";

  // ── Global State ───────────────────────────────────────────
  const state = {
    tripId: null,
    status: "IDLE", // IDLE, RUNNING, PAUSED, FINISHED
    speedMultiplier: 1,
    simTimeS: 0,
    currentLat: null,
    currentLng: null,
    currentDistanceM: 0,
    totalDistanceM: 0,
    junctions: [],
    alerts: [],
    routeGeometry: [],
    activeView: "dashboard",
    autoOfficer: true,
    pendingAckAlertId: null,
  };

  // ── Map References ─────────────────────────────────────────
  let mapInstance = null;
  let ambulanceMarker = null;
  let routePolyline = null;
  let junctionMarkers = {}; // { junctionId: L.Marker }
  let mapTilesFailed = false;

  // ── DOM Element Cache ──────────────────────────────────────
  const el = {};

  function cacheDom() {
    el.fallbackBadge = document.getElementById("fallback-badge");
    el.mapTileWarning = document.getElementById("map-tile-warning");
    el.navItems = document.querySelectorAll(".nav-item");
    el.viewPanels = document.querySelectorAll(".view-panel");

    // Status & Sim Time
    el.statusDot = document.getElementById("system-status-dot");
    el.statusText = document.getElementById("system-status-text");
    el.tripIdDisplay = document.getElementById("display-trip-id");
    el.speedText = document.getElementById("display-speed-text");
    el.simTimeDisplay = document.getElementById("display-sim-time");

    // Control Buttons
    el.btnStart = document.getElementById("btn-start");
    el.btnPause = document.getElementById("btn-pause");
    el.btnResume = document.getElementById("btn-resume");
    el.btnReset = document.getElementById("btn-reset");
    el.speedBtns = document.querySelectorAll(".btn-speed");
    el.chkAutoOfficer = document.getElementById("chk-auto-officer");

    // KPIs
    el.kpiDistVal = document.getElementById("kpi-distance-val");
    el.kpiTotalDistVal = document.getElementById("kpi-total-distance-val");
    el.kpiProgressBar = document.getElementById("kpi-progress-bar");
    el.kpiNextEta = document.getElementById("kpi-next-eta");
    el.kpiNextJunctionName = document.getElementById("kpi-next-junction-name");
    el.kpiActiveAlerts = document.getElementById("kpi-active-alerts");
    el.kpiEscalatedAlerts = document.getElementById("kpi-escalated-alerts");
    el.kpiTimeSaved = document.getElementById("kpi-time-saved");

    // Badges & Lists
    el.badgeJunctionsCount = document.getElementById("badge-junctions-count");
    el.badgeAlertsCount = document.getElementById("badge-alerts-count");
    el.quickAlertCount = document.getElementById("quick-alert-count");
    el.quickAlertsList = document.getElementById("quick-alerts-list");
    el.fullAlertsList = document.getElementById("full-alerts-list");

    // Tables
    el.junctionsTbody = document.getElementById("junctions-table-body");
    el.eventsTbody = document.getElementById("events-table-body");
    el.eventFilterSelect = document.getElementById("event-filter-select");
    el.btnExportCsv = document.getElementById("btn-export-csv");

    // Analytics
    el.analyticsBaseDur = document.getElementById("analytics-base-dur");
    el.analyticsCoordDur = document.getElementById("analytics-coord-dur");
    el.analyticsSavedDur = document.getElementById("analytics-saved-dur");
    el.analyticsBaseWait = document.getElementById("analytics-base-wait");
    el.analyticsCoordWait = document.getElementById("analytics-coord-wait");
    el.analyticsTbody = document.getElementById("analytics-table-body");
    el.analyticsAssumptionsList = document.getElementById("analytics-assumptions-list");

    // Settings
    el.settingsForm = document.getElementById("settings-form");
    el.btnSaveSettings = document.getElementById("btn-save-settings");
    el.settingsFeedback = document.getElementById("settings-feedback");

    // Modal
    el.ackModal = document.getElementById("ack-modal");
    el.modalAlertDetails = document.getElementById("modal-alert-details");
    el.officerNameInput = document.getElementById("officer-name-input");
    el.officerNameError = document.getElementById("officer-name-error");
    el.btnConfirmAck = document.getElementById("btn-confirm-ack");
    el.btnCancelAck = document.getElementById("btn-cancel-ack");
    el.modalClose = document.getElementById("modal-close");
  }

  // ── Initialization ─────────────────────────────────────────
  function init() {
    cacheDom();
    setupNavigation();
    setupControls();
    setupSettings();
    setupEventsView();
    setupModal();
    initMap();
    initSSE();
    loadInitialSettings();
  }

  // ── Navigation ─────────────────────────────────────────────
  function setupNavigation() {
    el.navItems.forEach((btn) => {
      btn.addEventListener("click", () => {
        const viewId = btn.getAttribute("data-view");
        switchView(viewId);
      });
    });
  }

  function switchView(viewId) {
    state.activeView = viewId;
    el.navItems.forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-view") === viewId);
    });
    el.viewPanels.forEach((p) => {
      p.classList.toggle("active", p.id === `view-${viewId}`);
    });

    if (viewId === "dashboard" && mapInstance) {
      setTimeout(() => mapInstance.invalidateSize(), 100);
    } else if (viewId === "events") {
      fetchEvents();
    } else if (viewId === "analytics") {
      fetchAnalytics();
    } else if (viewId === "junctions") {
      fetchJunctions();
    }
  }

  // ── Leaflet Map Setup ──────────────────────────────────────
  function initMap() {
    const mapContainer = document.getElementById("map");
    if (!mapContainer) return;

    if (typeof L === "undefined") {
      if (el.mapTileWarning) {
        el.mapTileWarning.classList.remove("hidden");
        el.mapTileWarning.textContent = "⚠️ Leaflet library unavailable (offline). Telemetry is tracked in tables.";
      }
      return;
    }

    try {
      // Default view centered on Anna Salai, Chennai
      mapInstance = L.map("map", {
        zoomControl: true,
      }).setView([13.045, 80.245], 13);

      const tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors",
      });

      tileLayer.on("tileerror", () => {
        if (!mapTilesFailed) {
          mapTilesFailed = true;
          if (el.mapTileWarning) {
            el.mapTileWarning.classList.remove("hidden");
            el.mapTileWarning.textContent = "⚠️ Map tiles unavailable (offline / blocked). Telemetry and route lines remain active.";
          }
        }
      });

      tileLayer.addTo(mapInstance);
    } catch (e) {
      console.warn("Map initialization notice:", e);
      if (el.mapTileWarning) {
        el.mapTileWarning.classList.remove("hidden");
      }
    }
  }

  // ── Controls Setup ─────────────────────────────────────────
  function setupControls() {
    el.btnStart.addEventListener("click", handleStart);
    el.btnPause.addEventListener("click", handlePause);
    el.btnResume.addEventListener("click", handleResume);
    el.btnReset.addEventListener("click", handleReset);

    el.speedBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const speed = parseInt(btn.getAttribute("data-speed"), 10);
        handleSpeedChange(speed);
      });
    });

    el.chkAutoOfficer.addEventListener("change", (e) => {
      const enabled = e.target.checked;
      state.autoOfficer = enabled;
      fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_officer_enabled: enabled ? "true" : "false" }),
      }).catch((err) => console.error("Error setting auto-officer:", err));
    });
  }

  async function handleStart() {
    try {
      el.btnStart.disabled = true;
      const res = await fetch("/api/trip/start", { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Failed to start simulation.");
        el.btnStart.disabled = false;
        return;
      }

      state.tripId = data.trip_id;
      state.status = "RUNNING";
      state.totalDistanceM = data.total_distance_m;
      updateStatusUI();

      if (data.fallback) {
        el.fallbackBadge.classList.remove("hidden");
        el.fallbackBadge.textContent = "⚠️ Fallback route in use: Anna Salai, Chennai";
      } else {
        el.fallbackBadge.classList.add("hidden");
      }

      // Load route geometry to draw polyline
      fetchRouteGeometry();
      fetchJunctions();
    } catch (err) {
      console.error(err);
      alert("Error starting simulation: " + err.message);
      el.btnStart.disabled = false;
    }
  }

  async function handlePause() {
    try {
      const res = await fetch("/api/trip/pause", { method: "POST" });
      if (res.ok) {
        state.status = "PAUSED";
        updateStatusUI();
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function handleResume() {
    try {
      const res = await fetch("/api/trip/resume", { method: "POST" });
      if (res.ok) {
        state.status = "RUNNING";
        updateStatusUI();
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function handleReset() {
    try {
      const res = await fetch("/api/trip/reset", { method: "POST" });
      if (res.ok) {
        state.status = "IDLE";
        state.tripId = null;
        state.simTimeS = 0;
        state.currentDistanceM = 0;
        state.alerts = [];
        state.junctions = [];
        updateStatusUI();
        clearMap();
        renderAlerts();
        renderJunctions();
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function handleSpeedChange(speed) {
    try {
      const res = await fetch("/api/trip/speed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ speed_multiplier: speed }),
      });
      if (res.ok) {
        state.speedMultiplier = speed;
        el.speedBtns.forEach((b) => {
          b.classList.toggle("active", parseInt(b.getAttribute("data-speed"), 10) === speed);
        });
        el.speedText.textContent = `${speed}x`;
      }
    } catch (err) {
      console.error(err);
    }
  }

  function updateStatusUI() {
    el.btnStart.disabled = state.status === "RUNNING" || state.status === "PAUSED";
    el.btnPause.disabled = state.status !== "RUNNING";
    el.btnResume.disabled = state.status !== "PAUSED";
    el.btnReset.disabled = state.status === "IDLE";

    el.statusDot.className = "dot";
    if (state.status === "RUNNING") {
      el.statusDot.classList.add("dot-running");
      el.statusText.textContent = "Running";
    } else if (state.status === "PAUSED") {
      el.statusDot.classList.add("dot-paused");
      el.statusText.textContent = "Paused";
    } else if (state.status === "FINISHED") {
      el.statusDot.classList.add("dot-finished");
      el.statusText.textContent = "Completed";
    } else {
      el.statusDot.classList.add("dot-idle");
      el.statusText.textContent = "Idle";
    }

    el.tripIdDisplay.textContent = state.tripId ? `#${state.tripId}` : "—";
  }

  // ── Route & Map Drawing ────────────────────────────────────
  async function fetchRouteGeometry() {
    try {
      const res = await fetch("/api/route");
      if (!res.ok) return;
      const data = await res.json();
      state.routeGeometry = data.geometry || [];

      if (!mapInstance || !state.routeGeometry.length) return;

      if (routePolyline) {
        mapInstance.removeLayer(routePolyline);
      }

      routePolyline = L.polyline(state.routeGeometry, {
        color: "#00e5ff",
        weight: 5,
        opacity: 0.85,
      }).addTo(mapInstance);

      mapInstance.fitBounds(routePolyline.getBounds(), { padding: [30, 30] });
    } catch (err) {
      console.error("Error loading route:", err);
    }
  }

  function updateAmbulanceMarker(lat, lng) {
    if (!mapInstance || lat === null || lng === null) return;

    if (!ambulanceMarker) {
      const icon = L.divIcon({
        className: "ambulance-div-icon",
        html: `<div class="ambulance-marker-pin">🚑</div>`,
        iconSize: [26, 26],
        iconAnchor: [13, 13],
      });
      ambulanceMarker = L.marker([lat, lng], { icon }).addTo(mapInstance);
      ambulanceMarker.bindPopup("<b>Emergency Ambulance</b><br>Responding with corridor priority");
    } else {
      ambulanceMarker.setLatLng([lat, lng]);
    }
  }

  function clearMap() {
    if (!mapInstance) return;
    if (ambulanceMarker) {
      mapInstance.removeLayer(ambulanceMarker);
      ambulanceMarker = null;
    }
    if (routePolyline) {
      mapInstance.removeLayer(routePolyline);
      routePolyline = null;
    }
    Object.values(junctionMarkers).forEach((m) => mapInstance.removeLayer(m));
    junctionMarkers = {};
  }

  function updateJunctionMapMarkers() {
    if (!mapInstance) return;

    state.junctions.forEach((j) => {
      const color = getJunctionColor(j);
      if (!junctionMarkers[j.id]) {
        const icon = L.divIcon({
          className: "junction-div-icon",
          html: `<div class="junction-marker-pin" style="background-color: ${color};"></div>`,
          iconSize: [16, 16],
          iconAnchor: [8, 8],
        });
        const marker = L.marker([j.lat, j.lng], { icon }).addTo(mapInstance);
        marker.bindPopup(buildJunctionPopup(j));
        junctionMarkers[j.id] = marker;
      } else {
        const marker = junctionMarkers[j.id];
        const iconHtml = `<div class="junction-marker-pin" style="background-color: ${color};"></div>`;
        marker.setIcon(
          L.divIcon({
            className: "junction-div-icon",
            html: iconHtml,
            iconSize: [16, 16],
            iconAnchor: [8, 8],
          })
        );
        marker.setPopupContent(buildJunctionPopup(j));
      }
    });
  }

  function getJunctionColor(j) {
    if (j.is_blocked) return "#dc2626"; // bright red
    if (j.is_passed) return "#a855f7"; // purple

    const alert = state.alerts.find((a) => a.junction_id === j.id);
    if (!alert) return "#64748b"; // gray upcoming
    if (alert.status === "ESCALATED") return "#ef4444"; // red
    if (alert.status === "ACKNOWLEDGED") return "#10b981"; // green
    if (alert.status === "CREATED") return "#f59e0b"; // amber
    return "#64748b";
  }

  function buildJunctionPopup(j) {
    return `
      <div style="font-size:12px; font-family:sans-serif;">
        <strong>${j.name}</strong><br>
        Remaining: ${(j.remaining_distance_m / 1000).toFixed(2)} km<br>
        Rule-based ETA: <b>${j.eta_s ? j.eta_s + "s" : "Passed"}</b><br>
        Risk: <span style="text-transform:uppercase;">${j.risk_level || "low"}</span><br>
        Status: ${j.is_blocked ? "<b style='color:#ef4444'>BLOCKED</b>" : (j.is_passed ? "Cleared" : "Upcoming")}
      </div>
    `;
  }

  // ── Server-Sent Events (SSE) ───────────────────────────────
  function initSSE() {
    if (!window.EventSource) {
      console.warn("SSE not supported by browser; falling back to polling.");
      setInterval(pollTelemetry, 1500);
      return;
    }

    const sse = new EventSource("/api/sse");

    sse.onmessage = function (event) {
      try {
        const msg = JSON.parse(event.data);
        handleSSEMessage(msg);
      } catch (err) {
        // Keepalive comments or ping
      }
    };

    sse.onerror = function () {
      // EventSource reconnects automatically
    };
  }

  function handleSSEMessage(msg) {
    if (!msg || !msg.type) return;

    if (msg.type === "tick") {
      handleTick(msg.data);
    } else if (msg.type === "alerts") {
      state.alerts = msg.data || [];
      renderAlerts();
      updateJunctionMapMarkers();
    } else if (msg.type === "junctions") {
      state.junctions = msg.data || [];
      renderJunctions();
      updateJunctionMapMarkers();
    } else if (msg.type === "roadblock") {
      fetchJunctions();
    } else if (msg.type === "analytics") {
      renderAnalyticsData(msg.data);
    } else if (msg.type === "finished") {
      state.status = "FINISHED";
      updateStatusUI();
      fetchAnalytics();
    }
  }

  function handleTick(data) {
    state.currentLat = data.current_lat;
    state.currentLng = data.current_lng;
    state.currentDistanceM = data.current_distance_m;
    state.totalDistanceM = data.total_distance_m;
    state.simTimeS = data.elapsed_sim_time_s;
    state.status = data.status;

    // Update status dot and timer
    updateStatusUI();
    const mins = Math.floor(state.simTimeS / 60);
    const secs = Math.floor(state.simTimeS % 60);
    el.simTimeDisplay.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;

    // KPI Distance & Progress bar
    const distKm = (state.currentDistanceM / 1000).toFixed(2);
    const totalKm = (state.totalDistanceM / 1000).toFixed(2);
    el.kpiDistVal.textContent = distKm;
    el.kpiTotalDistVal.textContent = totalKm;
    const pct = Math.min(100, (state.currentDistanceM / Math.max(state.totalDistanceM, 1)) * 100);
    el.kpiProgressBar.style.width = `${pct}%`;

    // Update Ambulance Marker on Map
    updateAmbulanceMarker(state.currentLat, state.currentLng);

    // Refresh junctions periodically
    fetchJunctions();
  }

  async function pollTelemetry() {
    if (state.status !== "RUNNING" && state.status !== "PAUSED") return;
    try {
      const res = await fetch("/api/trip/state");
      if (res.ok) {
        const data = await res.json();
        handleTick(data);
      }
      fetchAlerts();
    } catch (e) {}
  }

  // ── Junctions Fetch & Render ───────────────────────────────
  async function fetchJunctions() {
    try {
      const res = await fetch("/api/junctions");
      if (!res.ok) return;
      state.junctions = await res.json();
      renderJunctions();
      updateJunctionMapMarkers();
      updateNextJunctionKPI();
    } catch (err) {
      console.error(err);
    }
  }

  function renderJunctions() {
    el.badgeJunctionsCount.textContent = state.junctions.length;

    if (!state.junctions.length) {
      el.junctionsTbody.innerHTML = `
        <tr>
          <td colspan="7" class="text-center empty-cell">No active trip. Press "Start Simulation" to load junctions.</td>
        </tr>`;
      return;
    }

    let rowsHtml = "";
    state.junctions.forEach((j, idx) => {
      const alert = state.alerts.find((a) => a.junction_id === j.id);
      let alertBadge = '<span class="badge-status">Upcoming</span>';
      if (alert) {
        alertBadge = `<span class="badge-status status-${alert.status}">${alert.status}</span>`;
      }
      if (j.is_passed) {
        alertBadge = '<span class="badge-status status-CLEARED">Cleared</span>';
      }

      const riskClass = `risk-${j.risk_level || "low"}`;
      const blockBtn = j.is_blocked
        ? `<button class="btn btn-warning btn-sm" onclick="GreenCorridorApp.clearRoadblock(${j.id})">Clear Block</button>`
        : `<button class="btn btn-danger btn-sm" ${j.is_passed ? "disabled" : ""} onclick="GreenCorridorApp.setRoadblock(${j.id})">Block Road</button>`;

      rowsHtml += `
        <tr>
          <td>${idx + 1}</td>
          <td>
            <strong>${j.name}</strong>
            ${j.is_blocked ? ' <span class="badge-blocked">ROADBLOCK</span>' : ""}
          </td>
          <td>${(j.remaining_distance_m / 1000).toFixed(2)} km</td>
          <td><strong>${j.is_passed ? "Passed" : j.eta_s + " s"}</strong></td>
          <td><span class="risk-badge ${riskClass}">${j.risk_level || "low"}</span></td>
          <td>${alertBadge}</td>
          <td>${blockBtn}</td>
        </tr>
      `;
    });

    el.junctionsTbody.innerHTML = rowsHtml;
  }

  function updateNextJunctionKPI() {
    const next = state.junctions.find((j) => !j.is_passed);
    if (next) {
      el.kpiNextEta.textContent = `${next.eta_s}s`;
      el.kpiNextJunctionName.textContent = `Approaching ${next.name}`;
    } else if (state.status === "FINISHED") {
      el.kpiNextEta.textContent = "Arrived";
      el.kpiNextJunctionName.textContent = "Destination reached";
    } else {
      el.kpiNextEta.textContent = "—";
      el.kpiNextJunctionName.textContent = "Waiting for trip";
    }
  }

  // ── Roadblock API Calls ────────────────────────────────────
  window.GreenCorridorApp = {
    setRoadblock: async function (junctionId) {
      try {
        const res = await fetch("/api/roadblock/set", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ junction_id: junctionId }),
        });
        const data = await res.json();
        if (!res.ok) {
          alert(data.error || "Failed to set roadblock.");
        } else {
          fetchJunctions();
        }
      } catch (err) {
        console.error(err);
      }
    },
    clearRoadblock: async function (junctionId) {
      try {
        const res = await fetch("/api/roadblock/clear", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ junction_id: junctionId }),
        });
        const data = await res.json();
        if (!res.ok) {
          alert(data.error || "Failed to clear roadblock.");
        } else {
          fetchJunctions();
        }
      } catch (err) {
        console.error(err);
      }
    },
    openAckModal: function (alertId, junctionName) {
      state.pendingAckAlertId = alertId;
      el.modalAlertDetails.innerHTML = `Acknowledge corridor clearance signal at <strong>${junctionName}</strong>.`;
      el.officerNameInput.value = "";
      el.officerNameError.classList.add("hidden");
      el.ackModal.classList.remove("hidden");
      el.officerNameInput.focus();
    },
  };

  // ── Alerts Fetch & Render ──────────────────────────────────
  async function fetchAlerts() {
    if (!state.tripId) return;
    try {
      const res = await fetch("/api/alerts");
      if (!res.ok) return;
      state.alerts = await res.json();
      renderAlerts();
      updateJunctionMapMarkers();
    } catch (err) {
      console.error(err);
    }
  }

  function renderAlerts() {
    const activeAlerts = state.alerts.filter((a) => a.status === "CREATED" || a.status === "ESCALATED");
    const escalatedAlerts = state.alerts.filter((a) => a.status === "ESCALATED");

    el.kpiActiveAlerts.textContent = activeAlerts.length;
    el.kpiEscalatedAlerts.textContent = escalatedAlerts.length;
    el.badgeAlertsCount.textContent = activeAlerts.length;
    el.quickAlertCount.textContent = `${activeAlerts.length} active`;

    // Render Quick Alert Queue (Dashboard)
    if (!activeAlerts.length) {
      el.quickAlertsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🚨</div>
          <div class="empty-text">No active alerts. When ambulance is 60–90s from a junction, an alert will trigger here.</div>
        </div>`;
    } else {
      let qHtml = "";
      activeAlerts.forEach((a) => {
        qHtml += buildAlertCardHtml(a, true);
      });
      el.quickAlertsList.innerHTML = qHtml;
    }

    // Render Full Alerts List (Alerts View)
    if (!state.alerts.length) {
      el.fullAlertsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🛡️</div>
          <div class="empty-text">No alerts for current trip. Alerts will appear as ambulance progresses.</div>
        </div>`;
    } else {
      let fHtml = "";
      state.alerts.forEach((a) => {
        fHtml += buildAlertCardHtml(a, false);
      });
      el.fullAlertsList.innerHTML = fHtml;
    }
  }

  function buildAlertCardHtml(alert, isQuickView) {
    const timeout = 20; // default 20s
    const elapsedSinceCreated = Math.max(0, state.simTimeS - (alert.created_sim_time_s || 0));
    const countdown = Math.max(0, timeout - elapsedSinceCreated).toFixed(0);

    let actionSection = "";
    if (alert.status === "CREATED") {
      actionSection = `
        <div class="alert-card-footer">
          <span style="font-size:11px; color:var(--amber-warn);">Escalates in <b>${countdown}s</b></span>
          <button class="btn btn-primary btn-sm" onclick="GreenCorridorApp.openAckModal(${alert.id}, '${alert.junction_name}')">
            Acknowledge
          </button>
        </div>
      `;
    } else if (alert.status === "ESCALATED") {
      actionSection = `
        <div class="alert-card-footer">
          <span style="font-size:11px; color:var(--red-critical); font-weight:700;">⚠️ Escalated to Supervisor</span>
          <button class="btn btn-warning btn-sm" onclick="GreenCorridorApp.openAckModal(${alert.id}, '${alert.junction_name}')">
            Supervisor Ack
          </button>
        </div>
      `;
    } else if (alert.status === "ACKNOWLEDGED") {
      actionSection = `
        <div class="alert-card-footer" style="font-size:11px; color:var(--green-success);">
          <span>✓ Cleared by: ${alert.officer_name || "Officer"}</span>
        </div>
      `;
    } else {
      actionSection = `
        <div class="alert-card-footer" style="font-size:11px; color:var(--purple-accent);">
          <span>Ambulance passed junction</span>
        </div>
      `;
    }

    return `
      <div class="alert-card status-${alert.status}">
        <div class="alert-card-header">
          <span class="alert-junction-title">${alert.junction_name}</span>
          <span class="badge-status">${alert.status}</span>
        </div>
        <div class="alert-card-meta">
          <span>Alert #${alert.id}</span>
          <span>Created: ${alert.created_sim_time_s ? alert.created_sim_time_s.toFixed(1) + "s (sim)" : "—"}</span>
        </div>
        ${actionSection}
      </div>
    `;
  }

  // ── Acknowledge Modal Setup ────────────────────────────────
  function setupModal() {
    el.modalClose.addEventListener("click", closeModal);
    el.btnCancelAck.addEventListener("click", closeModal);
    el.btnConfirmAck.addEventListener("click", confirmAcknowledge);

    el.officerNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") confirmAcknowledge();
    });
  }

  function closeModal() {
    el.ackModal.classList.add("hidden");
    state.pendingAckAlertId = null;
  }

  async function confirmAcknowledge() {
    const officer = el.officerNameInput.value.trim();
    if (!officer) {
      el.officerNameError.classList.remove("hidden");
      return;
    }
    el.officerNameError.classList.add("hidden");

    try {
      const res = await fetch(`/api/alerts/${state.pendingAckAlertId}/acknowledge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ officer_name: officer }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Acknowledge failed.");
      } else {
        closeModal();
        fetchAlerts();
      }
    } catch (err) {
      console.error(err);
      alert("Error submitting acknowledge: " + err.message);
    }
  }

  // ── Analytics Fetch & Render ───────────────────────────────
  async function fetchAnalytics() {
    try {
      const res = await fetch("/api/analytics");
      if (!res.ok) return;
      const data = await res.json();
      renderAnalyticsData(data);
    } catch (err) {
      console.error(err);
    }
  }

  function renderAnalyticsData(data) {
    if (!data || !data.baseline || !data.coordinated) return;

    el.analyticsBaseDur.textContent = `${data.baseline.duration_s} s`;
    el.analyticsCoordDur.textContent = `${data.coordinated.duration_s} s`;
    el.analyticsSavedDur.textContent = `${data.time_saved_s} s`;
    el.kpiTimeSaved.textContent = `${data.time_saved_s} s`;

    el.analyticsBaseWait.textContent = `${data.baseline.total_wait_s} s`;
    el.analyticsCoordWait.textContent = `${data.coordinated.total_wait_s} s`;

    // Side-by-side Table
    el.analyticsTbody.innerHTML = `
      <tr>
        <td><strong>Total Trip Duration</strong></td>
        <td>${data.baseline.duration_s} s (simulated)</td>
        <td><strong>${data.coordinated.duration_s} s (simulated)</strong></td>
        <td><span class="green-highlight">-${data.time_saved_s} s (${((data.time_saved_s / data.baseline.duration_s) * 100).toFixed(1)}% saved)</span></td>
      </tr>
      <tr>
        <td><strong>Total Signal Wait Time</strong></td>
        <td>${data.baseline.total_wait_s} s (100% full wait)</td>
        <td><strong>${data.coordinated.total_wait_s} s</strong></td>
        <td><span class="green-highlight">-${(data.baseline.total_wait_s - data.coordinated.total_wait_s).toFixed(1)} s wait reduced</span></td>
      </tr>
      <tr>
        <td><strong>Alerts Generated</strong></td>
        <td>0 (no corridor clearance)</td>
        <td>${data.coordinated.alerts_sent}</td>
        <td>${data.coordinated.alerts_sent} alerts triggered</td>
      </tr>
      <tr>
        <td><strong>Alerts Acknowledged</strong></td>
        <td>—</td>
        <td>${data.coordinated.alerts_acknowledged}</td>
        <td>${data.coordinated.alerts_acknowledged} timely cleared</td>
      </tr>
      <tr>
        <td><strong>Alerts Escalated</strong></td>
        <td>—</td>
        <td>${data.coordinated.alerts_escalated}</td>
        <td>${data.coordinated.alerts_escalated} escalated to supervisor</td>
      </tr>
      <tr>
        <td><strong>Average Police Response Time</strong></td>
        <td>—</td>
        <td>${data.coordinated.avg_response_time_s} s</td>
        <td>Simulated officer response</td>
      </tr>
    `;

    // Assumptions List
    if (data.assumptions && data.assumptions.length) {
      let aHtml = "";
      data.assumptions.forEach((item) => {
        aHtml += `<li>${item}</li>`;
      });
      el.analyticsAssumptionsList.innerHTML = aHtml;
    }
  }

  // ── Event History ──────────────────────────────────────────
  function setupEventsView() {
    el.eventFilterSelect.addEventListener("change", fetchEvents);
    el.btnExportCsv.addEventListener("click", () => {
      const tripParam = state.tripId ? `?trip_id=${state.tripId}` : "";
      window.location.href = `/api/events/csv${tripParam}`;
    });
  }

  async function fetchEvents() {
    try {
      const filter = el.eventFilterSelect.value;
      let url = "/api/events";
      const params = [];
      if (filter) params.push(`type=${filter}`);
      if (state.tripId) params.push(`trip_id=${state.tripId}`);
      if (params.length) url += `?${params.join("&")}`;

      const res = await fetch(url);
      if (!res.ok) return;
      const events = await res.json();

      if (!events.length) {
        el.eventsTbody.innerHTML = `
          <tr>
            <td colspan="5" class="text-center empty-cell">No events found matching criteria.</td>
          </tr>`;
        return;
      }

      let rowsHtml = "";
      events.forEach((ev) => {
        rowsHtml += `
          <tr>
            <td>#${ev.id}</td>
            <td><code>${ev.timestamp}</code></td>
            <td>${ev.sim_time_s !== null ? ev.sim_time_s.toFixed(1) + " s" : "—"}</td>
            <td><span class="badge-status">${ev.event_type}</span></td>
            <td>${ev.description}</td>
          </tr>
        `;
      });
      el.eventsTbody.innerHTML = rowsHtml;
    } catch (err) {
      console.error(err);
    }
  }

  // ── Settings View ──────────────────────────────────────────
  function setupSettings() {
    el.btnSaveSettings.addEventListener("click", saveSettings);
  }

  async function loadInitialSettings() {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) return;
      const s = await res.json();

      document.getElementById("set-seed").value = s.seed || 42;
      document.getElementById("set-base-speed").value = s.base_speed_kmh || 50;
      document.getElementById("set-signal-delay").value = s.signal_delay_s || 20;
      document.getElementById("set-alert-start").value = s.alert_start_s || 90;
      document.getElementById("set-alert-min").value = s.alert_min_s || 60;
      document.getElementById("set-escalation-timeout").value = s.escalation_timeout_s || 20;
      document.getElementById("set-roadblock-penalty").value = s.roadblock_penalty_s || 120;
      document.getElementById("set-clearance-factor").value = s.clearance_factor || 0.70;
      document.getElementById("set-esc-clearance-factor").value = s.escalation_clearance_factor || 0.30;

      const autoOff = String(s.auto_officer_enabled).toLowerCase();
      el.chkAutoOfficer.checked = autoOff === "true" || autoOff === "1";
      state.autoOfficer = el.chkAutoOfficer.checked;

      // Update formula display notes
      document.getElementById("disp-cong-low").textContent = s.congestion_low || "0.1";
      document.getElementById("disp-cong-med").textContent = s.congestion_medium || "0.3";
      document.getElementById("disp-cong-high").textContent = s.congestion_high || "0.5";
    } catch (err) {
      console.error("Failed to load settings:", err);
    }
  }

  async function saveSettings() {
    const payload = {
      seed: document.getElementById("set-seed").value,
      base_speed_kmh: document.getElementById("set-base-speed").value,
      signal_delay_s: document.getElementById("set-signal-delay").value,
      alert_start_s: document.getElementById("set-alert-start").value,
      alert_min_s: document.getElementById("set-alert-min").value,
      escalation_timeout_s: document.getElementById("set-escalation-timeout").value,
      roadblock_penalty_s: document.getElementById("set-roadblock-penalty").value,
      clearance_factor: document.getElementById("set-clearance-factor").value,
      escalation_clearance_factor: document.getElementById("set-esc-clearance-factor").value,
    };

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      el.settingsFeedback.classList.remove("hidden");

      if (!res.ok) {
        el.settingsFeedback.className = "feedback-msg feedback-error";
        const errors = Object.values(data.errors || {}).join(", ");
        el.settingsFeedback.textContent = "Error saving settings: " + (errors || "Invalid input");
      } else {
        el.settingsFeedback.className = "feedback-msg feedback-success";
        el.settingsFeedback.textContent = "✓ Settings saved successfully.";
        setTimeout(() => el.settingsFeedback.classList.add("hidden"), 3000);
      }
    } catch (err) {
      console.error(err);
      el.settingsFeedback.className = "feedback-msg feedback-error";
      el.settingsFeedback.textContent = "Error: " + err.message;
    }
  }

  // Start app on DOM load
  document.addEventListener("DOMContentLoaded", init);
})();
