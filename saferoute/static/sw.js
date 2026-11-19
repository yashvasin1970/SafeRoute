"use strict";

/*
 * SafeRoute service worker: lets the app install ("Open as app") and keeps working when the
 * connection drops.
 *
 * - The page, scripts, styles, icons and road data are cached, so SafeRoute opens offline.
 * - Pages and live state are network-first: online you always see the latest closures; offline
 *   you see the last state this device received.
 * - Changes (POST/DELETE), routing requests and map tiles are never cached.
 */
const CACHE = "saferoute-v1";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/app.css",
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

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(staleWhileRevalidate(request));
  } else if (request.mode === "navigate" || url.pathname === "/api/area" || url.pathname === "/api/state") {
    event.respondWith(networkFirst(request));
  }
});
