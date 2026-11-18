"use strict";

/*
 * SafeRoute map client.
 *
 * One page, three roles:
 *   - residents tap where they are and get a route to the nearest open, reachable shelter;
 *   - responders sign in and mark roads blocked, reopen them and set shelter status;
 *   - planners try hypothetical closures and see how evacuation times change, without publishing.
 *
 * The page polls /api/state every few seconds. When anything changes, the map restyles and any
 * route on screen is recomputed, so people are rerouted around new closures without reloading.
 * All text from the server is inserted with textContent, never as HTML.
 */
(() => {
  const POLL_MS = 3000;
  const MAJOR = new Set(["motorway", "trunk", "primary", "secondary"]);
  const ICONS = {
    house: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 3 2 11h3v9h5v-6h4v6h5v-9h3z"/></svg>',
    drop: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2s7 8 7 13a7 7 0 0 1-14 0c0-5 7-13 7-13z"/></svg>',
  };

  const state = {
    tab: "route",
    area: null,
    roads: new Map(),
    nodes: new Map(),
    shelters: new Map(),
    sensors: new Map(),
    live: { version: null, closures: new Map(), shelters: {}, sensors: {}, events: [] },
    session: { responder: null, csrf: "", reasons: [] },
    route: { mode: "foot", start: null, result: null, error: null },
    plan: { mode: "foot", roads: new Set(), shelters: new Set(), result: null, coverage: true },
  };

  // ---- small helpers -----------------------------------------------------------------------

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const fmtEta = (s) => (s == null ? "–" : `${Math.max(1, Math.round(s / 60))} min`);
  const fmtDist = (m) => (m == null ? "–" : m < 1000 ? `${Math.max(10, Math.round(m / 10) * 10)} m` : `${(m / 1000).toFixed(1)} km`);
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  const streetName = (name) => (name === "Unnamed road" ? "an unnamed lane" : name);
  const StreetName = (name) => (name === "Unnamed road" ? "An unnamed lane" : name);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let COLORS = {};

  async function api(path, { method, body, csrf, cache = "no-cache" } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (csrf) headers["X-CSRF-Token"] = state.session.csrf;
    const response = await fetch(path, {
      method: method || (body !== undefined ? "POST" : "GET"),
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      cache,
    });
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `Something went wrong (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  let toastTimer;
  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.hidden = true), 4000);
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ---- map ---------------------------------------------------------------------------------

  let map;
  let renderer;
  const layers = {};

  function initMap() {
    const [lat, lon] = state.area.area.center;
    map = L.map("map", { zoomControl: true, preferCanvas: true, minZoom: 13, zoomSnap: 0.5 }).setView([lat, lon], 15);
    // The basemap is optional: roads, closures and routes are drawn from local data, so the map
    // still works if tiles can't load (for example when mobile data is down).
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      className: "basemap",
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);

    renderer = L.canvas({ tolerance: 8 });
    for (const name of ["zones", "roads", "route", "marks", "start", "shelters", "sensors"]) {
      layers[name] = L.layerGroup().addTo(map);
    }

    const bounds = L.latLngBounds([]);
    for (const road of state.roads.values()) {
      road.line = L.polyline(road.geometry, { renderer, bubblingMouseEvents: false, ...roadStyle(road) });
      road.line.on("click", (event) => onRoadClick(road, event.latlng));
      road.line.addTo(layers.roads);
      bounds.extend(road.line.getBounds());
    }
    map.setMaxBounds(bounds.pad(0.6));
    // Fit the area once the map has a real size (it can start at zero width inside a hidden
    // pane or a collapsing layout), and keep Leaflet in step with later size changes.
    let fitted = false;
    new ResizeObserver(() => {
      map.invalidateSize();
      if (!fitted && map.getSize().x > 0) {
        fitted = true;
        map.fitBounds(bounds, { padding: [16, 16] });
      }
    }).observe($("#map"));
    map.on("click", (event) => {
      if (state.tab === "route") setStart(event.latlng);
    });

    for (const shelter of state.shelters.values()) {
      shelter.marker = L.marker([shelter.lat, shelter.lon], { icon: shelterIcon(shelter), keyboard: true, title: shelter.name, alt: `Shelter: ${shelter.name}` });
      shelter.marker.bindTooltip(el("span", { text: shelter.name }), { direction: "top", offset: [0, -14] });
      shelter.marker.on("click", (event) => onShelterClick(shelter, event.latlng));
      shelter.marker.addTo(layers.shelters);
    }
    for (const sensor of state.sensors.values()) {
      sensor.marker = L.marker([sensor.lat, sensor.lon], { icon: sensorIcon(sensor), title: `Flood sensor ${sensor.id}`, zIndexOffset: 500 });
      sensor.marker.on("click", (event) => onSensorClick(sensor, event.latlng));
      sensor.marker.addTo(layers.sensors);
    }
  }

  function middleOf(geometry) {
    return geometry[Math.floor(geometry.length / 2)];
  }

  function coverageColor(seconds) {
    if (seconds == null) return COLORS.s5;
    const minutes = seconds / 60;
    if (minutes <= 5) return COLORS.s1;
    if (minutes <= 10) return COLORS.s2;
    if (minutes <= 20) return COLORS.s3;
    return COLORS.s4;
  }

  function roadEta(road) {
    const eta = state.plan.result?.eta_by_node || {};
    const times = [eta[road.u], eta[road.v]].filter((t) => t != null);
    return times.length ? Math.min(...times) : null;
  }

  function roadStyle(road) {
    const weight = MAJOR.has(road.kind) ? 5 : 3;
    if (state.live.closures.has(road.id)) {
      return { color: COLORS.closed, weight: weight + 3, opacity: 1, dashArray: "10 7", lineCap: "butt" };
    }
    if (state.tab === "plan" && state.plan.roads.has(road.id)) {
      return { color: COLORS.scenario, weight: weight + 3, opacity: 1, dashArray: "1 9", lineCap: "round" };
    }
    if (state.tab === "plan" && state.plan.coverage && state.plan.result) {
      const eta = roadEta(road);
      return { color: coverageColor(eta), weight: weight + 1, opacity: eta == null ? 0.75 : 0.9, dashArray: eta == null ? "4 6" : null, lineCap: "round" };
    }
    return { color: "#64748b", weight, opacity: MAJOR.has(road.kind) ? 0.5 : 0.32, dashArray: null, lineCap: "round" };
  }

  function restyleRoads() {
    for (const road of state.roads.values()) road.line.setStyle(roadStyle(road));
    for (const id of state.live.closures.keys()) state.roads.get(id)?.line.bringToFront();
    drawMarks();
  }

  function drawMarks() {
    layers.marks.clearLayers();
    const add = (road, extraClass) => {
      const icon = L.divIcon({ className: "mk-wrap", html: `<div class="mk-x ${extraClass}">✕</div>`, iconSize: [20, 20], iconAnchor: [10, 10] });
      const mark = L.marker(middleOf(road.geometry), { icon, title: road.name, zIndexOffset: 200 });
      mark.on("click", (event) => onRoadClick(road, event.latlng));
      mark.addTo(layers.marks);
    };
    // Responder closures get an ✕. Sensor closures already sit inside a shaded flood zone.
    for (const closure of state.live.closures.values()) {
      const road = state.roads.get(closure.road);
      if (road && !closure.source.startsWith("sensor:")) add(road, "");
    }
    if (state.tab === "plan") {
      for (const id of state.plan.roads) {
        if (!state.live.closures.has(id)) add(state.roads.get(id), "is-scenario");
      }
    }
  }

  function shelterStatus(id) {
    return state.live.shelters[id] || "open";
  }

  function shelterIcon(shelter) {
    const classes = ["mk-shelter"];
    if (state.tab === "plan" && state.plan.shelters.has(shelter.id)) classes.push("is-scenario");
    else if (shelterStatus(shelter.id) !== "open") classes.push(`is-${shelterStatus(shelter.id)}`);
    if (state.tab === "route" && state.route.result?.route?.shelter.id === shelter.id) classes.push("is-target");
    return L.divIcon({ className: "mk-wrap", html: `<div class="${classes.join(" ")}">${ICONS.house}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
  }

  function refreshShelterMarkers() {
    for (const shelter of state.shelters.values()) shelter.marker.setIcon(shelterIcon(shelter));
  }

  function sensorReading(id) {
    return state.live.sensors[id] || { level_cm: 0, band: "dry", armed: true, updated_at: null };
  }

  function sensorIcon(sensor) {
    const reading = sensorReading(sensor.id);
    const level = Math.round(Number(reading.level_cm) || 0);
    const band = ["dry", "caution", "flooded"].includes(reading.band) ? reading.band : "dry";
    return L.divIcon({ className: "mk-wrap", html: `<div class="mk-sensor is-${band}">${ICONS.drop}${level} cm</div>`, iconSize: null, iconAnchor: [14, 10] });
  }

  function refreshSensorMarkers() {
    for (const sensor of state.sensors.values()) {
      sensor.marker.setIcon(sensorIcon(sensor));
      // Shade the stretch a sensor watches once water starts rising there.
      const band = sensorReading(sensor.id).band;
      const color = band === "flooded" ? COLORS.closed : COLORS.caution;
      if (band === "dry") {
        sensor.zone?.remove();
        sensor.zone = null;
      } else if (!sensor.zone) {
        sensor.zone = L.circle([sensor.lat, sensor.lon], { radius: sensor.radius_m, renderer, interactive: false, color, weight: 1, opacity: 0.6, fillColor: color, fillOpacity: 0.12 }).addTo(layers.zones);
      } else {
        sensor.zone.setStyle({ color, fillColor: color });
      }
    }
  }

  function popup(latlng, content) {
    L.popup({ maxWidth: 280, autoPanPadding: [20, 20] }).setLatLng(latlng).setContent(content).openOn(map);
  }

  // ---- clicks on the map -------------------------------------------------------------------

  function onRoadClick(road, latlng) {
    if (state.tab === "route") return setStart(latlng);
    if (state.tab === "plan") return toggleScenarioRoad(road.id);
    if (!state.session.responder) return toast("Sign in as a responder to mark roads blocked.");

    const closure = state.live.closures.get(road.id);
    const title = el("div", { class: "popup-title", text: StreetName(road.name) });
    if (closure) {
      const sub = el("div", { class: "popup-sub", text: `Blocked: ${closure.reason}. Reported by ${closure.reported_by} at ${fmtTime(closure.at)}.` });
      const reopen = el("button", { type: "button", class: "is-ok", text: "Reopen this road", onclick: () => reopenRoad(road.id) });
      return popup(latlng, el("div", {}, title, sub, el("div", { class: "popup-actions" }, reopen)));
    }
    const sub = el("div", { class: "popup-sub", text: `${fmtDist(road.length_m)} segment. Why is it blocked?` });
    const buttons = state.session.reasons.map((reason) =>
      el("button", { type: "button", class: "is-danger", text: reason, onclick: () => closeRoad(road.id, reason) }),
    );
    popup(latlng, el("div", {}, title, sub, el("div", { class: "popup-actions" }, buttons)));
  }

  function onShelterClick(shelter, latlng) {
    if (state.tab === "plan") return toggleScenarioShelter(shelter.id);
    const status = shelterStatus(shelter.id);
    const title = el("div", { class: "popup-title", text: shelter.name });
    if (state.tab === "respond" && state.session.responder) {
      const buttons = ["open", "full", "closed"].map((value) =>
        el("button", {
          type: "button",
          "aria-pressed": String(status === value),
          text: value[0].toUpperCase() + value.slice(1),
          onclick: () => setShelter(shelter.id, value),
        }),
      );
      return popup(latlng, el("div", {}, title, el("div", { class: "popup-sub", text: "Set this shelter's status:" }), el("div", { class: "popup-actions" }, buttons)));
    }
    const option = state.route.result?.options.find((o) => o.id === shelter.id);
    let line = `Shelter · ${status}`;
    if (option && option.reachable) line += ` · ${fmtEta(option.eta_s)} away`;
    if (option && !option.reachable) line += " · cut off by blocked roads";
    popup(latlng, el("div", {}, title, el("div", { class: "popup-sub", text: line })));
  }

  function onSensorClick(sensor, latlng) {
    const reading = sensorReading(sensor.id);
    const lines = [
      `${sensor.label}`,
      `Water: ${Number(reading.level_cm).toFixed(0)} cm (${reading.band})`,
      reading.updated_at ? `Last reading at ${fmtTime(reading.updated_at)}` : "No readings yet",
      `Closes the ${sensor.roads.length} road segments within ${Math.round(sensor.radius_m)} m at 30 cm, and reopens them below 15 cm.`,
    ];
    if (!reading.armed) lines.push("A responder reopened this road, so the sensor waits for the water to drop below 15 cm.");
    popup(latlng, el("div", {}, el("div", { class: "popup-title", text: `Flood sensor ${sensor.id}` }), ...lines.map((text) => el("div", { class: "popup-sub", text }))));
  }

  // ---- residents: routing ------------------------------------------------------------------

  function setStart(latlng) {
    state.route.start = { lat: latlng.lat, lon: latlng.lng };
    hideBanner();
    requestRoute({ fit: true });
  }

  async function requestRoute({ fit = false } = {}) {
    if (!state.route.start) return;
    const previous = state.route.result;
    try {
      state.route.result = await api("/api/route", { body: { ...state.route.start, mode: state.route.mode } });
      state.route.error = null;
    } catch (error) {
      state.route.result = null;
      state.route.error = error.message;
    }
    drawRoute(fit);
    renderRouteResult();
    renderShelterList();
    refreshShelterMarkers();
    return previous;
  }

  function drawRoute(fit) {
    layers.route.clearLayers();
    layers.start.clearLayers();
    const start = state.route.start;
    if (!start) return;
    const pin = L.divIcon({ className: "mk-wrap", html: '<div class="mk-pin"></div>', iconSize: [20, 20], iconAnchor: [10, 10] });
    L.marker([start.lat, start.lon], { icon: pin, title: "You are here", interactive: false, zIndexOffset: 1000 }).addTo(layers.start);

    const result = state.route.result;
    if (!result || state.tab !== "route") return;
    L.polyline([[start.lat, start.lon], [result.start.lat, result.start.lon]], { renderer, interactive: false, color: COLORS.ink, weight: 2, dashArray: "3 5", opacity: 0.7 }).addTo(layers.route);
    if (!result.route) return;
    const casing = L.polyline(result.route.geometry, { renderer, interactive: false, color: "#ffffff", weight: 11, opacity: 0.95, lineJoin: "round", lineCap: "round" });
    const line = L.polyline(result.route.geometry, { renderer, interactive: false, color: COLORS.route, weight: 6, opacity: 1, lineJoin: "round", lineCap: "round" });
    casing.addTo(layers.route);
    line.addTo(layers.route);
    if (fit) map.fitBounds(line.getBounds().extend([start.lat, start.lon]), { padding: [60, 60], maxZoom: 17 });
  }

  function renderRouteResult() {
    const box = $("#route-result");
    box.replaceChildren();
    const result = state.route.result;
    if (state.route.error) {
      box.append(el("div", { class: "empty" }, el("strong", { text: "Can't route from there" }), el("span", { text: state.route.error })));
      return;
    }
    if (!result) {
      box.append(el("div", { class: "empty" }, el("strong", { text: "No location yet" }), el("span", { text: "Tap anywhere on the map, use your location or pick your street." })));
      return;
    }
    if (!result.route) {
      box.append(
        el(
          "div",
          { class: "result is-stuck" },
          el("div", { class: "result-kicker", text: "No safe route right now" }),
          el("div", { class: "result-name", text: "Every open shelter is cut off from here" }),
          el("p", { class: "steps", text: "Blocked roads separate this spot from all open shelters, or the shelters you can reach are full. Stay where you are if it is safe, move to higher ground if water is rising, and call 112." }),
        ),
      );
      return;
    }
    const route = result.route;
    const steps = route.steps.map((step, i) =>
      el("li", {}, `${i === 0 ? "Head along" : "Continue onto"} ${streetName(step.name)} `, el("span", { class: "dist", text: `· ${fmtDist(step.length_m)}` })),
    );
    steps.push(el("li", {}, el("strong", { text: `Arrive at ${route.shelter.name}` })));
    const blocked = state.live.closures.size;
    box.append(
      el(
        "div",
        { class: "result" },
        el("div", { class: "result-kicker", text: result.mode === "foot" ? "Walk to" : "Drive to" }),
        el("div", { class: "result-name", text: route.shelter.name }),
        el(
          "div",
          { class: "result-stats" },
          el("div", { class: "stat" }, el("b", { text: fmtEta(route.eta_s) }), el("span", { text: "estimated time" })),
          el("div", { class: "stat" }, el("b", { text: fmtDist(route.distance_m) }), el("span", { text: "distance" })),
        ),
        el("ol", { class: "steps" }, steps),
        blocked ? el("div", { class: "avoided", text: `Avoids all ${blocked} blocked road${blocked === 1 ? "" : "s"}. This route updates by itself if roads close.` }) : null,
      ),
    );
  }

  function renderShelterList() {
    const list = $("#shelter-list");
    list.replaceChildren();
    const options = state.route.result?.options;
    const bestId = state.route.result?.route?.shelter.id;
    const rows = options || [...state.shelters.values()].map((s) => ({ id: s.id, name: s.name, status: shelterStatus(s.id), reachable: null }));
    for (const row of rows) {
      let pill;
      let sub;
      if (row.status !== "open") {
        pill = el("span", { class: `pill pill-${row.status}`, text: row.status === "full" ? "Full" : "Closed" });
        sub = row.status === "full" ? "Not taking more people" : "Not in use";
      } else if (row.reachable === false) {
        pill = el("span", { class: "pill pill-cut", text: "Cut off" });
        sub = "Blocked roads stand in the way";
      } else {
        pill = el("span", { class: "pill pill-open", text: "Open" });
        sub = row.reachable ? `${fmtEta(row.eta_s)} · ${fmtDist(row.distance_m)}` : "Tap the map to see how far";
      }
      const shelter = state.shelters.get(row.id);
      list.append(
        el(
          "li",
          { class: row.id === bestId ? "best" : null },
          el("span", { class: "grow" }, el("span", { class: "title", text: row.name, title: row.name }), el("span", { class: "sub", text: sub })),
          pill,
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: "Show", "aria-label": `Show ${row.name} on the map`, onclick: () => map.setView([shelter.lat, shelter.lon], 17) }),
        ),
      );
    }
  }

  function showBanner(text) {
    const banner = $("#route-banner");
    banner.textContent = text;
    banner.hidden = false;
  }

  function hideBanner() {
    $("#route-banner").hidden = true;
  }

  async function rerouteAfterChange(newlyClosed, shelterChanges) {
    const before = state.route.result;
    await requestRoute();
    const after = state.route.result;
    if (!before || !after) return;
    const sameRoute =
      before.route && after.route && before.route.shelter.id === after.route.shelter.id && before.route.roads.join() === after.route.roads.join();
    if (sameRoute || (!before.route && !after.route)) return;

    let reason = "Conditions changed.";
    const hit = before.route && newlyClosed.find((id) => before.route.roads.includes(id));
    if (hit) reason = `${StreetName(state.roads.get(hit).name)} was just blocked.`;
    else if (before.route && shelterChanges[before.route.shelter.id]) reason = `${before.route.shelter.name} is now ${shelterChanges[before.route.shelter.id]}.`;
    else if (after.route && (!before.route || after.route.eta_s < before.route.eta_s)) reason = "A road reopened.";

    if (after.route) showBanner(`Route updated. ${reason} Now ${fmtEta(after.route.eta_s)} to ${after.route.shelter.name}.`);
    else showBanner(`No safe route right now. ${reason}`);
  }

  // ---- responders --------------------------------------------------------------------------

  async function closeRoad(roadId, reason) {
    map.closePopup();
    try {
      await api("/api/closures", { body: { road: roadId, reason }, csrf: true });
      toast(`Marked ${streetName(state.roads.get(roadId).name)} as blocked. Every open map updates within seconds.`);
      syncNow();
    } catch (error) {
      handleResponderError(error);
    }
  }

  async function reopenRoad(roadId) {
    map.closePopup();
    try {
      await api(`/api/closures/${encodeURIComponent(roadId)}`, { method: "DELETE", csrf: true });
      toast(`Reopened ${streetName(state.roads.get(roadId).name)}.`);
      syncNow();
    } catch (error) {
      handleResponderError(error);
    }
  }

  async function setShelter(shelterId, status) {
    map.closePopup();
    try {
      await api(`/api/shelters/${encodeURIComponent(shelterId)}`, { body: { status }, csrf: true });
      toast(`${state.shelters.get(shelterId).name} is now ${status}.`);
      syncNow();
    } catch (error) {
      handleResponderError(error);
    }
  }

  function handleResponderError(error) {
    if (error.status === 401 || error.status === 403) {
      state.session.responder = null;
      renderResponder();
    }
    toast(error.message);
  }

  function renderResponder() {
    const signedIn = Boolean(state.session.responder);
    $("#login-form").hidden = signedIn;
    $("#responder-tools").hidden = !signedIn;
    $("#responder-name").textContent = state.session.responder || "";
    renderClosureList();
  }

  function renderClosureList() {
    const list = $("#closure-list");
    list.replaceChildren();
    $("#closure-count").textContent = String(state.live.closures.size);
    if (!state.live.closures.size) {
      list.append(el("li", { class: "list-empty", text: "No roads are blocked." }));
      return;
    }
    for (const closure of state.live.closures.values()) {
      const road = state.roads.get(closure.road);
      const sensor = closure.source.startsWith("sensor:");
      list.append(
        el(
          "li",
          {},
          el(
            "span",
            { class: "grow" },
            el("span", { class: "title", text: closure.name, title: closure.name }),
            el("span", { class: "sub", text: `${closure.reason} · ${closure.reported_by} · ${fmtTime(closure.at)}` }),
          ),
          sensor ? el("span", { class: "pill pill-sensor", text: "Sensor" }) : null,
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: "Show", onclick: () => map.setView(middleOf(road.geometry), 18) }),
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: "Reopen", "aria-label": `Reopen ${closure.name}`, onclick: () => reopenRoad(closure.road) }),
        ),
      );
    }
  }

  // ---- planners ----------------------------------------------------------------------------

  function toggleScenarioRoad(roadId) {
    if (state.live.closures.has(roadId)) return toast("That road is already blocked on the live map.");
    if (state.plan.roads.has(roadId)) state.plan.roads.delete(roadId);
    else state.plan.roads.add(roadId);
    restyleRoads();
    renderScenario();
    requestImpactSoon();
  }

  function toggleScenarioShelter(shelterId) {
    if (state.plan.shelters.has(shelterId)) state.plan.shelters.delete(shelterId);
    else state.plan.shelters.add(shelterId);
    refreshShelterMarkers();
    renderScenario();
    requestImpactSoon();
  }

  async function requestImpact() {
    try {
      state.plan.result = await api("/api/impact", {
        body: { mode: state.plan.mode, closures: [...state.plan.roads], shelters_closed: [...state.plan.shelters] },
      });
    } catch (error) {
      toast(error.message);
      return;
    }
    renderImpact();
    if (state.tab === "plan") restyleRoads();
  }
  const requestImpactSoon = debounce(requestImpact, 200);

  function renderScenario() {
    const list = $("#scenario-list");
    list.replaceChildren();
    const count = state.plan.roads.size + state.plan.shelters.size;
    $("#scenario-count").textContent = String(count);
    if (!count) {
      list.append(el("li", { class: "list-empty", text: "Tap roads or shelters on the map to test closing them." }));
      return;
    }
    for (const id of state.plan.roads) {
      const road = state.roads.get(id);
      list.append(
        el(
          "li",
          {},
          el("span", { class: "grow" }, el("span", { class: "title", text: StreetName(road.name) }), el("span", { class: "sub", text: `Road · ${fmtDist(road.length_m)}` })),
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: "Remove", onclick: () => toggleScenarioRoad(id) }),
        ),
      );
    }
    for (const id of state.plan.shelters) {
      list.append(
        el(
          "li",
          {},
          el("span", { class: "grow" }, el("span", { class: "title", text: state.shelters.get(id).name }), el("span", { class: "sub", text: "Shelter closed" })),
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: "Remove", onclick: () => toggleScenarioShelter(id) }),
        ),
      );
    }
  }

  function renderImpact() {
    const box = $("#impact");
    box.replaceChildren();
    const result = state.plan.result;
    if (!result) return;
    const { before, after } = result;
    const empty = state.plan.roads.size + state.plan.shelters.size === 0;

    let headline;
    let calm = false;
    if (empty) {
      headline = `Live coverage: ${before.reachable} of ${before.junctions} junctions can reach an open shelter. Tap roads to test closures.`;
      calm = before.cut_off === 0;
    } else if (result.newly_cut_off > 0) {
      headline = `${result.newly_cut_off} junction${result.newly_cut_off === 1 ? "" : "s"} would be cut off from every shelter.`;
    } else if (result.slower_by_a_minute_or_more > 0) {
      headline = `No one is cut off, but ${result.slower_by_a_minute_or_more} junction${result.slower_by_a_minute_or_more === 1 ? "" : "s"} would take a minute or more longer.`;
    } else {
      headline = "This scenario barely changes evacuation times.";
      calm = true;
    }

    const row = (label, a, b, worse) =>
      el("tr", {}, el("td", { text: label }), el("td", { text: a }), el("td", { class: worse ? "worse" : null, text: b }));
    box.append(
      el(
        "div",
        { class: "impact" },
        el("div", { class: `impact-headline${calm ? " is-calm" : ""}`, text: headline }),
        el(
          "table",
          {},
          el("thead", {}, el("tr", {}, el("th", { text: result.mode === "foot" ? "On foot" : "By vehicle" }), el("th", { text: "Live" }), el("th", { text: "Scenario" }))),
          el(
            "tbody",
            {},
            row("Can reach a shelter", `${before.reachable}`, `${after.reachable}`, after.reachable < before.reachable),
            row("Cut off", `${before.cut_off} (${before.cut_off_pct}%)`, `${after.cut_off} (${after.cut_off_pct}%)`, after.cut_off > before.cut_off),
            row("Average time", fmtEta(before.mean_eta_s), fmtEta(after.mean_eta_s), (after.mean_eta_s || 0) - (before.mean_eta_s || 0) >= 30),
            row("9 in 10 reach within", fmtEta(before.p90_eta_s), fmtEta(after.p90_eta_s), (after.p90_eta_s || 0) - (before.p90_eta_s || 0) >= 30),
            row("Slowest", fmtEta(before.worst_eta_s), fmtEta(after.worst_eta_s), (after.worst_eta_s || 0) - (before.worst_eta_s || 0) >= 30),
          ),
        ),
      ),
    );
  }

  // ---- live state --------------------------------------------------------------------------

  function setLive(ok) {
    const live = $("#live");
    live.classList.toggle("is-live", ok);
    live.classList.toggle("is-offline", !ok);
    if (!ok) $("#live-text").textContent = "Offline · retrying";
    else renderSummary();
  }

  function renderSummary() {
    const total = state.shelters.size;
    const open = [...state.shelters.keys()].filter((id) => shelterStatus(id) === "open").length;
    const blocked = state.live.closures.size;
    $("#live-text").textContent = `Live · ${blocked} road${blocked === 1 ? "" : "s"} blocked · ${open} of ${total} shelters open`;
  }

  function describe(event) {
    switch (event.action) {
      case "closed":
        return `${event.actor} blocked ${event.detail}`;
      case "reopened":
        return `${event.actor} reopened ${event.detail}`;
      case "water level":
        return `${event.actor} reports ${event.detail}`;
      case "reset":
        return `${event.actor} reset the demo`;
      default:
        if (event.action.startsWith("shelter ")) return `${event.actor} marked ${event.detail} ${event.action.slice(8)}`;
        return `${event.actor}: ${event.action} ${event.detail}`;
    }
  }

  function renderEvents() {
    const list = $("#events");
    list.replaceChildren();
    if (!state.live.events.length) {
      list.append(el("li", {}, el("time", { text: "" }), el("span", { text: "No activity yet." })));
      return;
    }
    for (const event of state.live.events) {
      list.append(el("li", {}, el("time", { datetime: event.at, text: fmtTime(event.at) }), el("span", { text: describe(event) })));
    }
  }

  function applyState(data) {
    if (state.live.version != null && data.version < state.live.version) return;
    const previous = state.live;
    const closures = new Map(data.closures.map((c) => [c.road, c]));
    const newlyClosed = [...closures.keys()].filter((id) => !previous.closures.has(id));
    const shelterChanges = {};
    for (const [id, status] of Object.entries(data.shelters)) {
      if ((previous.shelters[id] || "open") !== status) shelterChanges[id] = status;
    }
    const first = previous.version == null;
    state.live = { version: data.version, closures, shelters: data.shelters, sensors: data.sensors, events: data.events };

    for (const id of closures.keys()) state.plan.roads.delete(id);
    restyleRoads();
    refreshShelterMarkers();
    refreshSensorMarkers();
    renderEvents();
    renderSummary();
    renderClosureList();
    renderScenario();
    if (first) {
      renderShelterList();
      return;
    }
    if (state.route.result) rerouteAfterChange(newlyClosed, shelterChanges);
    else renderShelterList();
    if (state.plan.result) requestImpactSoon();
  }

  let polling = false;
  async function syncNow() {
    if (polling) return;
    polling = true;
    let data;
    try {
      const since = state.live.version == null ? "" : `?since=${state.live.version}`;
      data = await api(`/api/state${since}`);
      setLive(true);
    } catch {
      setLive(false);
      polling = false;
      return;
    }
    try {
      if (data) applyState(data);
    } catch (error) {
      console.error("Couldn't apply the latest state", error);
    } finally {
      polling = false;
    }
  }

  function startPolling() {
    syncNow();
    setInterval(syncNow, POLL_MS);
  }

  // ---- tabs and controls -------------------------------------------------------------------

  function selectTab(name, focus = false) {
    state.tab = name;
    for (const button of $$(".tabs [role=tab]")) {
      const selected = button.dataset.tab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    }
    for (const panel of $$(".tab")) panel.hidden = panel.id !== `tab-${name}`;
    if (map) {
      map.closePopup();
      restyleRoads();
      refreshShelterMarkers();
      drawRoute(false);
      if (name === "plan" && !state.plan.result) requestImpact();
    }
  }

  function wireTabs() {
    const tabs = $$(".tabs [role=tab]");
    for (const [i, button] of tabs.entries()) {
      button.addEventListener("click", () => selectTab(button.dataset.tab));
      button.addEventListener("keydown", (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        selectTab(tabs[(i + step + tabs.length) % tabs.length].dataset.tab, true);
      });
    }
  }

  function wireModeGroup(group, onChange) {
    const buttons = $$(`[data-mode-group="${group}"] [role=radio]`);
    for (const button of buttons) {
      button.addEventListener("click", () => {
        for (const other of buttons) other.setAttribute("aria-checked", String(other === button));
        onChange(button.dataset.mode);
      });
    }
  }

  function wireControls() {
    wireModeGroup("route", (mode) => {
      state.route.mode = mode;
      hideBanner();
      requestRoute();
    });
    wireModeGroup("plan", (mode) => {
      state.plan.mode = mode;
      requestImpact();
    });

    $("#locate").addEventListener("click", () => {
      if (!navigator.geolocation) return toast("This browser can't share your location. Tap the map instead.");
      navigator.geolocation.getCurrentPosition(
        (position) => {
          const latlng = L.latLng(position.coords.latitude, position.coords.longitude);
          setStart(latlng);
        },
        () => toast("Couldn't get your location. Tap the map or pick your street instead."),
        { enableHighAccuracy: true, timeout: 10000 },
      );
    });

    $("#street").addEventListener("change", (event) => {
      const name = event.target.value;
      if (!name) return;
      const segments = [...state.roads.values()].filter((r) => r.name === name);
      const longest = segments.reduce((a, b) => (b.length_m > a.length_m ? b : a));
      const [lat, lon] = middleOf(longest.geometry);
      setStart(L.latLng(lat, lon));
    });

    $("#login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = $("#login-error");
      error.hidden = true;
      try {
        const data = await api("/api/responder/login", {
          body: { name: $("#login-name").value, passcode: $("#login-passcode").value },
        });
        state.session.responder = data.responder;
        state.session.csrf = data.csrf;
        $("#login-passcode").value = "";
        renderResponder();
        toast(`Signed in as ${data.responder}. Tap a road to mark it blocked.`);
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    });

    $("#logout").addEventListener("click", async () => {
      const data = await api("/api/responder/logout", { body: {} }).catch(() => null);
      state.session.responder = null;
      if (data) state.session.csrf = data.csrf;
      renderResponder();
    });

    $("#reset").addEventListener("click", async () => {
      if (!window.confirm("Reset the demo? This reopens every road, reopens every shelter and clears sensor readings.")) return;
      try {
        await api("/api/reset", { body: {}, csrf: true });
        toast("Demo reset.");
        syncNow();
      } catch (error) {
        handleResponderError(error);
      }
    });

    $("#coverage").addEventListener("change", (event) => {
      state.plan.coverage = event.target.checked;
      $("#scale").hidden = !event.target.checked;
      restyleRoads();
    });

    $("#scenario-clear").addEventListener("click", () => {
      state.plan.roads.clear();
      state.plan.shelters.clear();
      restyleRoads();
      refreshShelterMarkers();
      renderScenario();
      requestImpact();
    });
  }

  function fillStreets() {
    const names = [...new Set([...state.roads.values()].map((r) => r.name))].filter((n) => n !== "Unnamed road").sort();
    const select = $("#street");
    for (const name of names) select.append(el("option", { value: name, text: name }));
  }

  // ---- start -------------------------------------------------------------------------------

  async function init() {
    COLORS = {
      ink: cssVar("--ink"),
      caution: cssVar("--full"),
      route: cssVar("--route"),
      closed: cssVar("--closed"),
      scenario: cssVar("--scenario"),
      s1: cssVar("--s1"),
      s2: cssVar("--s2"),
      s3: cssVar("--s3"),
      s4: cssVar("--s4"),
      s5: cssVar("--s5"),
    };
    wireTabs();
    wireControls();
    try {
      state.area = await api("/api/area");
    } catch {
      $("#live-text").textContent = "Couldn't load the map. Reload to try again.";
      return;
    }
    for (const road of state.area.roads) state.roads.set(road.id, road);
    for (const [id, lat, lon] of state.area.nodes) state.nodes.set(id, [lat, lon]);
    for (const shelter of state.area.shelters) state.shelters.set(shelter.id, shelter);
    for (const sensor of state.area.sensors) state.sensors.set(sensor.id, sensor);

    initMap();
    fillStreets();
    renderShelterList();
    renderScenario();
    api("/api/session")
      .then((session) => {
        state.session = session;
        renderResponder();
      })
      .catch(() => {});
    startPolling();
  }

  init();
})();
