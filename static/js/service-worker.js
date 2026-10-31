const CACHE_PREFIX = "budget-tracker-shell-";
const CACHE_NAME = "budget-tracker-shell-v11";
const REQUIRED_PRECACHE_URLS = [
    "/static/vendor/bootstrap/bootstrap.min.css",
    "/static/vendor/bootstrap/bootstrap.bundle.min.js",
    "/static/css/app.css",
    "/static/css/app.css?v=v11",
    "/static/css/dashboard.css",
    "/static/css/dashboard.css?v=v11",
    "/static/js/app.js",
    "/static/js/app.js?v=v11",
    "/static/js/offline.js",
    "/static/js/offline.js?v=v11",
    "/static/js/pages/transaction.js",
    "/static/js/pages/transaction.js?v=v11",
    "/static/manifest.webmanifest",
];
const PRECACHE_URLS = REQUIRED_PRECACHE_URLS;
const OPTIONAL_PRECACHE_URLS = [];

self.addEventListener("install", function (event) {
    event.waitUntil(
        caches
            .open(CACHE_NAME)
            .then(function (cache) {
                var offlineReq = new Request("/offline/", {
                    method: "GET",
                    credentials: "omit",
                    cache: "reload",
                });

                var requiredFetches = [
                    fetch(offlineReq).then(function (resp) {
                        if (!resp || resp.status !== 200) {
                            var status = resp ? resp.status : "network_error";
                            throw new Error("Failed to cache required offline page: HTTP " + status);
                        }
                        return cache.put("/offline/", resp);
                    }),
                ];

                REQUIRED_PRECACHE_URLS.forEach(function (url) {
                    requiredFetches.push(
                        fetch(new Request(url, { cache: "reload" })).then(function (resp) {
                            if (!resp || resp.status !== 200) {
                                var status = resp ? resp.status : "network_error";
                                throw new Error("Failed to cache required asset " + url + ": HTTP " + status);
                            }
                            return cache.put(url, resp);
                        })
                    );
                });

                var optionalFetches = OPTIONAL_PRECACHE_URLS.map(function (url) {
                    return fetch(new Request(url, { cache: "reload" }))
                        .then(function (resp) {
                            if (resp && resp.status === 200) {
                                return cache.put(url, resp);
                            }
                        })
                        .catch(function () {
                            // Optional assets may fail without rejecting installation
                        });
                });

                return Promise.all(requiredFetches).then(function () {
                    return Promise.all(optionalFetches);
                });
            })
            .then(function () {
                return self.skipWaiting();
            })
            .catch(function (error) {
                return caches.delete(CACHE_NAME).catch(function () {}).then(function () {
                    throw error;
                });
            })
    );
});

self.addEventListener("activate", function (event) {
    event.waitUntil(
        caches
            .keys()
            .then(function (keys) {
                return Promise.all(
                    keys
                        .filter(function (k) {
                            return k.indexOf(CACHE_PREFIX) === 0 && k !== CACHE_NAME;
                        })
                        .map(function (k) {
                            return caches.delete(k);
                        })
                );
            })
            .then(function () {
                return self.clients.claim();
            })
    );
});

self.addEventListener("fetch", function (event) {
    var request = event.request;
    if (request.method !== "GET") {
        return;
    }

    var url = new URL(request.url);
    if (url.origin !== self.location.origin) {
        return;
    }

    if (url.pathname.startsWith("/api/")) {
        return;
    }

    if (url.pathname.startsWith("/static/")) {
        event.respondWith(
            caches.match(request).then(function (cached) {
                return (
                    cached ||
                    fetch(request).then(function (networkResp) {
                        if (networkResp && networkResp.status === 200) {
                            var copy = networkResp.clone();
                            caches.open(CACHE_NAME).then(function (cache) {
                                cache.put(request, copy);
                            });
                        }
                        return networkResp;
                    })
                );
            })
        );
        return;
    }

    if (request.mode === "navigate") {
        event.respondWith(
            fetch(request).catch(function () {
                return caches.match("/offline/");
            })
        );
    }
});
