{% load static %}/* AttendEase service worker.
 *
 * Served from the site root (see core/urls.py) so its scope covers the whole
 * app. Bump CACHE_VERSION whenever the precached assets change — the old cache
 * is dropped on activate.
 *
 * Deliberately conservative: HTML is never cached (pages are per-user and carry
 * CSRF tokens), only same-origin GETs for static assets are stored.
 */

var CACHE_VERSION = "attendease-v1";
var OFFLINE_URL = "/offline/";

var PRECACHE = [
  OFFLINE_URL,
  "{% static 'css/style.css' %}",
  "{% static 'js/app.js' %}",
  "{% static 'js/pwa.js' %}",
  "{% static 'js/attendance.js' %}",
  "{% static 'img/icon-192.png' %}",
  "{% static 'img/icon-512.png' %}",
  "{% static 'img/apple-touch-icon.png' %}"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(function (cache) {
      // addAll() is all-or-nothing; add individually so one 404 during
      // development does not abort the whole install.
      return Promise.all(
        PRECACHE.map(function (url) {
          return cache.add(new Request(url, { cache: "reload" })).catch(function () {});
        })
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (key) {
          return key === CACHE_VERSION ? null : caches.delete(key);
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

/** Static assets: serve from cache, refresh in the background. */
function staleWhileRevalidate(request) {
  return caches.open(CACHE_VERSION).then(function (cache) {
    return cache.match(request).then(function (cached) {
      var network = fetch(request)
        .then(function (response) {
          if (response && response.ok) {
            cache.put(request, response.clone());
          }
          return response;
        })
        .catch(function () {
          return cached;
        });
      return cached || network;
    });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;

  if (request.method !== "GET") {
    return;
  }

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // Page loads: always hit the network, fall back to the offline notice.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(OFFLINE_URL).then(function (cached) {
          return (
            cached ||
            new Response("You are offline.", {
              status: 503,
              headers: { "Content-Type": "text/plain" }
            })
          );
        });
      })
    );
    return;
  }

  if (url.pathname.indexOf("{% static '' %}") === 0) {
    event.respondWith(staleWhileRevalidate(request));
  }
});
