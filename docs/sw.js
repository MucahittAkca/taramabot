// Önbellek: uygulama dosyaları ağ öncelikli (güncellemeler hemen gelir, çevrimdışıysa önbellekten açılır),
// fontlar önbellek öncelikli (çevrimdışı da doğru font çizilir).
const CACHE = "sr-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (/(^|\.)(googleapis|gstatic)\.com$/.test(url.hostname)) {
    e.respondWith(
      caches.open(CACHE).then(async (c) => {
        const hit = await c.match(req);
        const net = fetch(req)
          .then((r) => { if (r.ok || r.type === "opaque") c.put(req, r.clone()); return r; })
          .catch(() => hit);
        return hit || net;
      })
    );
    return;
  }
  if (url.origin === location.origin) {
    e.respondWith(
      fetch(req)
        .then((r) => { const cp = r.clone(); caches.open(CACHE).then((c) => c.put(req, cp)); return r; })
        .catch(() => caches.match(req).then((m) => m || caches.match("index.html")))
    );
  }
});
