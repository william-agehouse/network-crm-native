/**
 * sw.js — Network CRM service worker
 *
 * Strategy:
 *   - App shell (HTML, CSS, JS, fonts) → network-first with fallback to cache
 *   - API calls → network-only (never cache live data)
 *   - Everything else → network-first with fallback to cache
 *
 * The shell used to be cache-first, which was wrong for the desktop app: it
 * is served from 127.0.0.1, where the network is never the slow path, so
 * cache-first bought no speed and cost a whole launch of correctness — a
 * rebuilt /app/*.js only appeared on the *next* start. A service worker sits
 * below WKWebView's HTTP cache, so the native window's
 * .reloadIgnoringLocalCacheData could not reach past it either. The cache is
 * still filled on every successful fetch and still answers when the network
 * can't, so a copy served over the network keeps working offline.
 *
 * Install: registered in static/app/main.js
 */

// Bump on any change to the shell list or caching strategy: activate deletes
// every other "ncrmv*" cache, which is what evicts a user's stale shell.
// v3: the redesign changed every stylesheet, and the shell was being served
// from an HTTP cache that had no validator to revalidate against (fixed in
// crm/app.py). Existing installs need their precached shell evicted once to
// pick that up, and bumping this is what does it.
const CACHE_VERSION = "ncrmv3";
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const RUNTIME_CACHE = `${CACHE_VERSION}-runtime`;

// Files that form the app shell — precached on install.
//
// This list is hand-kept, not generated: an install-time fetch of "/" to derive
// it would make SW install itself depend on the network succeeding, which is
// the one thing this file's whole strategy (network-first, cache is only the
// fallback) should never require. In exchange, tests/test_service_worker.py
// walks static/index.html's own <link>/<script> tags and the real
// `import ... from "./x.js"` graph starting at app/main.js, and fails if this
// array and that graph ever disagree — in either direction: an omission here
// (silently uncached, only visible offline or on a cold start) or a stale
// entry (rejects the WHOLE cache.addAll(), which fails SW install entirely).
// Keep this list and that test in the same change, always.
//
// static/vendor/preact-htm.module.js is reachable from every module above but
// is deliberately NOT listed: isShell() below only precaches "/", "/css/*"
// and "/app/*", so vendor (and fonts) fall to RUNTIME_CACHE's network-first
// fallback instead, backed by their own long-lived HTTP Cache-Control.
const SHELL_ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/css/tokens.css",
  "/css/ui.css",
  "/css/view-ask.css",
  "/css/view-network.css",
  "/css/view-data.css",
  "/css/view-settings.css",
  "/css/view-setup.css",
  "/css/view-intake.css",
  "/css/view-ledger.css",
  "/css/boot.css",
  "/app/main.js",
  "/app/boot.js",
  "/app/ask.js",
  "/app/people.js",
  "/app/settings.js",
  "/app/onboarding.js",
  "/app/lists.js",
  "/app/data.js",
  "/app/ledger.js",
  "/app/intake.js",
  "/app/ui.js",
  "/app/api.js",
  "/app/icons.js",
  "/app/select.js",
  "/app/windowing.js",
  "/app/menu.js",
  "/app/dnd.js",
  "/app/fda.js",
  "/app/gmail.js",
];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith("ncrmv") && k !== SHELL_CACHE && k !== RUNTIME_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept API calls or non-GET requests
  if (request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return;
  }

  // App shell: network-first, so an edited asset lands on this load
  const isShell =
    url.pathname === "/" ||
    url.pathname.startsWith("/css/") ||
    url.pathname.startsWith("/app/") ||
    url.pathname === "/manifest.webmanifest";

  if (isShell) {
    event.respondWith(networkFirstForShell(request, SHELL_CACHE));
    return;
  }

  // Everything else (fonts, vendor, photos): network-first, cache fallback
  event.respondWith(networkFirstWithFallback(request, RUNTIME_CACHE));
});

// Separate from networkFirstWithFallback because the shell also falls back on a
// non-ok response, not just a thrown fetch: a copy served from another machine
// through a reverse proxy (README's home-server setup) gets a 502 when the origin
// is down, and a 502 must not replace a perfectly good cached app. A real 404/500 with nothing cached is still passed
// through untouched rather than masked as "Offline".
async function networkFirstForShell(request, cacheName) {
  const cache = await caches.open(cacheName);
  let response = null;
  try {
    response = await fetch(request);
  } catch {
    response = null;
  }
  if (response && response.ok) {
    // Awaited, not fire-and-forget: the worker can be torn down as soon as it
    // has answered, and a dropped put would leave the offline copy behind
    // whatever the app is actually running. On loopback the write is noise.
    await cache.put(request, response.clone());
    return response;
  }
  const cached = await cache.match(request);
  return cached || response || new Response("Offline", { status: 503 });
}

async function networkFirstWithFallback(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    const cached = await cache.match(request);
    return cached || new Response("Offline", { status: 503 });
  }
}
