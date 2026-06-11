// 간단한 서비스 워커:
// - 앱 셸(index)은 캐시 우선
// - 데이터(JSON)는 네트워크 우선, 실패 시 마지막 캐시로 폴백(오프라인 대비)
const CACHE = "exhibitions-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const isData = url.pathname.endsWith("exhibitions.json");

  if (isData) {
    // 네트워크 우선
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // 그 외: 캐시 우선
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
