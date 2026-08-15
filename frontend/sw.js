// Minimal service worker: caches the static app shell only, purely so the
// browser considers this installable (Add to Home Screen). Every API call
// is dynamic/authenticated and must never be cached, so anything outside
// SHELL_FILES always goes straight to the network, untouched.
const CACHE_NAME = "mass-unfollow-shell-v1";
const SHELL_FILES = ["/", "/style.css", "/app.js", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)));
  // Deliberately no skipWaiting() here. A newly installed worker sits in the
  // "waiting" state and never takes over open tabs on its own until the page
  // unregisters it — which only happens when the user clicks Reload on the
  // update banner (see app.js). Without this, a new worker could silently
  // start controlling every open tab the instant it finishes installing,
  // before anyone has agreed to update.
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

// Push notifications — currently just the unfollow worker auto-pausing
// because Instagram signaled a rate limit (see backend/app/worker.py).
self.addEventListener("push", (event) => {
  let data = { title: "Mass Unfollow", body: "" };
  try {
    data = event.data.json();
  } catch (_) {
    // Malformed/empty payload — fall back to the default above rather than
    // dropping the notification silently.
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Mass Unfollow", {
      body: data.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-32.png",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
  );
});
