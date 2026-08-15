// Minimal service worker: caches the static app shell only, purely so the
// browser considers this installable (Add to Home Screen). Every API call
// is dynamic/authenticated and must never be cached, so anything outside
// SHELL_FILES always goes straight to the network, untouched.
const CACHE_NAME = "mass-unfollow-shell-v1";
const SHELL_FILES = ["/", "/style.css", "/app.js", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || !SHELL_FILES.includes(url.pathname)) {
    return; // let the browser handle it normally (in particular: never touch /api/*)
  }
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((res) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, res.clone()));
          return res;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
