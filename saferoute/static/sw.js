"use strict";

/*
 * SafeRoute service worker: lets the app install ("Open as app") and keeps working when the
 * connection drops.
 *
 * - The page, scripts, styles, icons and road data are cached, so SafeRoute opens offline.
 * - The app's code, pages and live state are network-first: online you always get the latest;
 *   offline you get the last copy this device received. Libraries and icons are cache-first.
 * - Changes (POST/DELETE), routing requests and map tiles are never cached.
 */
const CACHE = "saferoute-v2";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/app.css",
  "/static/i18n.js",
  "/static/icon.svg",
  "/static/vendor/leaflet/leaflet.js",
  "/static/vendor/leaflet/leaflet.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.webmanifest",
  "/api/area",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && response.status === 200) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request, { ignoreSearch: request.url.includes("/api/state") });
    if (cached) return cached;
    throw error;
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const refresh = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || refresh;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // Libraries and icons rarely change: serve from cache, refresh in the background.
  if (url.pathname.startsWith("/static/vendor/") || url.pathname.startsWith("/static/icons/")) {
    event.respondWith(staleWhileRevalidate(request));
  } else if (
    // The app's own code, the page and live data: always fresh online, cached copy offline, so a
    // new version of the server is never paired with old code.
    url.pathname.startsWith("/static/") ||
    url.pathname === "/manifest.webmanifest" ||
    request.mode === "navigate" ||
    url.pathname === "/api/area" ||
    url.pathname === "/api/state"
  ) {
    event.respondWith(networkFirst(request));
  }
});
