const CACHE_NAME = 'amar-voice-v5';
const SHELL_FILES = [
  '/',
  '/manifest.json',
  '/icon.png',
  '/icon-192.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_FILES).catch(err => console.log('Cache prefill notice:', err));
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
    )).then(() => clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  // Preset audio is cached per full URL (speaker included) — never ignore query string,
  // otherwise ?speaker=sp could return the cached default voice.
  if (e.request.url.includes('/api/presets/') || e.request.mode === 'navigate') {
    e.respondWith(
      caches.match(e.request).then((res) => {
        return res || fetch(e.request).then((fetchRes) => {
          if (fetchRes.status === 200) {
            const clone = fetchRes.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
          }
          return fetchRes;
        }).catch(() => caches.match('/'));
      })
    );
  } else {
    e.respondWith(fetch(e.request));
  }
});
