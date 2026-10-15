// Service worker d'Escapade (servi à la racine : il contrôle tout le site).
// - Interface : réseau d'abord (toujours la dernière version en ligne), cache en secours hors ligne.
// - Polices Google : cache d'abord (elles ne changent pas), mises en cache au premier usage.
// - API : jamais interceptée ni mise en cache (données d'un voyage, cookies).
// Changer VERSION purge les anciens caches à l'activation.

const VERSION = "escapade-v2";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/manifest.webmanifest",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
];
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(fallbackUrl || request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(fallbackUrl || request);
    if (cached) return cached;
    throw error;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok || response.type === "opaque") cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (FONT_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(request));
    return;
  }
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  // Toutes les pages (/, /t/{code}) sont la même page : hors ligne, on sert la copie de "/".
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, "/"));
    return;
  }
  event.respondWith(networkFirst(request));
});
