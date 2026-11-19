"use strict";

/*
 * SafeRoute map client.
 *
 * One page, three roles:
 *   - residents tap where they are and get a route to the best open, reachable shelter
 *     (safest or fastest), report problems, or try the SMS service;
 *   - responders sign in, block and reopen roads, set shelter status and occupancy,
 *     verify reports and play the flood story;
 *   - planners try hypothetical closures and see how evacuation times change.
 *
 * The page polls /api/state every few seconds. When anything changes the map restyles and any
 * route on screen is recomputed. Offline, routes are worked out on the device from the last
 * state it received. All server text is inserted with textContent, never as HTML.
 */
(() => {
  const POLL_MS = 3000;
  const MAJOR = new Set(["motorway", "trunk", "primary", "secondary"]);
  const WALK_KMH = 4;
  const DRIVE_KMH = { motorway: 40, trunk: 40, primary: 30, secondary: 25, tertiary: 22, unclassified: 18, residential: 15, living_street: 10 };
  const MAX_SNAP_M = 400;
  const ICONS = {
    house: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 3 2 11h3v9h5v-6h4v6h5v-9h3z"/></svg>',
    drop: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2s7 8 7 13a7 7 0 0 1-14 0c0-5 7-13 7-13z"/></svg>',
  };
  const I18N = window.SR_I18N;

  const state = {
    lang: "en",
    tab: "route",
    area: null,
    roads: new Map(),
    nodes: new Map(),
    adjacency: new Map(),
    shelters: new Map(),
    sensors: new Map(),
    live: { version: null, closures: new Map(), shelters: {}, sensors: {}, events: [], risk: {}, reports: [], weather: null, story: null, at: null },
    session: { responder: null, csrf: "", reasons: [] },
    route: { mode: "foot", prefer: "safest", start: null, result: null, error: null },
    plan: { mode: "foot", roads: new Set(), shelters: new Set(), result: null, coverage: true },
    storyHiddenAt: null,
  };

  // ---- language ----------------------------------------------------------------------------

  function t(key, vars = {}) {
    const table = I18N[state.lang] || I18N.en;
    const text = table[key] ?? I18N.en[key] ?? key;
    return text.replace(/\{(\w+)\}/g, (_, name) => (vars[name] ?? `{${name}}`));
  }

  function storeGet(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storeSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* private mode: fine */
    }
  }

  function pickLanguage() {
    const saved = storeGet("saferoute.lang");
    if (saved && I18N[saved]) return saved;
    const browser = (navigator.language || "en").slice(0, 2);
    return I18N[browser] ? browser : "en";
  }

  function applyStaticText() {
    document.documentElement.lang = state.lang;
    for (const node of $$("[data-i18n]")) node.textContent = t(node.dataset.i18n);
    for (const node of $$("[data-i18n-placeholder]")) node.placeholder = t(node.dataset.i18nPlaceholder);
    for (const node of $$("[data-i18n-aria]")) node.setAttribute("aria-label", t(node.dataset.i18nAria));
    $("#lang").value = state.lang;
  }

  function setLanguage(lang) {
    state.lang = I18N[lang] ? lang : "en";
    storeSet("saferoute.lang", state.lang);
    applyStaticText();
    renderEverything();
  }

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

  const fmtEta = (s) => (s == null ? "–" : t("unit.min", { n: Math.max(1, Math.round(s / 60)) }));
  const fmtDist = (m) => (m == null ? "–" : m < 1000 ? `${Math.max(10, Math.round(m / 10) * 10)} m` : `${(m / 1000).toFixed(1)} km`);
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  const street = (name) => (name === "Unnamed road" ? t("street.unnamed") : name);
  const Street = (name) => (name === "Unnamed road" ? t("street.Unnamed") : name);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let COLORS = {};

  function distanceM(a, b) {
    const rad = Math.PI / 180;
    const dLat = (b[0] - a[0]) * rad;
    const dLon = (b[1] - a[1]) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.sqrt(h));
  }

  async function api(path, { method, body, csrf } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (csrf) headers["X-CSRF-Token"] = state.session.csrf;
    const response = await fetch(path, {
      method: method || (body !== undefined ? "POST" : "GET"),
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      cache: "no-cache",
    });
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `Something went wrong (${response.status}).`);
      error.status = response.status;
      error.code = data.code;
      throw error;
    }
    return data;
  }

  function errorText(error) {
    if (error.code && I18N.en[`err.${error.code}`]) return t(`err.${error.code}`);
    return error.message;
  }

  let toastTimer;
  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.hidden = true), 4500);
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  function shelterLive(id) {
    return state.live.shelters[id] || { status: "open", set_status: "open", capacity: state.shelters.get(id)?.capacity || 0, occupancy: 0, en_route: 0 };
  }

  function reasonText(reason) {
    if (reason.type === "rising") return t("reason.rising", { place: reason.place, value: Math.round(reason.value) });
    if (reason.type === "report") return t("reason.report", { kind: t(`kind.${reason.place}`) });
    return t("reason.rain", { place: reason.place });
  }

  function reasonsText(reasons) {
    return reasons.slice(0, 3).map(reasonText).join("; ");
  }

  // ---- map ---------------------------------------------------------------------------------

  let map;
  let renderer;
  const layers = {};

  function initMap() {
    const [lat, lon] = state.area.area.center;
    map = L.map("map", { zoomControl: true, preferCanvas: true, minZoom: 13, zoomSnap: 0.5 }).setView([lat, lon], 15);
    // The basemap is optional: roads, closures and routes are drawn from local data.
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      className: "basemap",
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);

    renderer = L.canvas({ tolerance: 8 });
    for (const name of ["zones", "roads", "route", "marks", "start", "reports", "shelters", "sensors"]) {
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
      shelter.marker = L.marker([shelter.lat, shelter.lon], { icon: shelterIcon(shelter), keyboard: true, title: shelter.name, alt: shelter.name });
      shelter.marker.bindTooltip(el("span", { text: shelter.name }), { direction: "top", offset: [0, -14] });
      shelter.marker.on("click", (event) => onShelterClick(shelter, event.latlng));
      shelter.marker.addTo(layers.shelters);
    }
    for (const sensor of state.sensors.values()) {
      sensor.marker = L.marker([sensor.lat, sensor.lon], { icon: sensorIcon(sensor), title: sensor.id, zIndexOffset: 500 });
      sensor.marker.on("click", (event) => onSensorClick(sensor, event.latlng));
      sensor.marker.addTo(layers.sensors);
    }
  }

  const middleOf = (geometry) => geometry[Math.floor(geometry.length / 2)];

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
    const times = [eta[road.u], eta[road.v]].filter((x) => x != null);
    return times.length ? Math.min(...times) : null;
  }

  function roadStyle(road) {
    const weight = MAJOR.has(road.kind) ? 5 : 3;
    if (state.live.closures.has(road.id)) return { color: COLORS.closed, weight: weight + 3, opacity: 1, dashArray: "10 7", lineCap: "butt" };
    if (state.tab === "plan" && state.plan.roads.has(road.id)) return { color: COLORS.scenario, weight: weight + 3, opacity: 1, dashArray: "1 9", lineCap: "round" };
    if (state.tab === "plan" && state.plan.coverage && state.plan.result) {
      const eta = roadEta(road);
      return { color: coverageColor(eta), weight: weight + 1, opacity: eta == null ? 0.75 : 0.9, dashArray: eta == null ? "4 6" : null, lineCap: "round" };
    }
    if (state.live.risk[road.id]) return { color: COLORS.risky, weight: weight + 2, opacity: 0.95, dashArray: "6 5", lineCap: "round" };
    return { color: "#64748b", weight, opacity: MAJOR.has(road.kind) ? 0.5 : 0.32, dashArray: null, lineCap: "round" };
  }

  function restyleRoads() {
    for (const road of state.roads.values()) road.line.setStyle(roadStyle(road));
    for (const id of Object.keys(state.live.risk)) state.roads.get(id)?.line.bringToFront();
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
    for (const closure of state.live.closures.values()) {
      const road = state.roads.get(closure.road);
      if (road && !closure.source.startsWith("sensor:")) add(road, "");
    }
    if (state.tab === "plan") {
      for (const id of state.plan.roads) if (!state.live.closures.has(id)) add(state.roads.get(id), "is-scenario");
    }
  }

  function shelterIcon(shelter) {
    const live = shelterLive(shelter.id);
    const classes = ["mk-shelter"];
    if (state.tab === "plan" && state.plan.shelters.has(shelter.id)) classes.push("is-scenario");
    else if (live.status !== "open") classes.push(`is-${live.status}`);
    else if ((live.occupancy + live.en_route) / live.capacity >= 0.7) classes.push("is-busy");
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
      const band = sensorReading(sensor.id).band;
      const color = band === "flooded" ? COLORS.closed : COLORS.risky;
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

  function drawReports() {
    layers.reports.clearLayers();
    for (const report of state.live.reports) {
      const cls = report.kind === "rescue" ? "mk-report is-rescue" : "mk-report";
      const icon = L.divIcon({ className: "mk-wrap", html: `<div class="${cls}">!</div>`, iconSize: [24, 24], iconAnchor: [12, 12] });
      const marker = L.marker([report.lat, report.lon], { icon, title: t(`kind.${report.kind}`), zIndexOffset: 300 });
      marker.on("click", (event) => {
        const lines = [el("div", { class: "popup-title", text: `#${report.id} · ${t(`kind.${report.kind}`)}` }), el("div", { class: "popup-sub", text: t("report.popup", { kind: t(`kind.${report.kind}`), place: report.place, time: fmtTime(report.at) }) })];
        if (report.note) lines.push(el("div", { class: "popup-note", text: report.note }));
        if (state.session.responder) lines.push(reportActions(report));
        popup(event.latlng, el("div", {}, lines));
      });
      marker.addTo(layers.reports);
    }
  }

  function popup(latlng, content) {
    L.popup({ maxWidth: 300, autoPanPadding: [20, 20] }).setLatLng(latlng).setContent(content).openOn(map);
  }

  // ---- clicks on the map -------------------------------------------------------------------

  function onRoadClick(road, latlng) {
    if (state.tab === "route") return setStart(latlng);
    if (state.tab === "plan") return toggleScenarioRoad(road.id);
    if (!state.session.responder) return toast(t("popup.sign_in_first"));

    const closure = state.live.closures.get(road.id);
    const title = el("div", { class: "popup-title", text: Street(road.name) });
    if (closure) {
      const sub = el("div", { class: "popup-sub", text: t("popup.blocked_by", { reason: closure.reason, by: closure.reported_by, time: fmtTime(closure.at) }) });
      const reopen = el("button", { type: "button", class: "is-ok", text: t("popup.reopen"), onclick: () => reopenRoad(road.id) });
      return popup(latlng, el("div", {}, title, sub, el("div", { class: "popup-actions" }, reopen)));
    }
    const sub = el("div", { class: "popup-sub", text: t("popup.why", { len: fmtDist(road.length_m) }) });
    const buttons = state.session.reasons.map((reason) => el("button", { type: "button", class: "is-danger", text: t(`closure.${reason}`), onclick: () => closeRoad(road.id, reason) }));
    popup(latlng, el("div", {}, title, sub, el("div", { class: "popup-actions" }, buttons)));
  }

  function onShelterClick(shelter, latlng) {
    if (state.tab === "plan") return toggleScenarioShelter(shelter.id);
    const live = shelterLive(shelter.id);
    const title = el("div", { class: "popup-title", text: shelter.name });
    const summary = el("div", { class: "popup-sub", text: t("shelter.popup", { status: t(`status.${live.status}`), occ: live.occupancy, cap: live.capacity }) });
    if (state.tab === "respond" && state.session.responder) {
      const statusButtons = ["open", "full", "closed"].map((value) =>
        el("button", { type: "button", "aria-pressed": String(live.set_status === value), text: t(`status.${value}`), onclick: () => updateShelter(shelter.id, { status: value }) }),
      );
      const input = el("input", { type: "number", min: "0", max: String(live.capacity * 2), value: String(live.occupancy), class: "popup-input", "aria-label": t("popup.people", { cap: live.capacity }) });
      const save = el("button", { type: "button", class: "is-ok", text: t("popup.save"), onclick: () => updateShelter(shelter.id, { occupancy: Number(input.value) }) });
      return popup(
        latlng,
        el(
          "div",
          {},
          title,
          summary,
          el("div", { class: "popup-label", text: t("popup.status") }),
          el("div", { class: "popup-actions" }, statusButtons),
          el("div", { class: "popup-label", text: t("popup.people", { cap: live.capacity }) }),
          el("div", { class: "popup-actions" }, input, save),
        ),
      );
    }
    const option = state.route.result?.options?.find((o) => o.id === shelter.id);
    const extra = option?.reachable ? ` · ${fmtEta(option.eta_s)}` : "";
    popup(latlng, el("div", {}, title, el("div", { class: "popup-sub", text: summary.textContent + extra })));
  }

  function onSensorClick(sensor, latlng) {
    const reading = sensorReading(sensor.id);
    const lines = [
      sensor.label,
      t("sensor.water", { cm: Number(reading.level_cm).toFixed(0), band: t(`band.${reading.band}`) }),
      reading.updated_at ? t("sensor.last", { time: fmtTime(reading.updated_at) }) : t("sensor.none"),
      t("sensor.rule", { n: sensor.roads.length, r: Math.round(sensor.radius_m) }),
    ];
    if (!reading.armed) lines.push(t("sensor.quiet"));
    popup(latlng, el("div", {}, el("div", { class: "popup-title", text: t("sensor.title", { id: sensor.id }) }), ...lines.map((text) => el("div", { class: "popup-sub", text }))));
  }

  // ---- residents: routing ------------------------------------------------------------------

  function setStart(latlng) {
    state.route.start = { lat: latlng.lat, lon: latlng.lng };
    hideBanner();
    requestRoute({ fit: true });
  }

  async function requestRoute({ fit = false } = {}) {
    if (!state.route.start) return;
    try {
      state.route.result = await api("/api/route", { body: { ...state.route.start, mode: state.route.mode, prefer: state.route.prefer } });
      state.route.error = null;
    } catch (error) {
      if (error.status === undefined) {
        const offline = offlineRoute(state.route.start.lat, state.route.start.lon, state.route.mode);
        state.route.result = offline.result;
        state.route.error = offline.error;
      } else {
        state.route.result = null;
        state.route.error = errorText(error);
      }
    }
    drawRoute(fit);
    renderRouteResult();
    renderShelterList();
    refreshShelterMarkers();
  }

  function drawRoute(fit) {
    layers.route.clearLayers();
    layers.start.clearLayers();
    const start = state.route.start;
    if (!start) return;
    const pin = L.divIcon({ className: "mk-wrap", html: '<div class="mk-pin"></div>', iconSize: [20, 20], iconAnchor: [10, 10] });
    L.marker([start.lat, start.lon], { icon: pin, interactive: false, zIndexOffset: 1000 }).addTo(layers.start);

    const result = state.route.result;
    if (!result || state.tab !== "route") return;
    L.polyline([[start.lat, start.lon], [result.start.lat, result.start.lon]], { renderer, interactive: false, color: COLORS.ink, weight: 2, dashArray: "3 5", opacity: 0.7 }).addTo(layers.route);
    if (!result.route) return;
    const casing = L.polyline(result.route.geometry, { renderer, interactive: false, color: "#ffffff", weight: 11, opacity: 0.95, lineJoin: "round", lineCap: "round" });
    const line = L.polyline(result.route.geometry, { renderer, interactive: false, color: COLORS.route, weight: 6, opacity: 1, lineJoin: "round", lineCap: "round" });
    casing.addTo(layers.route);
    line.addTo(layers.route);
    for (const risky of result.route.risky || []) {
      const road = state.roads.get(risky.road);
      if (road) L.polyline(road.geometry, { renderer, interactive: false, color: COLORS.risky, weight: 6, opacity: 1, dashArray: "4 6" }).addTo(layers.route);
    }
    if (fit) map.fitBounds(line.getBounds().extend([start.lat, start.lon]), { padding: [60, 60], maxZoom: 17 });
  }

  function arriveBox(route) {
    const people = el("select", { "aria-label": t("arrive.people") }, Array.from({ length: 10 }, (_, i) => el("option", { value: String(i + 1), text: String(i + 1) })));
    const message = el("p", { class: "arrive-msg", role: "status" });
    const button = el("button", {
      type: "button",
      class: "btn btn-primary btn-auto",
      text: t("arrive.button"),
      onclick: async () => {
        button.disabled = true;
        try {
          await api("/api/arrivals", { body: { shelter: route.shelter.id, people: Number(people.value) } });
          message.textContent = t("arrive.done");
          syncNow();
        } catch (error) {
          message.textContent = errorText(error);
          button.disabled = false;
        }
      },
    });
    return el("div", { class: "arrive" }, el("div", { class: "arrive-row" }, el("label", { class: "arrive-people" }, t("arrive.people"), people), button), message);
  }

  function renderRouteResult() {
    const box = $("#route-result");
    box.replaceChildren();
    const result = state.route.result;
    if (state.route.error && !result) {
      box.append(el("div", { class: "empty" }, el("strong", { text: t("route.error_title") }), el("span", { text: state.route.error })));
      return;
    }
    if (!result) {
      box.append(el("div", { class: "empty" }, el("strong", { text: t("route.empty_title") }), el("span", { text: t("route.empty_text") })));
      return;
    }
    const offlineNote = result.offline ? el("p", { class: "note-line is-offline", text: t("route.offline", { time: fmtTime(state.live.at) }) }) : null;
    if (!result.route) {
      box.append(
        el(
          "div",
          { class: "result is-stuck" },
          el("div", { class: "result-kicker", text: t("route.stuck_kicker") }),
          el("div", { class: "result-name", text: t("route.stuck_title") }),
          el("p", { class: "steps", text: t("route.stuck_text") }),
          offlineNote,
        ),
      );
      return;
    }
    const route = result.route;
    const steps = route.steps.map((step, i) => el("li", {}, `${t(i === 0 ? "route.head" : "route.continue", { street: street(step.name) })} `, el("span", { class: "dist", text: `· ${fmtDist(step.length_m)}` })));
    steps.push(el("li", {}, el("strong", { text: t("route.arrive", { shelter: route.shelter.name }) })));

    const notes = [];
    if (route.balanced_from) {
      notes.push(el("p", { class: "note-line is-balance", text: t("route.balanced", { shelter: route.balanced_from.shelter.name, pct: route.balanced_from.load_pct, extra: fmtEta(route.balanced_from.extra_s) }) }));
    }
    if (route.avoided?.length && route.fastest_eta_s != null) {
      notes.push(el("p", { class: "note-line is-safe", text: t("route.safer", { extra: fmtEta(Math.max(60, route.eta_s - route.fastest_eta_s)), reasons: reasonsText(route.avoided) }) }));
    }
    if (route.risky?.length) {
      const reasons = route.risky.flatMap((r) => r.reasons);
      notes.push(el("p", { class: "note-line is-risk", text: t("route.risky", { reasons: reasonsText(reasons) }) }));
    }
    const blocked = state.live.closures.size;
    if (blocked) notes.push(el("p", { class: "note-line", text: t("route.avoids", { n: blocked }) }));

    box.append(
      el(
        "div",
        { class: "result" },
        el("div", { class: "result-kicker", text: t(result.mode === "foot" ? "route.walk_to" : "route.drive_to") }),
        el("div", { class: "result-name", text: route.shelter.name }),
        el(
          "div",
          { class: "result-stats" },
          el("div", { class: "stat" }, el("b", { text: fmtEta(route.eta_s) }), el("span", { text: t("route.eta") })),
          el("div", { class: "stat" }, el("b", { text: fmtDist(route.distance_m) }), el("span", { text: t("route.distance") })),
        ),
        offlineNote,
        notes,
        el("ol", { class: "steps" }, steps),
        result.offline ? null : arriveBox(route),
      ),
    );
  }

  function renderShelterList() {
    const list = $("#shelter-list");
    list.replaceChildren();
    const options = state.route.result?.options;
    const bestId = state.route.result?.route?.shelter.id;
    const rows = options || [...state.shelters.values()].map((s) => ({ id: s.id, name: s.name, status: shelterLive(s.id).status, reachable: null }));
    for (const row of rows) {
      const live = shelterLive(row.id);
      let pill;
      let sub;
      if (row.status !== "open") {
        pill = el("span", { class: `pill pill-${row.status}`, text: t(`shelter.${row.status}`) });
        sub = t(row.status === "full" ? "shelter.not_taking" : "shelter.not_used");
      } else if (row.reachable === false) {
        pill = el("span", { class: "pill pill-cut", text: t("shelter.cut") });
        sub = t("shelter.blocked");
      } else {
        pill = el("span", { class: "pill pill-open", text: t("shelter.open") });
        sub = row.reachable ? `${fmtEta(row.eta_s)} · ${fmtDist(row.distance_m)}` : t("shelter.tap");
      }
      const load = Math.min(1, (live.occupancy + live.en_route) / (live.capacity || 1));
      const bar = el("span", { class: "load" }, el("span", { class: `load-fill${load >= 0.9 ? " is-high" : load >= 0.7 ? " is-mid" : ""}` }));
      bar.firstChild.style.width = `${Math.round(load * 100)}%`;
      const loadText = `${t("shelter.load", { occ: live.occupancy, cap: live.capacity })}${live.en_route ? ` ${t("shelter.coming", { n: live.en_route })}` : ""}`;
      const shelter = state.shelters.get(row.id);
      list.append(
        el(
          "li",
          { class: row.id === bestId ? "best" : null },
          el("span", { class: "grow" }, el("span", { class: "title", text: row.name, title: row.name }), el("span", { class: "sub", text: sub }), bar, el("span", { class: "sub small", text: loadText })),
          pill,
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("shelter.show"), onclick: () => map.setView([shelter.lat, shelter.lon], 17) }),
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
    const same = before.route && after.route && before.route.shelter.id === after.route.shelter.id && before.route.roads.join() === after.route.roads.join();
    if (same || (!before.route && !after.route)) return;

    let reason = t("banner.changed");
    const hit = before.route && newlyClosed.find((id) => before.route.roads.includes(id));
    if (hit) reason = t("banner.blocked", { street: Street(state.roads.get(hit).name) });
    else if (before.route && shelterChanges[before.route.shelter.id]) reason = t("banner.shelter", { shelter: before.route.shelter.name, status: t(`status.${shelterChanges[before.route.shelter.id]}`) });
    else if (after.route && (!before.route || after.route.eta_s < before.route.eta_s)) reason = t("banner.reopened");

    if (after.route) showBanner(t("banner.updated", { reason, eta: fmtEta(after.route.eta_s), shelter: after.route.shelter.name }));
    else showBanner(t("banner.none", { reason }));
  }

  // ---- offline routing (Dijkstra on this device) ------------------------------------------

  function buildAdjacency() {
    for (const road of state.roads.values()) {
      const foot = road.length_m / (WALK_KMH / 3.6);
      const vehicle = road.length_m / ((DRIVE_KMH[road.kind] || DRIVE_KMH.residential) / 3.6);
      for (const [a, b] of [[road.u, road.v], [road.v, road.u]]) {
        if (!state.adjacency.has(a)) state.adjacency.set(a, []);
        state.adjacency.get(a).push({ to: b, road, foot, vehicle });
      }
    }
  }

  function offlineRoute(lat, lon, mode) {
    if (!state.area) return { result: null, error: t("route.offline_fail") };
    let start = null;
    let snap = Infinity;
    for (const [id, coords] of state.nodes) {
      const d = distanceM([lat, lon], coords);
      if (d < snap) {
        snap = d;
        start = id;
      }
    }
    if (snap > MAX_SNAP_M) return { result: null, error: t("route.outside") };

    // Binary-heap Dijkstra over open roads.
    const dist = new Map([[start, 0]]);
    const prev = new Map();
    const heap = [[0, start]];
    const push = (item) => {
      heap.push(item);
      let i = heap.length - 1;
      while (i > 0) {
        const p = (i - 1) >> 1;
        if (heap[p][0] <= heap[i][0]) break;
        [heap[p], heap[i]] = [heap[i], heap[p]];
        i = p;
      }
    };
    const pop = () => {
      const top = heap[0];
      const last = heap.pop();
      if (heap.length) {
        heap[0] = last;
        let i = 0;
        for (;;) {
          const l = 2 * i + 1;
          const r = l + 1;
          let m = i;
          if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
          if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
          if (m === i) break;
          [heap[m], heap[i]] = [heap[i], heap[m]];
          i = m;
        }
      }
      return top;
    };
    while (heap.length) {
      const [d, node] = pop();
      if (d > dist.get(node)) continue;
      for (const edge of state.adjacency.get(node) || []) {
        if (state.live.closures.has(edge.road.id)) continue;
        const nd = d + edge[mode];
        if (nd < (dist.get(edge.to) ?? Infinity)) {
          dist.set(edge.to, nd);
          prev.set(edge.to, edge);
          push([nd, edge.to]);
        }
      }
    }

    const lead = snap / ((mode === "foot" ? WALK_KMH : DRIVE_KMH.residential) / 3.6);
    const options = [...state.shelters.values()].map((s) => {
      const live = shelterLive(s.id);
      const node = s.node ?? null;
      const reachable = node != null && dist.has(node);
      return { id: s.id, name: s.name, status: live.status, reachable, eta_s: reachable ? dist.get(node) + lead : null, distance_m: null };
    });
    const usable = options.filter((o) => o.status === "open" && o.reachable).sort((a, b) => a.eta_s - b.eta_s);
    const base = { offline: true, mode, start: { lat: state.nodes.get(start)[0], lon: state.nodes.get(start)[1] }, options };
    if (!usable.length) return { result: { ...base, route: null }, error: null };

    const best = usable[0];
    const shelter = state.shelters.get(best.id);
    const edges = [];
    for (let at = shelter.node; at !== start; at = prev.get(at).road.u === at ? prev.get(at).road.v : prev.get(at).road.u) {
      edges.push(prev.get(at));
      if (edges.length > 5000) break;
    }
    edges.reverse();
    let from = start;
    const geometry = [];
    const steps = [];
    let distance = snap;
    for (const edge of edges) {
      const coords = edge.road.u === from ? edge.road.geometry : [...edge.road.geometry].reverse();
      geometry.push(...(geometry.length ? coords.slice(1) : coords));
      distance += edge.road.length_m;
      if (steps.length && steps.at(-1).name === edge.road.name) steps.at(-1).length_m += edge.road.length_m;
      else steps.push({ name: edge.road.name, length_m: edge.road.length_m });
      from = edge.to;
    }
    best.distance_m = distance;
    return {
      result: { ...base, route: { shelter: { id: best.id, name: best.name }, eta_s: best.eta_s, distance_m: distance, steps, roads: edges.map((e) => e.road.id), geometry, risky: [], avoided: [] } },
      error: null,
    };
  }

  // ---- residents: reports and SMS ----------------------------------------------------------

  async function sendReport(kind) {
    const message = $("#report-msg");
    message.hidden = false;
    if (!state.route.start) {
      message.textContent = t("report.need_spot");
      return;
    }
    try {
      const data = await api("/api/reports", { body: { ...state.route.start, kind, note: $("#report-note").value } });
      message.textContent = t(kind === "rescue" ? "report.sent_rescue" : "report.sent", { id: data.id, place: data.place });
      $("#report-note").value = "";
      syncNow();
    } catch (error) {
      message.textContent = errorText(error);
    }
  }

  async function sendSms(text) {
    const thread = $("#sms-thread");
    thread.append(el("li", { class: "bubble is-me", text }));
    thread.scrollTop = thread.scrollHeight;
    try {
      const data = await api("/api/sms/simulate", { body: { text } });
      thread.append(el("li", { class: "bubble", text: data.reply }));
      syncNow();
    } catch (error) {
      thread.append(el("li", { class: "bubble is-error", text: errorText(error) }));
    }
    thread.scrollTop = thread.scrollHeight;
  }

  // ---- responders --------------------------------------------------------------------------

  async function responderAction(promise, success) {
    map.closePopup();
    try {
      await promise;
      if (success) toast(success);
      syncNow(true);
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        state.session.responder = null;
        renderResponder();
      }
      toast(errorText(error));
    }
  }

  const closeRoad = (roadId, reason) => responderAction(api("/api/closures", { body: { road: roadId, reason }, csrf: true }), t("toast.blocked", { street: street(state.roads.get(roadId).name) }));
  const reopenRoad = (roadId) => responderAction(api(`/api/closures/${encodeURIComponent(roadId)}`, { method: "DELETE", csrf: true }), t("toast.reopened", { street: street(state.roads.get(roadId).name) }));
  const updateShelter = (id, body) => responderAction(api(`/api/shelters/${encodeURIComponent(id)}`, { body, csrf: true }), t("toast.saved", { shelter: state.shelters.get(id).name }));
  const handleReport = (id, action) => responderAction(api(`/api/reports/${id}/${action}`, { body: {}, csrf: true }));

  function reportActions(report) {
    return el(
      "div",
      { class: "popup-actions" },
      el("button", { type: "button", class: "is-ok", text: t(report.kind === "rescue" ? "resp.verify_rescue" : "resp.verify"), onclick: () => handleReport(report.id, "verify") }),
      el("button", { type: "button", text: t("resp.dismiss"), onclick: () => handleReport(report.id, "dismiss") }),
    );
  }

  function renderResponder() {
    const signedIn = Boolean(state.session.responder);
    $("#login-form").hidden = signedIn;
    $("#responder-tools").hidden = !signedIn;
    $("#responder-name").textContent = signedIn ? t("resp.signed", { name: state.session.responder }) : "";
    renderClosureList();
    renderReportList();
  }

  function renderReportList() {
    const list = $("#report-list");
    list.replaceChildren();
    $("#report-count").textContent = String(state.live.reports.length);
    if (!state.live.reports.length) {
      list.append(el("li", { class: "list-empty", text: t("resp.no_reports") }));
      return;
    }
    for (const report of state.live.reports) {
      list.append(
        el(
          "li",
          { class: report.kind === "rescue" ? "is-rescue" : null },
          el(
            "span",
            { class: "grow" },
            el("span", { class: "title", text: `#${report.id} · ${t(`kind.${report.kind}`)} · ${report.place}` }),
            el("span", { class: "sub", text: [fmtTime(report.at), report.source, report.note].filter(Boolean).join(" · ") }),
            reportActions(report),
          ),
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("shelter.show"), onclick: () => map.setView([report.lat, report.lon], 18) }),
        ),
      );
    }
  }

  function renderClosureList() {
    const list = $("#closure-list");
    list.replaceChildren();
    $("#closure-count").textContent = String(state.live.closures.size);
    if (!state.live.closures.size) {
      list.append(el("li", { class: "list-empty", text: t("resp.no_blocked") }));
      return;
    }
    for (const closure of state.live.closures.values()) {
      const road = state.roads.get(closure.road);
      list.append(
        el(
          "li",
          {},
          el("span", { class: "grow" }, el("span", { class: "title", text: closure.name, title: closure.name }), el("span", { class: "sub", text: `${closure.reason} · ${closure.reported_by} · ${fmtTime(closure.at)}` })),
          closure.source.startsWith("sensor:") ? el("span", { class: "pill pill-sensor", text: t("resp.sensor") }) : null,
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("shelter.show"), onclick: () => map.setView(middleOf(road.geometry), 18) }),
          el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("resp.reopen"), onclick: () => reopenRoad(closure.road) }),
        ),
      );
    }
  }

  // ---- planners ----------------------------------------------------------------------------

  function toggleScenarioRoad(roadId) {
    if (state.live.closures.has(roadId)) return toast(t("plan.already"));
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
      state.plan.result = await api("/api/impact", { body: { mode: state.plan.mode, closures: [...state.plan.roads], shelters_closed: [...state.plan.shelters] } });
    } catch (error) {
      toast(errorText(error));
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
      list.append(el("li", { class: "list-empty", text: t("plan.empty") }));
      return;
    }
    for (const id of state.plan.roads) {
      const road = state.roads.get(id);
      list.append(el("li", {}, el("span", { class: "grow" }, el("span", { class: "title", text: Street(road.name) }), el("span", { class: "sub", text: t("plan.road", { len: fmtDist(road.length_m) }) })), el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("plan.remove"), onclick: () => toggleScenarioRoad(id) })));
    }
    for (const id of state.plan.shelters) {
      list.append(el("li", {}, el("span", { class: "grow" }, el("span", { class: "title", text: state.shelters.get(id).name }), el("span", { class: "sub", text: t("plan.shelter_closed") })), el("button", { type: "button", class: "btn btn-quiet btn-small", text: t("plan.remove"), onclick: () => toggleScenarioShelter(id) })));
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
      headline = t("impact.live", { reach: before.reachable, total: before.junctions });
      calm = before.cut_off === 0;
    } else if (result.newly_cut_off > 0) headline = t("impact.cut", { n: result.newly_cut_off });
    else if (result.slower_by_a_minute_or_more > 0) headline = t("impact.slower", { n: result.slower_by_a_minute_or_more });
    else {
      headline = t("impact.calm");
      calm = true;
    }
    const row = (label, a, b, worse) => el("tr", {}, el("td", { text: label }), el("td", { text: a }), el("td", { class: worse ? "worse" : null, text: b }));
    box.append(
      el(
        "div",
        { class: "impact" },
        el("div", { class: `impact-headline${calm ? " is-calm" : ""}`, text: headline }),
        el(
          "table",
          {},
          el("thead", {}, el("tr", {}, el("th", { text: t(result.mode === "foot" ? "mode.foot" : "mode.vehicle") }), el("th", { text: t("impact.col_live") }), el("th", { text: t("impact.col_scn") }))),
          el(
            "tbody",
            {},
            row(t("impact.reach"), `${before.reachable}`, `${after.reachable}`, after.reachable < before.reachable),
            row(t("impact.cutoff"), `${before.cut_off} (${before.cut_off_pct}%)`, `${after.cut_off} (${after.cut_off_pct}%)`, after.cut_off > before.cut_off),
            row(t("impact.avg"), fmtEta(before.mean_eta_s), fmtEta(after.mean_eta_s), (after.mean_eta_s || 0) - (before.mean_eta_s || 0) >= 30),
            row(t("impact.p90"), fmtEta(before.p90_eta_s), fmtEta(after.p90_eta_s), (after.p90_eta_s || 0) - (before.p90_eta_s || 0) >= 30),
            row(t("impact.slowest"), fmtEta(before.worst_eta_s), fmtEta(after.worst_eta_s), (after.worst_eta_s || 0) - (before.worst_eta_s || 0) >= 30),
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
    if (!ok) $("#live-text").textContent = t("live.offline");
    else renderSummary();
  }

  function renderSummary() {
    if (state.live.version == null) return;
    const total = state.shelters.size;
    const open = [...state.shelters.keys()].filter((id) => shelterLive(id).status === "open").length;
    $("#live-text").textContent = t("live.summary", { blocked: state.live.closures.size, open, total });
  }

  function renderWeather() {
    const chip = $("#weather");
    const weather = state.live.weather;
    if (!weather) {
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    chip.className = `weather is-${weather.level || "none"}`;
    let text;
    if (weather.level) text = t(`weather.${weather.level}`, { mm: Math.round(weather.next3h_mm) });
    else text = t(weather.source === "loading" ? "weather.loading" : "weather.unavailable");
    if (weather.source === "drill") text += ` · ${t("weather.drill")}`;
    chip.textContent = text;
  }

  function renderStory() {
    const box = $("#story");
    const story = state.live.story;
    if (!story || state.storyHiddenAt === story.step) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    $("#story-label").textContent = t("story.label", { step: story.step, total: story.total });
    $("#story-text").textContent = story.text[state.lang] || story.text.en;
    $("#story-bar").style.width = `${Math.round((100 * story.step) / story.total)}%`;
  }

  function describe(event) {
    const vars = { actor: event.actor, detail: event.detail };
    switch (event.action) {
      case "closed":
        return t("ev.closed", vars);
      case "reopened":
        return t("ev.reopened", vars);
      case "water level":
        return t("ev.water", vars);
      case "reset":
        return t("ev.reset", vars);
      case "occupancy":
        return t("ev.occupancy", vars);
      case "report":
        return t("ev.report", vars);
      case "report verified":
        return t("ev.verified", vars);
      case "report dismissed":
        return t("ev.dismissed", vars);
      case "weather":
        return t("ev.weather", vars);
      default:
        if (event.action.startsWith("shelter ")) return t("ev.shelter", { ...vars, status: t(`status.${event.action.slice(8)}`) });
        return `${event.actor}: ${event.action} ${event.detail}`;
    }
  }

  function renderEvents() {
    const list = $("#events");
    list.replaceChildren();
    if (!state.live.events.length) {
      list.append(el("li", {}, el("time", { text: "" }), el("span", { text: t("activity.none") })));
      return;
    }
    for (const event of state.live.events) list.append(el("li", {}, el("time", { datetime: event.at, text: fmtTime(event.at) }), el("span", { text: describe(event) })));
  }

  function renderEverything() {
    if (!state.area) return;
    renderSummary();
    renderWeather();
    renderStory();
    renderEvents();
    renderRouteResult();
    renderShelterList();
    renderResponder();
    renderScenario();
    renderImpact();
  }

  function applyState(data) {
    if (state.live.version != null && data.version < state.live.version) return;
    const previous = state.live;
    const closures = new Map(data.closures.map((c) => [c.road, c]));
    const newlyClosed = [...closures.keys()].filter((id) => !previous.closures.has(id));
    const shelterChanges = {};
    for (const [id, info] of Object.entries(data.shelters)) {
      if ((previous.shelters[id]?.status || "open") !== info.status) shelterChanges[id] = info.status;
    }
    const first = previous.version == null;
    state.live = { version: data.version, closures, shelters: data.shelters, sensors: data.sensors, events: data.events, risk: data.risk || {}, reports: data.reports || [], weather: data.weather, story: data.story, at: new Date().toISOString() };

    for (const id of closures.keys()) state.plan.roads.delete(id);
    restyleRoads();
    refreshShelterMarkers();
    refreshSensorMarkers();
    drawReports();
    renderSummary();
    renderWeather();
    renderStory();
    renderEvents();
    renderResponder();
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
  async function syncNow(full = false) {
    if (polling) return;
    polling = true;
    let data;
    try {
      const since = full || state.live.version == null ? "" : `?since=${state.live.version}`;
      data = await api(`/api/state${since}`);
      setLive(true);
    } catch {
      setLive(false);
      polling = false;
      return;
    }
    try {
      if (data) {
        if (full) state.live.version = null; // a signed-in responder sees more: take the whole picture
        applyState(data);
      }
    } catch (error) {
      console.error("Couldn't apply the latest state", error);
    } finally {
      polling = false;
    }
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

  function wireGroup(group, onChange) {
    const buttons = $$(`[data-group="${group}"] [role=radio]`);
    for (const button of buttons) {
      button.addEventListener("click", () => {
        for (const other of buttons) other.setAttribute("aria-checked", String(other === button));
        onChange(button.dataset.value);
      });
    }
  }

  function wireControls() {
    $("#lang").addEventListener("change", (event) => setLanguage(event.target.value));
    wireGroup("mode", (mode) => {
      state.route.mode = mode;
      hideBanner();
      requestRoute();
    });
    wireGroup("prefer", (prefer) => {
      state.route.prefer = prefer;
      hideBanner();
      requestRoute();
    });
    wireGroup("plan-mode", (mode) => {
      state.plan.mode = mode;
      requestImpact();
    });

    $("#locate").addEventListener("click", () => {
      if (!navigator.geolocation) return toast(t("route.empty_text"));
      navigator.geolocation.getCurrentPosition(
        (position) => setStart(L.latLng(position.coords.latitude, position.coords.longitude)),
        () => toast(t("route.empty_text")),
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

    for (const button of $$("[data-kind]")) button.addEventListener("click", () => sendReport(button.dataset.kind));

    $("#sms-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("#sms-text");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      sendSms(text);
    });

    $("#login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = $("#login-error");
      error.hidden = true;
      try {
        const data = await api("/api/responder/login", { body: { name: $("#login-name").value, passcode: $("#login-passcode").value } });
        state.session.responder = data.responder;
        state.session.csrf = data.csrf;
        $("#login-passcode").value = "";
        renderResponder();
        toast(t("toast.signed_in", { name: data.responder }));
        syncNow(true);
      } catch (err) {
        error.textContent = errorText(err);
        error.hidden = false;
      }
    });

    $("#logout").addEventListener("click", async () => {
      const data = await api("/api/responder/logout", { body: {} }).catch(() => null);
      state.session.responder = null;
      if (data) state.session.csrf = data.csrf;
      renderResponder();
      syncNow(true);
    });

    $("#reset").addEventListener("click", () => {
      if (!window.confirm(t("resp.reset_confirm"))) return;
      responderAction(api("/api/reset", { body: {}, csrf: true }), t("toast.reset"));
    });
    $("#story-start").addEventListener("click", () => {
      state.storyHiddenAt = null;
      responderAction(api("/api/story/start", { body: {}, csrf: true }), t("toast.story"));
    });
    $("#story-stop").addEventListener("click", () => responderAction(api("/api/story/stop", { body: {}, csrf: true })));
    $("#story-hide").addEventListener("click", () => {
      state.storyHiddenAt = state.live.story?.step ?? null;
      renderStory();
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
      route: cssVar("--route"),
      closed: cssVar("--closed"),
      scenario: cssVar("--scenario"),
      risky: cssVar("--risky"),
      s1: cssVar("--s1"),
      s2: cssVar("--s2"),
      s3: cssVar("--s3"),
      s4: cssVar("--s4"),
      s5: cssVar("--s5"),
    };
    state.lang = pickLanguage();
    applyStaticText();
    wireTabs();
    wireControls();
    try {
      state.area = await api("/api/area");
    } catch {
      $("#live-text").textContent = t("route.offline_fail");
      return;
    }
    for (const road of state.area.roads) state.roads.set(road.id, road);
    for (const [id, lat, lon] of state.area.nodes) state.nodes.set(id, [lat, lon]);
    for (const shelter of state.area.shelters) state.shelters.set(shelter.id, shelter);
    for (const sensor of state.area.sensors) state.sensors.set(sensor.id, sensor);
    // Shelters need their junction for offline routing: the nearest node to each.
    for (const shelter of state.shelters.values()) {
      let best = null;
      let bestD = Infinity;
      for (const [id, coords] of state.nodes) {
        const d = distanceM([shelter.lat, shelter.lon], coords);
        if (d < bestD) {
          bestD = d;
          best = id;
        }
      }
      shelter.node = shelter.node ?? best;
    }
    buildAdjacency();

    initMap();
    fillStreets();
    renderEverything();
    api("/api/session")
      .then((session) => {
        state.session = session;
        renderResponder();
        if (session.responder) syncNow(true);
      })
      .catch(() => {});
    syncNow();
    setInterval(syncNow, POLL_MS);
  }

  init();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch((error) => console.warn("Offline support unavailable", error));
  }
})();
