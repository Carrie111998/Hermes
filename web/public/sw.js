// Hermes intentionally does not cache dashboard HTML or API responses: the
// HTML can contain a short-lived session token and API payloads may be private.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
