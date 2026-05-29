// ═══════════════════════════════════════════════════════════════════
// AirQuali Service Worker v3 — Full Offline PWA
// Strategy:
//   • API routes  → Network-first, fall back to cached JSON stub
//   • Static shell → Cache-first after first load (cache-then-network update)
//   • CDN assets  → Cache-first (fonts, Chart.js) — long-lived
// ═══════════════════════════════════════════════════════════════════

const CACHE_VERSION  = 'airquali-v3';
const CDN_CACHE      = 'airquali-cdn-v3';
const OFFLINE_API    = '{}';

// ── Assets to pre-cache on install ───────────────────────────────────
const PRECACHE_SHELL = [
  '/',
  '/analytics',
  '/manifest.json',
  '/icon.svg',
  '/sw.js',
];

// CDN resources to cache on first fetch (not pre-cached to avoid install failures)
const CDN_ORIGINS = [
  'cdn.jsdelivr.net',
  'fonts.googleapis.com',
  'fonts.gstatic.com',
];

// API routes — always network-first, never block offline mode
const API_ROUTES = [
  '/data', '/latest', '/history', '/predict', '/anomalies',
  '/alerts', '/devices', '/analytics-data', '/export', '/clear',
  '/reports', '/generate-report', '/public-url',
];

// ── Install: pre-cache the app shell ─────────────────────────────────
self.addEventListener('install', event => {
  self.skipWaiting();   // activate immediately, don't wait for old SW to die
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => {
      // Use individual requests so one 404 doesn't fail the whole install
      return Promise.allSettled(
        PRECACHE_SHELL.map(url =>
          cache.add(new Request(url, { cache: 'reload' })).catch(err => {
            console.warn(`[SW] Pre-cache skipped: ${url}`, err.message);
          })
        )
      );
    })
  );
});

// ── Activate: purge stale caches ─────────────────────────────────────
self.addEventListener('activate', event => {
  const KEEP = new Set([CACHE_VERSION, CDN_CACHE]);
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => !KEEP.has(k)).map(k => {
          console.log(`[SW] Purging old cache: ${k}`);
          return caches.delete(k);
        })
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: routing logic ──────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle GET (POST for /data goes straight to network)
  if (req.method !== 'GET') return;

  // ① API routes — network-first, graceful offline fallback
  const isApiRoute = API_ROUTES.some(r => url.pathname === r || url.pathname.startsWith(r + '/') || url.pathname.startsWith(r + '?'));
  if (isApiRoute) {
    event.respondWith(networkFirstApi(req));
    return;
  }

  // ② CDN resources — cache-first (they don't change)
  const isCDN = CDN_ORIGINS.some(o => url.hostname.includes(o));
  if (isCDN) {
    event.respondWith(cdnCacheFirst(req));
    return;
  }

  // ③ App shell — stale-while-revalidate
  if (url.origin === self.location.origin) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }
});

// ── Strategy: network-first for API, return empty stub if offline ─────
async function networkFirstApi(req) {
  try {
    const resp = await fetch(req, { cache: 'no-store' });
    // Cache successful API responses for offline display
    if (resp.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(req, resp.clone());
    }
    return resp;
  } catch {
    // Offline: return last cached API response, or empty stub
    const cached = await caches.match(req);
    return cached || new Response(OFFLINE_API, {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'X-Served-By': 'ServiceWorker-Offline' }
    });
  }
}

// ── Strategy: cache-first for CDN assets ─────────────────────────────
async function cdnCacheFirst(req) {
  const cached = await caches.match(req, { cacheName: CDN_CACHE });
  if (cached) return cached;
  try {
    const resp = await fetch(req);
    if (resp.ok && resp.type !== 'opaque') {
      const cache = await caches.open(CDN_CACHE);
      cache.put(req, resp.clone());
    }
    return resp;
  } catch {
    return new Response('/* offline */', {
      status: 503,
      headers: { 'Content-Type': 'text/javascript' }
    });
  }
}

// ── Strategy: stale-while-revalidate for app shell ───────────────────
async function staleWhileRevalidate(req) {
  const cache  = await caches.open(CACHE_VERSION);
  const cached = await cache.match(req);

  // Always kick off a background network fetch to keep cache fresh
  const fetchPromise = fetch(req).then(resp => {
    if (resp.ok) cache.put(req, resp.clone());
    return resp;
  }).catch(() => null);

  // Return cached version immediately if available, otherwise await network
  return cached || (await fetchPromise) || offlinePage();
}

// ── Fallback offline page ─────────────────────────────────────────────
function offlinePage() {
  return new Response(`
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>AirQuali — Offline</title>
      <style>
        body {
          margin: 0; min-height: 100vh; display: flex; flex-direction: column;
          align-items: center; justify-content: center;
          background: #060b14; color: #f1f5f9;
          font-family: system-ui, sans-serif; text-align: center; padding: 24px;
        }
        .icon { font-size: 72px; margin-bottom: 24px; animation: pulse 2s infinite; }
        h1 { font-size: 1.5rem; margin: 0 0 12px; }
        p  { color: #94a3b8; max-width: 300px; line-height: 1.6; }
        button {
          margin-top: 24px; padding: 12px 28px; border-radius: 100px;
          background: #6366f1; color: #fff; border: none;
          font-size: 14px; cursor: pointer;
        }
        @keyframes pulse {
          0%,100% { transform: scale(1); }
          50%      { transform: scale(1.1); }
        }
      </style>
    </head>
    <body>
      <div class="icon">📡</div>
      <h1>You're Offline</h1>
      <p>AirQuali can't reach the sensor server right now. Last cached data may still be visible in the app.</p>
      <button onclick="location.reload()">Try Again</button>
    </body>
    </html>
  `, {
    status: 503,
    headers: { 'Content-Type': 'text/html; charset=utf-8' }
  });
}

// ── Message channel: allow pages to trigger skipWaiting ──────────────
self.addEventListener('message', event => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
