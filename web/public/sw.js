/**
 * sw.js — ClearNode 极速商城 PWA Service Worker (Phase 9 SPEC §5)
 *
 * 职责:
 *   1. 硬缓存支付成功后的加密 JWT 提货码 (CACHE_PICKUP_TICKET 消息)
 *   2. 断网探测: 离线时仍可读取提货码渲染 (微仓扫码枪反向读取)
 *   3. 静态资源预缓存 (轻量 shell, 不缓存 API 动态响应)
 */

const PICKUP_CACHE = 'hemall-pickup-tickets-v1';
const SHELL_CACHE = 'hemall-shell-v1';

const SHELL_ASSETS = ['/', '/shop/feed', '/shop/express-checkout'];

// ── 安装: 预缓存应用壳 ──────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS).catch(() => null))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== PICKUP_CACHE && k !== SHELL_CACHE)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// ── 提货码硬缓存 (来自页面 postMessage) ─────────────────────────────
self.addEventListener('message', (event) => {
  const data = event.data;
  if (!data || data.type !== 'CACHE_PICKUP_TICKET' || !data.payload?.pickup_code) return;

  caches.open(PICKUP_CACHE).then((cache) => {
    // 存成独立 key: pickup:<order_id>
    const key = `pickup:${data.payload.order_id}`;
    cache.put(
      key,
      new Response(JSON.stringify(data.payload), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    // 同时写入"最新一张"槽位，OfflineTicketView 直接读它
    cache.put(
      'pickup:latest',
      new Response(JSON.stringify(data.payload), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
});

// ── 离线回退 ────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 只处理同源请求
  if (url.origin !== self.location.origin) return;

  // 离线凭证读取: 提货码查询由 OfflineTicketView 直接从 Cache API 读,
  // 这里仅做导航请求的离线壳回退。
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match(event.request).then((cached) => cached || caches.match('/shop/feed')),
      ),
    );
  }
});
