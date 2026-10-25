(function () {
    "use strict";

    var DB_VERSION = 2;
    var MAX_SYNC_BATCH_SIZE = 100;
    var SYNC_LOCK_LEASE_MS = 30000;

    var STORAGE_ACTIVE_USER_ID = "budget_tracker_active_user_id";
    var STORAGE_ACTIVE_USERNAME = "budget_tracker_active_username";
    var STORAGE_AUTH_STATE = "budget_tracker_auth_state";
    var STORAGE_DB_SIGNAL = "budget_tracker_db_signal";
    var STORAGE_SYNC_LOCK_PREFIX = "budget_tracker_sync_lock_u_";
    var CHANNEL_NAME = "budget_tracker_sync_channel";

    var TAB_ID = generateUuid();
    var openConnections = {};
    var inTabSyncLocks = {};
    var inFlightMutationsByUser = {};
    var activeAbortControllers = [];

    var tabInitialDomUserId = "";
    var tabInitialDomUsername = "";
    var offlineRenderedUserId = "";
    var tabInitialized = false;
    var tabInvalidated = false;
    var tabInvalidationReason = "";
    var tabGeneration = 1;

    var syncChannel = null;
    if (typeof window !== "undefined" && "BroadcastChannel" in window) {
        try {
            syncChannel = new BroadcastChannel(CHANNEL_NAME);
        } catch (e) {
            syncChannel = null;
        }
    }

    function getBodyAttr(name) {
        var body = document.body;
        if (!body) return "";
        return (body.getAttribute(name) || "").trim();
    }

    function isOfflineShellPage() {
        return getBodyAttr("data-offline-shell") === "true";
    }

    function cloneJson(obj) {
        if (obj === undefined || obj === null) return obj;
        try {
            return JSON.parse(JSON.stringify(obj));
        } catch (e) {
            return obj;
        }
    }

    /**
     * One-time page identity initialization called only on page load (DOMContentLoaded).
     * Never called by status callbacks, storage listeners, or BroadcastChannel handlers.
     */
    function initializePageIdentity() {
        var domUserId = getBodyAttr("data-authenticated-user-id");
        var domUsername = getBodyAttr("data-authenticated-username");

        tabInitialDomUserId = domUserId;
        tabInitialDomUsername = domUsername;
        tabInitialized = true;
        tabInvalidated = false;
        tabInvalidationReason = "";

        if (domUserId) {
            try {
                var prevUserId = (localStorage.getItem(STORAGE_ACTIVE_USER_ID) || "").trim();
                localStorage.setItem(STORAGE_ACTIVE_USER_ID, domUserId);
                localStorage.setItem(STORAGE_ACTIVE_USERNAME, domUsername || "");
                localStorage.setItem(STORAGE_AUTH_STATE, "authenticated");
                if (prevUserId && prevUserId !== domUserId) {
                    closeOpenConnection(prevUserId);
                    broadcastSignal({
                        type: "account_switched",
                        previous_user_id: prevUserId,
                        user_id: domUserId,
                        username: domUsername || "",
                        source_tab_id: TAB_ID,
                    });
                }
            } catch (e) {}
            return {
                userId: domUserId,
                username: domUsername || "",
                fromServerDom: true,
                invalidated: false,
            };
        }

        var current = getActiveIdentity();
        if (isOfflineShellPage()) {
            if (current.invalidated) {
                invalidateCurrentTab(current.reason || "logged_out");
            } else if (current.userId) {
                offlineRenderedUserId = current.userId;
            }
        }
        return current;
    }

    /**
     * Pure read-only identity lookup.
     * Never writes to localStorage and never broadcasts signals, preventing
     * stale tabs from restoring an old account identity or causing broadcast loops.
     */
    function getActiveIdentity() {
        if (tabInvalidated) {
            return {
                userId: "",
                username: "",
                fromServerDom: false,
                invalidated: true,
                reason: tabInvalidationReason || "session_invalidated",
            };
        }

        var domUserId = tabInitialized
            ? tabInitialDomUserId
            : getBodyAttr("data-authenticated-user-id");
        var domUsername = tabInitialized
            ? tabInitialDomUsername
            : getBodyAttr("data-authenticated-username");

        if (domUserId) {
            try {
                var authState = (localStorage.getItem(STORAGE_AUTH_STATE) || "").trim();
                var storedId = (localStorage.getItem(STORAGE_ACTIVE_USER_ID) || "").trim();
                if (authState === "logged_out" || authState === "invalidated") {
                    return {
                        userId: "",
                        username: "",
                        fromServerDom: false,
                        invalidated: true,
                        reason: "logged_out",
                    };
                }
                if (storedId && storedId !== domUserId) {
                    return {
                        userId: "",
                        username: "",
                        fromServerDom: false,
                        invalidated: true,
                        reason: "account_switched",
                    };
                }
            } catch (e) {}
            return {
                userId: domUserId,
                username: domUsername || "",
                fromServerDom: true,
                invalidated: false,
            };
        }

        if (isOfflineShellPage()) {
            try {
                var offAuthState = (localStorage.getItem(STORAGE_AUTH_STATE) || "").trim();
                var offStoredId = (localStorage.getItem(STORAGE_ACTIVE_USER_ID) || "").trim();
                var offStoredName = (localStorage.getItem(STORAGE_ACTIVE_USERNAME) || "").trim();
                if (offAuthState === "logged_out" || offAuthState === "invalidated") {
                    return {
                        userId: "",
                        username: "",
                        fromServerDom: false,
                        invalidated: true,
                        reason: offAuthState,
                    };
                }
                if (
                    offlineRenderedUserId &&
                    offStoredId &&
                    offStoredId !== offlineRenderedUserId
                ) {
                    return {
                        userId: "",
                        username: "",
                        fromServerDom: false,
                        invalidated: true,
                        reason: "account_switched",
                    };
                }
                if (offStoredId) {
                    return {
                        userId: offStoredId,
                        username: offStoredName,
                        fromServerDom: false,
                        invalidated: false,
                    };
                }
            } catch (e) {}
        }

        return { userId: "", username: "", fromServerDom: false, invalidated: false };
    }

    function isGenerationValid(expectedGen, expectedUserId) {
        if (tabInvalidated || expectedGen !== tabGeneration) {
            return false;
        }
        if (expectedUserId) {
            var current = getActiveIdentity();
            if (current.invalidated || String(current.userId) !== String(expectedUserId)) {
                return false;
            }
        }
        return true;
    }

    function createTrackedAbortController() {
        if (typeof AbortController === "undefined") {
            return null;
        }
        var controller = new AbortController();
        activeAbortControllers.push(controller);
        return controller;
    }

    function releaseTrackedAbortController(controller) {
        if (!controller) return;
        var idx = activeAbortControllers.indexOf(controller);
        if (idx !== -1) {
            activeAbortControllers.splice(idx, 1);
        }
    }

    function abortInFlightRequests() {
        var controllers = activeAbortControllers.slice();
        activeAbortControllers = [];
        controllers.forEach(function (c) {
            try {
                c.abort();
            } catch (e) {}
        });
    }

    function getSyncLockStorageKey(userId) {
        return STORAGE_SYNC_LOCK_PREFIX + String(userId || "").trim();
    }

    function acquireCrossTabSyncLock(userId) {
        var key = String(userId || "").trim();
        if (!key) return false;
        var storageKey = getSyncLockStorageKey(key);
        var now = Date.now();
        try {
            var raw = localStorage.getItem(storageKey);
            if (raw) {
                var existing = JSON.parse(raw);
                if (
                    existing &&
                    existing.tab_id &&
                    existing.tab_id !== TAB_ID &&
                    typeof existing.expires_at === "number" &&
                    existing.expires_at > now
                ) {
                    return false;
                }
            }
            var lease = {
                tab_id: TAB_ID,
                user_id: key,
                acquired_at: now,
                expires_at: now + SYNC_LOCK_LEASE_MS,
            };
            localStorage.setItem(storageKey, JSON.stringify(lease));
            var verifyRaw = localStorage.getItem(storageKey);
            if (verifyRaw) {
                var verified = JSON.parse(verifyRaw);
                if (verified && verified.tab_id && verified.tab_id !== TAB_ID) {
                    return false;
                }
            }
            return true;
        } catch (e) {
            return true;
        }
    }

    function renewCrossTabSyncLock(userId) {
        var key = String(userId || "").trim();
        if (!key) return false;
        var storageKey = getSyncLockStorageKey(key);
        var now = Date.now();
        try {
            var raw = localStorage.getItem(storageKey);
            if (raw) {
                var existing = JSON.parse(raw);
                if (
                    existing &&
                    existing.tab_id &&
                    existing.tab_id !== TAB_ID &&
                    typeof existing.expires_at === "number" &&
                    existing.expires_at > now
                ) {
                    return false;
                }
            }
            localStorage.setItem(
                storageKey,
                JSON.stringify({
                    tab_id: TAB_ID,
                    user_id: key,
                    acquired_at: now,
                    expires_at: now + SYNC_LOCK_LEASE_MS,
                })
            );
            return true;
        } catch (e) {
            return true;
        }
    }

    function releaseCrossTabSyncLock(userId) {
        var key = String(userId || "").trim();
        if (!key) return;
        var storageKey = getSyncLockStorageKey(key);
        try {
            var raw = localStorage.getItem(storageKey);
            if (!raw) return;
            var existing = JSON.parse(raw);
            if (!existing || existing.tab_id === TAB_ID) {
                localStorage.removeItem(storageKey);
            }
        } catch (e) {}
    }

    function releaseAllLocksForThisTab() {
        Object.keys(inTabSyncLocks).forEach(function (uid) {
            delete inTabSyncLocks[uid];
            releaseCrossTabSyncLock(uid);
        });
        inFlightMutationsByUser = {};
        if (tabInitialDomUserId) {
            releaseCrossTabSyncLock(tabInitialDomUserId);
        }
        if (offlineRenderedUserId) {
            releaseCrossTabSyncLock(offlineRenderedUserId);
        }
    }

    /**
     * Immediately invalidates the current tab when another tab switches accounts
     * or logs out, or when the server reports an identity mismatch.
     * Closes IndexedDB connections without deleting queued items, cancels in-flight
     * requests, disables actions, and clears rendered private records from the DOM.
     * Never writes to localStorage or re-broadcasts signals.
     */
    function invalidateCurrentTab(reason) {
        tabGeneration += 1;
        tabInvalidated = true;
        tabInvalidationReason = reason || "session_invalidated";

        abortInFlightRequests();
        releaseAllLocksForThisTab();
        closeAllOpenConnections();
        clearAndDisablePrivateDom(tabInvalidationReason);
    }

    function clearAndDisablePrivateDom(reason) {
        if (typeof document === "undefined" || !document.body) return;

        document.body.setAttribute("data-tab-invalidated", "true");
        document.body.setAttribute("data-invalidation-reason", reason || "session_invalidated");
        document.body.removeAttribute("data-authenticated-user-id");
        document.body.removeAttribute("data-authenticated-username");

        var badge = document.getElementById("offline-queue-badge");
        if (badge) {
            badge.textContent = "";
            badge.classList.add("d-none");
        }

        var reviewPanel = document.getElementById("offline-queue-review-panel");
        if (reviewPanel) {
            while (reviewPanel.firstChild) {
                reviewPanel.removeChild(reviewPanel.firstChild);
            }
            reviewPanel.classList.add("d-none");
        }

        var syncBtn = document.getElementById("btn-sync-now");
        if (syncBtn) {
            syncBtn.disabled = true;
            syncBtn.setAttribute("aria-disabled", "true");
        }

        var settingsSyncBtn = document.getElementById("btn-settings-sync");
        if (settingsSyncBtn) {
            settingsSyncBtn.disabled = true;
            settingsSyncBtn.setAttribute("aria-disabled", "true");
        }

        var loadMoreBtn = document.getElementById("btn-offline-load-more");
        if (loadMoreBtn) {
            loadMoreBtn.disabled = true;
            loadMoreBtn.classList.add("d-none");
        }

        if (isOfflineShellPage()) {
            var userBadge = document.getElementById("offline-active-user-badge");
            if (userBadge) {
                userBadge.textContent = "Profile locked — account changed or signed out";
            }
            var lastSyncBadge = document.getElementById("offline-last-sync-badge");
            if (lastSyncBadge) {
                lastSyncBadge.textContent = "";
            }
            var boundedNotice = document.getElementById("offline-bounded-notice");
            if (boundedNotice) {
                boundedNotice.classList.add("d-none");
            }
            var noSnapshotAlert = document.getElementById("offline-no-snapshot-alert");
            if (noSnapshotAlert) {
                noSnapshotAlert.classList.remove("d-none");
            }
            var queuedCountBadge = document.getElementById("offline-queued-count-badge");
            if (queuedCountBadge) {
                queuedCountBadge.textContent = "Locked";
            }
            var txSummaryBadge = document.getElementById("offline-tx-summary-badge");
            if (txSummaryBadge) {
                txSummaryBadge.textContent = "Locked";
            }

            ["offline-queued-list", "offline-wallets-list", "offline-categories-list"].forEach(
                function (id) {
                    var el = document.getElementById(id);
                    if (!el) return;
                    while (el.firstChild) {
                        el.removeChild(el.firstChild);
                    }
                    var msg = document.createElement("p");
                    msg.className = "text-secondary small mb-0";
                    msg.textContent =
                        "Private offline data is locked because the session ended or switched accounts. Sign in while online to access your account.";
                    el.appendChild(msg);
                }
            );

            var tbody = document.getElementById("offline-transactions-tbody");
            if (tbody) {
                while (tbody.firstChild) {
                    tbody.removeChild(tbody.firstChild);
                }
                var tr = document.createElement("tr");
                var td = document.createElement("td");
                td.colSpan = 6;
                td.className = "text-secondary small py-3 text-center";
                td.textContent =
                    "Private offline history is locked because the session ended or switched accounts.";
                tr.appendChild(td);
                tbody.appendChild(tr);
            }

            var offlineForm = document.getElementById("offline-entry-form");
            if (offlineForm) {
                offlineForm.reset();
                var controls = offlineForm.querySelectorAll("input, select, textarea, button");
                controls.forEach(function (ctrl) {
                    ctrl.disabled = true;
                    ctrl.setAttribute("aria-disabled", "true");
                });
                [
                    "offline-tx-wallet",
                    "offline-tx-category",
                    "offline-tx-shared-space",
                    "offline-tx-paid-by",
                ].forEach(function (selId) {
                    var sel = document.getElementById(selId);
                    if (!sel) return;
                    while (sel.firstChild) {
                        sel.removeChild(sel.firstChild);
                    }
                    var opt = document.createElement("option");
                    opt.value = "";
                    opt.textContent = "Locked";
                    sel.appendChild(opt);
                });
            }
        } else {
            var mainContainer = document.querySelector("#main-content .container-fluid");
            if (mainContainer) {
                var banner = document.getElementById("offline-sync-banner");
                var children = Array.prototype.slice.call(mainContainer.children);
                children.forEach(function (child) {
                    if (
                        child !== banner &&
                        child.id !== "offline-queue-review-panel" &&
                        child.id !== "tab-invalidated-lock-notice"
                    ) {
                        mainContainer.removeChild(child);
                    }
                });
                if (!document.getElementById("tab-invalidated-lock-notice")) {
                    var lockBox = document.createElement("div");
                    lockBox.id = "tab-invalidated-lock-notice";
                    lockBox.className = "dashboard-panel p-4";
                    var h2 = document.createElement("h2");
                    h2.className = "h6 fw-semibold mb-2";
                    h2.textContent = "Tab locked after account change or sign-out";
                    var p = document.createElement("p");
                    p.className = "text-secondary small mb-0";
                    p.textContent =
                        "Private records and entry controls in this tab have been cleared because your session changed in another tab. Any pending offline changes for the previous account remain safely stored in isolated local storage.";
                    lockBox.appendChild(h2);
                    lockBox.appendChild(p);
                    mainContainer.appendChild(lockBox);
                }
            }

            var modalEl = document.getElementById("transactionModal");
            if (modalEl) {
                if (window.bootstrap && window.bootstrap.Modal) {
                    var inst = window.bootstrap.Modal.getInstance(modalEl);
                    if (inst) inst.hide();
                }
                while (modalEl.firstChild) {
                    modalEl.removeChild(modalEl.firstChild);
                }
            }

            var allControls = document.querySelectorAll(
                "header form button, header form select, header button[data-bs-target='#transactionModal']"
            );
            allControls.forEach(function (ctrl) {
                ctrl.disabled = true;
                ctrl.setAttribute("aria-disabled", "true");
            });
        }

        var bannerMsg =
            reason === "account_switched"
                ? "Another account signed in on this browser. This tab has been locked and private data cleared to protect account isolation. Reload or sign in to continue."
                : "Your session was signed out or invalidated. This tab has been locked and private data cleared. Sign in while online to continue.";
        setBannerState("danger", bannerMsg);
    }

    function getDbName(userId) {
        return "budget_tracker_offline_u_" + String(userId).trim();
    }

    function trackOpenConnection(userId, db) {
        var key = String(userId || "").trim();
        if (!key || !db) return;
        if (!Array.isArray(openConnections[key])) {
            openConnections[key] = [];
        }
        if (openConnections[key].indexOf(db) === -1) {
            openConnections[key].push(db);
        }
    }

    function closeSpecificConnection(userId, db) {
        var key = String(userId || "").trim();
        if (db) {
            try {
                db.close();
            } catch (e) {}
        }
        if (key && Array.isArray(openConnections[key])) {
            var idx = openConnections[key].indexOf(db);
            if (idx !== -1) {
                openConnections[key].splice(idx, 1);
            }
            if (openConnections[key].length === 0) {
                delete openConnections[key];
            }
        }
    }

    function closeOpenConnection(userId) {
        if (!userId) return;
        var key = String(userId).trim();
        var list = openConnections[key];
        if (Array.isArray(list)) {
            list.slice().forEach(function (conn) {
                try {
                    conn.close();
                } catch (e) {}
            });
        } else if (list && typeof list.close === "function") {
            try {
                list.close();
            } catch (e) {}
        }
        delete openConnections[key];
    }

    function closeAllOpenConnections() {
        Object.keys(openConnections).forEach(function (k) {
            closeOpenConnection(k);
        });
    }

    function broadcastSignal(payload) {
        var msg = Object.assign({ timestamp: Date.now(), source_tab_id: TAB_ID }, payload);
        if (syncChannel) {
            try {
                syncChannel.postMessage(msg);
            } catch (e) {}
        }
        try {
            localStorage.setItem(STORAGE_DB_SIGNAL, JSON.stringify(msg));
        } catch (e) {}
    }

    function getTabBoundUserId() {
        return tabInitialDomUserId || offlineRenderedUserId || "";
    }

    function handleIncomingSignal(msg) {
        if (!msg || typeof msg !== "object") return;
        if (msg.source_tab_id && msg.source_tab_id === TAB_ID) return;

        var boundUserId = getTabBoundUserId();

        if (msg.type === "close_db" && msg.user_id) {
            closeOpenConnection(msg.user_id);
        } else if (msg.type === "logout") {
            if (msg.user_id) {
                closeOpenConnection(msg.user_id);
            }
            if (!boundUserId || !msg.user_id || String(boundUserId) === String(msg.user_id)) {
                invalidateCurrentTab("logged_out");
            }
        } else if (msg.type === "account_switched") {
            if (msg.previous_user_id) {
                closeOpenConnection(msg.previous_user_id);
            }
            if (
                boundUserId &&
                ((msg.user_id && String(msg.user_id) !== String(boundUserId)) ||
                    (msg.previous_user_id && String(msg.previous_user_id) === String(boundUserId)))
            ) {
                invalidateCurrentTab("account_switched");
                return;
            }
            if (!boundUserId && isOfflineShellPage()) {
                invalidateCurrentTab("account_switched");
            }
        } else if (msg.type === "queue_updated" && msg.user_id) {
            if (tabInvalidated) return;
            var active = getActiveIdentity();
            if (!active.invalidated && active.userId && String(active.userId) === String(msg.user_id)) {
                updateUiStatus(active.userId);
                if (isOfflineShellPage()) {
                    renderOfflinePage(active.userId, active.username);
                }
            }
        }
    }

    if (syncChannel) {
        syncChannel.onmessage = function (ev) {
            handleIncomingSignal(ev.data);
        };
    }

    if (typeof window !== "undefined") {
        window.addEventListener("storage", function (ev) {
            if (ev.key === STORAGE_DB_SIGNAL && ev.newValue) {
                try {
                    handleIncomingSignal(JSON.parse(ev.newValue));
                } catch (e) {}
            } else if (ev.key === STORAGE_ACTIVE_USER_ID) {
                var boundUserId = getTabBoundUserId();
                var newVal = (ev.newValue || "").trim();
                if (!newVal) {
                    invalidateCurrentTab("logged_out");
                } else if (boundUserId && newVal !== String(boundUserId)) {
                    invalidateCurrentTab("account_switched");
                }
            } else if (ev.key === STORAGE_AUTH_STATE) {
                var newState = (ev.newValue || "").trim();
                if (newState === "logged_out" || newState === "invalidated") {
                    invalidateCurrentTab("logged_out");
                }
            }
        });
    }

    function getCsrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var match = (document.cookie || "").match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function generateUuid() {
        if (typeof window !== "undefined" && window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
            var r = (Math.random() * 16) | 0;
            var v = c === "x" ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }

    function computeMutationFingerprint(mutation) {
        if (!mutation || typeof mutation !== "object") return "";
        var payload = mutation.payload || {};
        var sortedKeys = Object.keys(payload).sort();
        var canonicalPayload = {};
        sortedKeys.forEach(function (k) {
            canonicalPayload[k] = payload[k];
        });
        return JSON.stringify({
            op: String(mutation.operation || ""),
            entity_id: String(mutation.entity_id || ""),
            base_revision:
                mutation.base_revision !== undefined && mutation.base_revision !== null
                    ? mutation.base_revision
                    : null,
            payload: canonicalPayload,
        });
    }

    function compareLegacyOrder(a, b) {
        var timeCmp = String(a.queued_at || "").localeCompare(String(b.queued_at || ""));
        if (timeCmp !== 0) return timeCmp;
        return String(a.mutation_id || "").localeCompare(String(b.mutation_id || ""));
    }

    function compareQueueOrder(a, b) {
        var seqA =
            typeof a.queue_seq === "number" && a.queue_seq > 0
                ? a.queue_seq
                : Number.MAX_SAFE_INTEGER;
        var seqB =
            typeof b.queue_seq === "number" && b.queue_seq > 0
                ? b.queue_seq
                : Number.MAX_SAFE_INTEGER;
        if (seqA !== seqB) {
            return seqA - seqB;
        }
        return compareLegacyOrder(a, b);
    }

    function isValidCurrencyAmountString(rawValue, decimalPlaces) {
        if (typeof rawValue !== "string") return false;
        var trimmed = rawValue.trim();
        if (!trimmed) return false;
        var places = typeof decimalPlaces === "number" && !isNaN(decimalPlaces) ? decimalPlaces : 2;
        var pattern =
            places === 0
                ? /^\d{1,15}(\.0+)?$/
                : new RegExp("^\\d{1,15}(\\.\\d{1," + places + "}0*)?$");
        if (!pattern.test(trimmed)) {
            return false;
        }
        var parts = trimmed.split(".");
        var intPart = parts[0].replace(/^0+/, "") || "0";
        var fracPart = (parts[1] || "").replace(/0+$/, "");
        return intPart !== "0" || fracPart.length > 0;
    }

    function isValidManualRateString(rawValue) {
        if (rawValue === null || rawValue === undefined) return true;
        var trimmed = String(rawValue).trim();
        if (!trimmed) return true;
        if (!/^\d{1,12}(\.\d{1,8}0*)?$/.test(trimmed)) return false;
        var parts = trimmed.split(".");
        var intPart = parts[0].replace(/^0+/, "") || "0";
        var fracPart = (parts[1] || "").replace(/0+$/, "");
        return intPart !== "0" || fracPart.length > 0;
    }

    function openUserDb(userId) {
        return new Promise(function (resolve, reject) {
            var key = String(userId || "").trim();
            if (!key || tabInvalidated || typeof window === "undefined" || !window.indexedDB) {
                resolve(null);
                return;
            }
            var settled = false;
            var req = window.indexedDB.open(getDbName(key), DB_VERSION);
            req.onupgradeneeded = function (event) {
                var db = event.target.result;
                if (!db.objectStoreNames.contains("snapshot")) {
                    db.createObjectStore("snapshot", { keyPath: "key" });
                }
                if (!db.objectStoreNames.contains("mutation_queue")) {
                    db.createObjectStore("mutation_queue", { keyPath: "mutation_id" });
                }
            };
            req.onsuccess = function (event) {
                if (settled) return;
                settled = true;
                var db = event.target.result;
                if (tabInvalidated) {
                    try {
                        db.close();
                    } catch (e) {}
                    resolve(null);
                    return;
                }
                trackOpenConnection(key, db);
                db.onversionchange = function () {
                    closeSpecificConnection(key, db);
                };
                resolve(db);
            };
            req.onerror = function () {
                if (settled) return;
                settled = true;
                reject(req.error);
            };
            req.onblocked = function () {
                if (settled) return;
                settled = true;
                resolve(null);
            };
        });
    }

    /**
     * Reads all queued mutations for `userId`, non-destructively assigning
     * explicit sequential `queue_seq`, `local_version`, and `local_fingerprint`
     * metadata to any older queued entries that lack them, and returns the
     * queue sorted in strict `queue_seq` order.
     */
    function getQueuedMutations(userId) {
        var key = String(userId || "").trim();
        if (!key || tabInvalidated) return Promise.resolve([]);
        return openUserDb(key)
            .then(function (db) {
                if (!db) return [];
                return new Promise(function (resolve) {
                    var settled = false;
                    var resultItems = [];
                    function finish(items) {
                        if (settled) return;
                        settled = true;
                        closeSpecificConnection(key, db);
                        resolve(items);
                    }
                    var tx;
                    try {
                        tx = db.transaction(["mutation_queue", "snapshot"], "readwrite");
                    } catch (e) {
                        finish([]);
                        return;
                    }
                    var queueStore = tx.objectStore("mutation_queue");
                    var snapStore = tx.objectStore("snapshot");

                    tx.oncomplete = function () {
                        resultItems.sort(compareQueueOrder);
                        finish(resultItems);
                    };
                    tx.onerror = function () {
                        finish([]);
                    };
                    tx.onabort = function () {
                        finish([]);
                    };

                    var itemsReq = queueStore.getAll();
                    itemsReq.onsuccess = function () {
                        var rawItems = itemsReq.result || [];
                        var seqReq = snapStore.get("queue_seq_meta");
                        seqReq.onsuccess = function () {
                            var seqMeta = seqReq.result;
                            var maxSeq =
                                seqMeta && typeof seqMeta.last_seq === "number" ? seqMeta.last_seq : 0;

                            var withSeq = [];
                            var withoutSeq = [];

                            rawItems.forEach(function (item) {
                                if (typeof item.queue_seq === "number" && item.queue_seq > 0) {
                                    if (item.queue_seq > maxSeq) {
                                        maxSeq = item.queue_seq;
                                    }
                                    withSeq.push(item);
                                } else {
                                    withoutSeq.push(item);
                                }
                            });

                            withoutSeq.sort(compareLegacyOrder);

                            var metaUpdated = false;
                            withoutSeq.forEach(function (legacyItem) {
                                maxSeq += 1;
                                legacyItem.queue_seq = maxSeq;
                                if (typeof legacyItem.local_version !== "number" || legacyItem.local_version < 1) {
                                    legacyItem.local_version = 1;
                                }
                                if (!legacyItem.local_fingerprint) {
                                    legacyItem.local_fingerprint = computeMutationFingerprint(legacyItem);
                                }
                                queueStore.put(legacyItem);
                                metaUpdated = true;
                            });

                            withSeq.forEach(function (item) {
                                var itemChanged = false;
                                if (typeof item.local_version !== "number" || item.local_version < 1) {
                                    item.local_version = 1;
                                    itemChanged = true;
                                }
                                if (!item.local_fingerprint) {
                                    item.local_fingerprint = computeMutationFingerprint(item);
                                    itemChanged = true;
                                }
                                if (itemChanged) {
                                    queueStore.put(item);
                                }
                            });

                            if (metaUpdated || !seqMeta || seqMeta.last_seq < maxSeq) {
                                snapStore.put({ key: "queue_seq_meta", last_seq: maxSeq });
                            }

                            resultItems = withSeq.concat(withoutSeq);
                        };
                    };
                });
            })
            .catch(function () {
                return [];
            });
    }

    function getInFlightEntryForUser(userId, mutationId, entityId) {
        var key = String(userId || "").trim();
        var map = inFlightMutationsByUser[key];
        if (!map) return null;
        if (mutationId && map[mutationId]) {
            return map[mutationId];
        }
        if (entityId) {
            var targetEntity = String(entityId).trim();
            var ids = Object.keys(map);
            for (var i = 0; i < ids.length; i++) {
                var entry = map[ids[i]];
                if (entry && String(entry.entity_id || "").trim() === targetEntity) {
                    return entry;
                }
            }
        }
        return null;
    }

    /**
     * Enqueues or updates a mutation in `mutation_queue`.
     * - If the target mutation (or another mutation on the same entity) is currently
     *   in flight / awaiting server confirmation, keeps the uploaded mutation's ID,
     *   operation, base_revision, and payload completely immutable for retries and
     *   records the newer edit or deletion in `staged_followup`.
     * - If the caller passes a `mutation_id` that was already acknowledged by the
     *   server, assigns a new `mutation_id` and rebases onto the committed server
     *   revision so committed mutation IDs are never reused with changed payloads.
     */
    function saveMutationToQueue(userId, mutation, options) {
        var opts = options || {};
        var key = String(userId || "").trim();
        if (!key || tabInvalidated || !mutation) return Promise.resolve(null);
        return openUserDb(key).then(function (db) {
            if (!db) return null;
            return new Promise(function (resolve, reject) {
                var settled = false;
                var savedResult = mutation;
                function finishOk(res) {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    if (!opts.suppressBroadcast) {
                        broadcastSignal({ type: "queue_updated", user_id: key });
                    }
                    resolve(res);
                }
                function finishErr(err) {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    reject(err);
                }

                var tx;
                try {
                    tx = db.transaction(["mutation_queue", "snapshot"], "readwrite");
                } catch (e) {
                    finishErr(e);
                    return;
                }
                var queueStore = tx.objectStore("mutation_queue");
                var snapStore = tx.objectStore("snapshot");

                tx.oncomplete = function () {
                    finishOk(savedResult);
                };
                tx.onerror = function () {
                    finishErr(tx.error);
                };
                tx.onabort = function () {
                    finishErr(tx.error || new Error("IndexedDB transaction aborted."));
                };

                var allReq = queueStore.getAll();
                allReq.onsuccess = function () {
                    var allItems = allReq.result || [];
                    var seqReq = snapStore.get("queue_seq_meta");
                    seqReq.onsuccess = function () {
                        var seqMeta = seqReq.result;
                        var ackReq = snapStore.get("acknowledged_mutations");
                        ackReq.onsuccess = function () {
                            var ackRecord = ackReq.result;
                            var ackMap =
                                ackRecord && ackRecord.by_id && typeof ackRecord.by_id === "object"
                                    ? ackRecord.by_id
                                    : {};
                            var entityRevMap =
                                ackRecord && ackRecord.by_entity && typeof ackRecord.by_entity === "object"
                                    ? ackRecord.by_entity
                                    : {};

                            var maxSeq =
                                seqMeta && typeof seqMeta.last_seq === "number"
                                    ? seqMeta.last_seq
                                    : 0;
                            var existingRecord = null;
                            var inFlightEntityRecord = null;
                            var targetEntityId = String(mutation.entity_id || "").trim();

                            allItems.forEach(function (it) {
                                if (typeof it.queue_seq === "number" && it.queue_seq > maxSeq) {
                                    maxSeq = it.queue_seq;
                                }
                                if (it.mutation_id === mutation.mutation_id) {
                                    existingRecord = it;
                                }
                                if (
                                    targetEntityId &&
                                    String(it.entity_id || "").trim() === targetEntityId &&
                                    (it.upload_in_flight ||
                                        getInFlightEntryForUser(key, it.mutation_id, null))
                                ) {
                                    inFlightEntityRecord = it;
                                }
                            });

                            var inMemInFlight = getInFlightEntryForUser(
                                key,
                                mutation.mutation_id,
                                targetEntityId
                            );

                            // Case 1: Editing an existing queued item (or queueing a follow-up on the same entity) while its upload is in flight
                            var activeInFlightRecord =
                                existingRecord &&
                                (existingRecord.upload_in_flight ||
                                    getInFlightEntryForUser(key, existingRecord.mutation_id, null))
                                    ? existingRecord
                                    : !existingRecord && inFlightEntityRecord && !opts.isSyncStatusUpdate
                                    ? inFlightEntityRecord
                                    : null;

                            if (activeInFlightRecord && !opts.isSyncStatusUpdate) {
                                var immutableBase =
                                    activeInFlightRecord.uploaded_request ||
                                    inMemInFlight || {
                                        mutation_id: activeInFlightRecord.mutation_id,
                                        queue_seq: activeInFlightRecord.queue_seq,
                                        operation: activeInFlightRecord.operation,
                                        entity_id: activeInFlightRecord.entity_id,
                                        base_revision: activeInFlightRecord.base_revision,
                                        queued_at: activeInFlightRecord.queued_at,
                                        payload: cloneJson(activeInFlightRecord.payload || {}),
                                        local_version: activeInFlightRecord.local_version || 1,
                                        local_fingerprint:
                                            activeInFlightRecord.local_fingerprint ||
                                            computeMutationFingerprint(activeInFlightRecord),
                                    };

                                // Keep top-level uploaded fields immutable for safe retries if response is lost
                                activeInFlightRecord.mutation_id = immutableBase.mutation_id;
                                activeInFlightRecord.queue_seq = immutableBase.queue_seq;
                                activeInFlightRecord.operation = immutableBase.operation;
                                activeInFlightRecord.entity_id = immutableBase.entity_id;
                                activeInFlightRecord.base_revision = immutableBase.base_revision;
                                activeInFlightRecord.payload = cloneJson(immutableBase.payload || {});
                                activeInFlightRecord.local_fingerprint =
                                    immutableBase.local_fingerprint ||
                                    computeMutationFingerprint(activeInFlightRecord);
                                activeInFlightRecord.upload_in_flight = true;
                                activeInFlightRecord.uploaded_request = cloneJson(immutableBase);

                                var prevFollowup = activeInFlightRecord.staged_followup;
                                var candidateNewId =
                                    mutation.mutation_id &&
                                    mutation.mutation_id !== activeInFlightRecord.mutation_id &&
                                    !ackMap[mutation.mutation_id]
                                        ? mutation.mutation_id
                                        : prevFollowup &&
                                          prevFollowup.mutation_id &&
                                          prevFollowup.mutation_id !== activeInFlightRecord.mutation_id
                                        ? prevFollowup.mutation_id
                                        : generateUuid();

                                var followupOp =
                                    mutation.operation === "delete_transaction"
                                        ? "delete_transaction"
                                        : "update_transaction";

                                activeInFlightRecord.staged_followup = {
                                    mutation_id: candidateNewId,
                                    operation: followupOp,
                                    entity_id: String(
                                        mutation.entity_id || activeInFlightRecord.entity_id || ""
                                    ),
                                    payload: cloneJson(
                                        mutation.payload !== undefined &&
                                            mutation.payload !== null &&
                                            Object.keys(mutation.payload).length > 0
                                            ? mutation.payload
                                            : prevFollowup && prevFollowup.payload
                                            ? prevFollowup.payload
                                            : activeInFlightRecord.payload || {}
                                    ),
                                    queued_at: mutation.queued_at || new Date().toISOString(),
                                };
                                activeInFlightRecord.local_version =
                                    (typeof activeInFlightRecord.local_version === "number"
                                        ? activeInFlightRecord.local_version
                                        : 1) + 1;

                                queueStore.put(activeInFlightRecord);
                                savedResult = activeInFlightRecord;
                                return;
                            }

                            // Case 2: Updating an existing queued item that is NOT currently in flight
                            if (existingRecord) {
                                if (
                                    typeof existingRecord.queue_seq === "number" &&
                                    existingRecord.queue_seq > 0
                                ) {
                                    mutation.queue_seq = existingRecord.queue_seq;
                                } else if (
                                    typeof mutation.queue_seq !== "number" ||
                                    mutation.queue_seq <= 0
                                ) {
                                    maxSeq += 1;
                                    mutation.queue_seq = maxSeq;
                                    snapStore.put({ key: "queue_seq_meta", last_seq: maxSeq });
                                }

                                var prevVersion =
                                    typeof existingRecord.local_version === "number"
                                        ? existingRecord.local_version
                                        : 1;
                                var prevFingerprint =
                                    existingRecord.local_fingerprint ||
                                    computeMutationFingerprint(existingRecord);
                                var updatedFingerprint = computeMutationFingerprint(mutation);

                                if (opts.isSyncStatusUpdate) {
                                    mutation.local_version = prevVersion;
                                    mutation.local_fingerprint = prevFingerprint;
                                } else {
                                    mutation.local_version = Math.max(
                                        prevVersion + 1,
                                        typeof mutation.local_version === "number"
                                            ? mutation.local_version
                                            : 1
                                    );
                                    mutation.local_fingerprint = updatedFingerprint;
                                }

                                queueStore.put(mutation);
                                savedResult = mutation;
                                return;
                            }

                            // Case 3: Attempt to save a mutation whose mutation_id was ALREADY committed & acknowledged
                            if (mutation.mutation_id && ackMap[mutation.mutation_id]) {
                                var prevAck = ackMap[mutation.mutation_id];
                                var ackEntityId = targetEntityId || String((prevAck && prevAck.entity_id) || "").trim();
                                var latestEntRev =
                                    ackEntityId && typeof entityRevMap[ackEntityId] === "number"
                                        ? entityRevMap[ackEntityId]
                                        : typeof prevAck.revision === "number"
                                        ? prevAck.revision
                                        : mutation.base_revision;
                                mutation = Object.assign({}, mutation, {
                                    mutation_id: generateUuid(),
                                    operation:
                                        mutation.operation === "delete_transaction"
                                            ? "delete_transaction"
                                            : "update_transaction",
                                    base_revision: latestEntRev,
                                });
                            }

                            if (typeof mutation.queue_seq === "number" && mutation.queue_seq > 0) {
                                if (mutation.queue_seq > maxSeq) {
                                    maxSeq = mutation.queue_seq;
                                }
                            } else {
                                maxSeq += 1;
                                mutation.queue_seq = maxSeq;
                            }
                            snapStore.put({ key: "queue_seq_meta", last_seq: maxSeq });
                            mutation.local_version =
                                typeof mutation.local_version === "number" &&
                                mutation.local_version >= 1
                                    ? mutation.local_version
                                    : 1;
                            mutation.local_fingerprint = computeMutationFingerprint(mutation);

                            queueStore.put(mutation);
                            savedResult = mutation;
                        };
                    };
                };
            });
        });
    }

    /**
     * Marks a batch of mutations as in-flight in IndexedDB and in memory before
     * sending the HTTP POST, preserving each item's immutable `uploaded_request`
     * snapshot so that if a response is lost, retries send the exact original ID
     * and payload before promoting any dependent newer local edit.
     */
    function markBatchInFlight(userId, batchItems) {
        var key = String(userId || "").trim();
        if (!key || !Array.isArray(batchItems) || !batchItems.length) {
            return Promise.resolve([]);
        }
        if (!inFlightMutationsByUser[key]) {
            inFlightMutationsByUser[key] = {};
        }

        var preparedMetaList = batchItems.map(function (it) {
            var baseReq =
                it.upload_in_flight && it.uploaded_request
                    ? cloneJson(it.uploaded_request)
                    : {
                          mutation_id: it.mutation_id,
                          queue_seq: it.queue_seq,
                          operation: it.operation,
                          entity_id: it.entity_id,
                          base_revision:
                              it.base_revision !== undefined ? it.base_revision : null,
                          queued_at: it.queued_at,
                          payload: cloneJson(it.payload || {}),
                          local_version:
                              typeof it.local_version === "number" ? it.local_version : 1,
                          local_fingerprint:
                              it.local_fingerprint || computeMutationFingerprint(it),
                      };
            inFlightMutationsByUser[key][baseReq.mutation_id] = baseReq;
            return baseReq;
        });

        return openUserDb(key)
            .then(function (db) {
                if (!db) return preparedMetaList;
                return new Promise(function (resolve) {
                    var settled = false;
                    function finish() {
                        if (settled) return;
                        settled = true;
                        closeSpecificConnection(key, db);
                        resolve(preparedMetaList);
                    }
                    var tx;
                    try {
                        tx = db.transaction("mutation_queue", "readwrite");
                    } catch (e) {
                        finish();
                        return;
                    }
                    var store = tx.objectStore("mutation_queue");
                    tx.oncomplete = finish;
                    tx.onerror = finish;
                    tx.onabort = finish;

                    preparedMetaList.forEach(function (baseReq) {
                        var getReq = store.get(baseReq.mutation_id);
                        getReq.onsuccess = function () {
                            var current = getReq.result;
                            if (!current) return;
                            if (!current.upload_in_flight || !current.uploaded_request) {
                                current.upload_in_flight = true;
                                current.uploaded_request = cloneJson(baseReq);
                                store.put(current);
                            }
                        };
                    });
                });
            })
            .catch(function () {
                return preparedMetaList;
            });
    }

    function clearInMemoryInFlight(userId, mutationIds) {
        var key = String(userId || "").trim();
        var map = inFlightMutationsByUser[key];
        if (!map) return;
        (mutationIds || []).forEach(function (mid) {
            delete map[mid];
        });
    }

    /**
     * Atomically processes the server acknowledgment for `mutationId`:
     * - If the queued item was not edited while in flight, removes `mutationId`
     *   and rebases any subsequent pending operations on the same entity that
     *   had a null/pre-commit `base_revision`.
     * - If the queued item WAS edited (or had a deletion staged) while in flight,
     *   atomically deletes the acknowledged `mutationId` and inserts a NEW
     *   mutation with:
     *   - a new `mutation_id` (never reusing the committed ID),
     *   - the same `entity_id` and `queue_seq` order,
     *   - `base_revision` set to `serverResult.revision`,
     *   - the appropriate `update_transaction` or `delete_transaction` operation,
     *   - the latest local payload.
     */
    function acknowledgeAppliedMutation(
        userId,
        mutationId,
        sentVersion,
        sentFingerprint,
        serverResult
    ) {
        var key = String(userId || "").trim();
        if (!key || !mutationId) return Promise.resolve({ removed: false });
        clearInMemoryInFlight(key, [mutationId]);

        return openUserDb(key).then(function (db) {
            if (!db) return { removed: false };
            return new Promise(function (resolve) {
                var settled = false;
                var outcome = {
                    removed: false,
                    replaced_with_followup: false,
                    replacement_mutation_id: null,
                    replacement: null,
                };
                function finish() {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    resolve(outcome);
                }

                var tx;
                try {
                    tx = db.transaction(["mutation_queue", "snapshot"], "readwrite");
                } catch (e) {
                    finish();
                    return;
                }
                var queueStore = tx.objectStore("mutation_queue");
                var snapStore = tx.objectStore("snapshot");

                tx.oncomplete = function () {
                    if (outcome.replaced_with_followup && outcome.replacement) {
                        var form = document.getElementById("offline-entry-form");
                        if (
                            form &&
                            form.getAttribute("data-editing-mutation-id") === String(mutationId)
                        ) {
                            form.setAttribute(
                                "data-editing-mutation-id",
                                outcome.replacement.mutation_id
                            );
                            form.setAttribute(
                                "data-editing-operation",
                                outcome.replacement.operation
                            );
                            form.setAttribute(
                                "data-editing-base-revision",
                                outcome.replacement.base_revision !== null &&
                                    outcome.replacement.base_revision !== undefined
                                    ? String(outcome.replacement.base_revision)
                                    : ""
                            );
                        }
                    }
                    finish();
                };
                tx.onerror = finish;
                tx.onabort = finish;

                var allReq = queueStore.getAll();
                allReq.onsuccess = function () {
                    var allItems = allReq.result || [];
                    var ackReq = snapStore.get("acknowledged_mutations");
                    ackReq.onsuccess = function () {
                        var ackRecord = ackReq.result || {
                            key: "acknowledged_mutations",
                            by_id: {},
                            by_entity: {},
                        };
                        if (!ackRecord.by_id || typeof ackRecord.by_id !== "object") {
                            ackRecord.by_id = {};
                        }
                        if (!ackRecord.by_entity || typeof ackRecord.by_entity !== "object") {
                            ackRecord.by_entity = {};
                        }

                        var current = null;
                        allItems.forEach(function (it) {
                            if (it.mutation_id === mutationId) {
                                current = it;
                            }
                        });

                        var committedRev =
                            serverResult && typeof serverResult.revision === "number"
                                ? serverResult.revision
                                : null;
                        var entityKey = String(
                            (serverResult && serverResult.entity_id) ||
                                (current && current.entity_id) ||
                                ""
                        ).trim();

                        ackRecord.by_id[mutationId] = {
                            entity_id: entityKey,
                            revision: committedRev,
                            acknowledged_at: new Date().toISOString(),
                        };
                        if (entityKey && committedRev !== null) {
                            var prevEntRev = ackRecord.by_entity[entityKey];
                            if (typeof prevEntRev !== "number" || committedRev > prevEntRev) {
                                ackRecord.by_entity[entityKey] = committedRev;
                            }
                        }
                        snapStore.put(ackRecord);

                        if (!current) {
                            outcome.removed = true;
                            return;
                        }

                        var currentVersion =
                            typeof current.local_version === "number" ? current.local_version : 1;
                        var currentFingerprint =
                            current.local_fingerprint || computeMutationFingerprint(current);

                        var hasStagedFollowup = Boolean(
                            current.staged_followup && typeof current.staged_followup === "object"
                        );
                        var versionMatches =
                            sentVersion === undefined ||
                            sentVersion === null ||
                            currentVersion === sentVersion;
                        var fingerprintMatches =
                            !sentFingerprint || currentFingerprint === sentFingerprint;

                        if (!hasStagedFollowup && versionMatches && fingerprintMatches) {
                            queueStore.delete(mutationId);
                            outcome.removed = true;

                            // Rebase any subsequent pending mutations on the same entity that lacked the committed revision
                            if (entityKey && committedRev !== null) {
                                allItems.forEach(function (other) {
                                    if (
                                        other.mutation_id !== mutationId &&
                                        String(other.entity_id || "").trim() === entityKey &&
                                        !other.upload_in_flight &&
                                        (other.base_revision === null ||
                                            other.base_revision === undefined ||
                                            other.base_revision === current.base_revision)
                                    ) {
                                        other.base_revision = committedRev;
                                        if (other.operation === "create_transaction") {
                                            other.operation = "update_transaction";
                                        }
                                        other.local_fingerprint = computeMutationFingerprint(other);
                                        queueStore.put(other);
                                    }
                                });
                            }
                            return;
                        }

                        // A newer edit or deletion occurred while this mutation was in flight.
                        // Atomically delete the acknowledged mutationId and insert a NEW mutation.
                        var staged = current.staged_followup || {};
                        var newMutationId =
                            staged.mutation_id &&
                            staged.mutation_id !== mutationId &&
                            !ackRecord.by_id[staged.mutation_id]
                                ? staged.mutation_id
                                : generateUuid();
                        while (newMutationId === mutationId || ackRecord.by_id[newMutationId]) {
                            newMutationId = generateUuid();
                        }

                        var rawOp = staged.operation || current.operation || "update_transaction";
                        var newOp =
                            rawOp === "delete_transaction"
                                ? "delete_transaction"
                                : "update_transaction";
                        var newBaseRev =
                            committedRev !== null ? committedRev : current.base_revision;
                        var latestPayload = cloneJson(
                            staged.payload !== undefined ? staged.payload : current.payload || {}
                        );

                        var replacement = {
                            mutation_id: newMutationId,
                            queue_seq: current.queue_seq,
                            operation: newOp,
                            entity_id: entityKey || String(current.entity_id || ""),
                            base_revision: newBaseRev,
                            sync_status: "pending",
                            queued_at: staged.queued_at || new Date().toISOString(),
                            payload: latestPayload,
                            local_version: 1,
                            replaces_mutation_id: mutationId,
                        };
                        replacement.local_fingerprint = computeMutationFingerprint(replacement);

                        queueStore.delete(mutationId);
                        queueStore.put(replacement);

                        outcome.removed = false;
                        outcome.replaced_with_followup = true;
                        outcome.replacement_mutation_id = newMutationId;
                        outcome.replacement = replacement;
                    };
                };
            });
        });
    }

    /**
     * Updates the sync error/conflict/blocked status on a queued mutation only
     * if it has not been replaced by a newer local edit while in flight.
     */
    function updateQueueItemStatusIfUnchanged(
        userId,
        mutationId,
        sentVersion,
        sentFingerprint,
        statusFields
    ) {
        var key = String(userId || "").trim();
        if (!key || !mutationId) return Promise.resolve();
        clearInMemoryInFlight(key, [mutationId]);

        return openUserDb(key).then(function (db) {
            if (!db) return;
            return new Promise(function (resolve) {
                var settled = false;
                function finish() {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    resolve();
                }
                var tx;
                try {
                    tx = db.transaction("mutation_queue", "readwrite");
                } catch (e) {
                    finish();
                    return;
                }
                var store = tx.objectStore("mutation_queue");
                tx.oncomplete = finish;
                tx.onerror = finish;
                tx.onabort = finish;

                var getReq = store.get(mutationId);
                getReq.onsuccess = function () {
                    var current = getReq.result;
                    if (!current) return;
                    delete current.upload_in_flight;
                    delete current.uploaded_request;

                    var currentVersion =
                        typeof current.local_version === "number" ? current.local_version : 1;
                    var currentFingerprint =
                        current.local_fingerprint || computeMutationFingerprint(current);

                    var versionMatches =
                        sentVersion === undefined ||
                        sentVersion === null ||
                        currentVersion === sentVersion;
                    var fingerprintMatches =
                        !sentFingerprint || currentFingerprint === sentFingerprint;

                    if (!versionMatches || !fingerprintMatches || current.staged_followup) {
                        store.put(current);
                        return;
                    }

                    current.sync_status = statusFields.sync_status || "error";
                    current.last_error = statusFields.last_error || "Synchronization failed.";
                    if (statusFields.server_revision !== undefined) {
                        current.server_revision = statusFields.server_revision;
                    }
                    if (statusFields.server_is_deleted !== undefined) {
                        current.server_is_deleted = statusFields.server_is_deleted;
                    }
                    if (statusFields.server_record !== undefined) {
                        current.server_record = statusFields.server_record;
                    }
                    current.last_attempt_at = new Date().toISOString();
                    store.put(current);
                };
            });
        });
    }

    function removeMutationFromQueue(userId, mutationId) {
        var key = String(userId || "").trim();
        if (!key || !mutationId) return Promise.resolve();
        clearInMemoryInFlight(key, [mutationId]);
        return openUserDb(key).then(function (db) {
            if (!db) return;
            return new Promise(function (resolve) {
                var settled = false;
                function finish(shouldBroadcast) {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    if (shouldBroadcast) {
                        broadcastSignal({ type: "queue_updated", user_id: key });
                    }
                    resolve();
                }
                var tx;
                try {
                    tx = db.transaction("mutation_queue", "readwrite");
                } catch (e) {
                    finish(false);
                    return;
                }
                tx.objectStore("mutation_queue").delete(mutationId);
                tx.oncomplete = function () {
                    finish(true);
                };
                tx.onerror = function () {
                    finish(false);
                };
                tx.onabort = function () {
                    finish(false);
                };
            });
        });
    }

    function getStateSnapshot(userId) {
        var key = String(userId || "").trim();
        if (!key || tabInvalidated) return Promise.resolve(null);
        return openUserDb(key)
            .then(function (db) {
                if (!db) return null;
                return new Promise(function (resolve) {
                    var settled = false;
                    var snapData = null;
                    function finish(val) {
                        if (settled) return;
                        settled = true;
                        closeSpecificConnection(key, db);
                        resolve(val);
                    }
                    var tx;
                    try {
                        tx = db.transaction("snapshot", "readonly");
                    } catch (e) {
                        finish(null);
                        return;
                    }
                    var req = tx.objectStore("snapshot").get("latest");
                    req.onsuccess = function () {
                        snapData = req.result ? req.result.data : null;
                    };
                    tx.oncomplete = function () {
                        finish(snapData);
                    };
                    tx.onerror = function () {
                        finish(null);
                    };
                    tx.onabort = function () {
                        finish(null);
                    };
                });
            })
            .catch(function () {
                return null;
            });
    }

    function saveStateSnapshot(userId, snapshotData) {
        var key = String(userId || "").trim();
        if (!key || tabInvalidated) return Promise.resolve(null);
        return openUserDb(key).then(function (db) {
            if (!db) return null;
            return new Promise(function (resolve) {
                var settled = false;
                function finish(val) {
                    if (settled) return;
                    settled = true;
                    closeSpecificConnection(key, db);
                    resolve(val);
                }
                var tx;
                try {
                    tx = db.transaction("snapshot", "readwrite");
                } catch (e) {
                    finish(null);
                    return;
                }
                tx.objectStore("snapshot").put({
                    key: "latest",
                    saved_at: new Date().toISOString(),
                    data: snapshotData,
                });
                tx.oncomplete = function () {
                    finish(snapshotData);
                };
                tx.onerror = function () {
                    finish(null);
                };
                tx.onabort = function () {
                    finish(null);
                };
            });
        });
    }

    function deleteUserDatabase(userId) {
        return new Promise(function (resolve) {
            var key = String(userId || "").trim();
            if (!key || typeof window === "undefined" || !window.indexedDB) {
                resolve({ status: "deleted" });
                return;
            }
            closeOpenConnection(key);
            broadcastSignal({ type: "close_db", user_id: key });

            var settled = false;
            var req = window.indexedDB.deleteDatabase(getDbName(key));
            req.onsuccess = function () {
                if (settled) return;
                settled = true;
                resolve({ status: "deleted" });
            };
            req.onerror = function () {
                if (settled) return;
                settled = true;
                resolve({
                    status: "error",
                    message: "Failed to delete local IndexedDB storage for this account.",
                });
            };
            req.onblocked = function () {
                if (settled) return;
                settled = true;
                resolve({
                    status: "blocked",
                    message:
                        "Local offline database deletion is blocked by another open tab. Close other Budget Tracker tabs to complete cleanup.",
                });
            };
        });
    }

    function setBannerState(variant, message) {
        var banner = document.getElementById("offline-sync-banner");
        var bannerText = document.getElementById("offline-sync-banner-text");
        if (!banner || !bannerText) return;

        banner.classList.remove("d-none", "alert-warning", "alert-danger", "alert-success", "alert-info");
        banner.classList.add("alert-" + (variant || "warning"));
        bannerText.textContent = message;
    }

    function isNonPendingFailureStatus(status) {
        return (
            status === "error" ||
            status === "conflict" ||
            status === "forbidden" ||
            status === "blocked_by_dependency"
        );
    }

    function renderQueueReviewPanel(userId, items) {
        var panel = document.getElementById("offline-queue-review-panel");
        if (!panel) return;

        while (panel.firstChild) {
            panel.removeChild(panel.firstChild);
        }

        if (tabInvalidated || !items || !items.length) {
            panel.classList.add("d-none");
            return;
        }

        var failedOrConflicting = items.filter(function (item) {
            return isNonPendingFailureStatus(item.sync_status);
        });
        if (!failedOrConflicting.length && navigator.onLine !== false) {
            panel.classList.add("d-none");
            return;
        }

        panel.classList.remove("d-none");
        var heading = document.createElement("div");
        heading.className = "d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2";
        var title = document.createElement("strong");
        title.className = "small";
        title.textContent =
            failedOrConflicting.length > 0
                ? failedOrConflicting.length + " offline change(s) require your attention"
                : items.length + " offline change(s) queued locally";
        heading.appendChild(title);
        panel.appendChild(heading);

        var list = document.createElement("div");
        list.className = "vstack gap-2";
        items.forEach(function (item) {
            list.appendChild(buildQueuedMutationCard(userId, item));
        });
        panel.appendChild(list);
    }

    function buildQueuedMutationCard(userId, item) {
        var card = document.createElement("div");
        var status = item.sync_status || "pending";
        var borderClass = isNonPendingFailureStatus(status) ? "border-danger" : "border-warning";
        card.className = "border rounded p-2 bg-white " + borderClass;

        var topRow = document.createElement("div");
        topRow.className = "d-flex flex-wrap align-items-center justify-content-between gap-2";

        var displaySource = item.staged_followup || item;
        var summary = document.createElement("div");
        summary.className = "small fw-medium";
        var opLabel =
            displaySource.operation === "create_transaction"
                ? "Create"
                : displaySource.operation === "update_transaction"
                ? "Update"
                : displaySource.operation === "delete_transaction"
                ? "Delete"
                : displaySource.operation;
        var p = displaySource.payload || item.payload || {};
        var detailParts = [];
        if (typeof item.queue_seq === "number") {
            detailParts.push("#" + String(item.queue_seq));
        }
        detailParts.push(opLabel);
        if (p.transaction_type) detailParts.push(String(p.transaction_type).toUpperCase());
        if (p.amount) detailParts.push(String(p.amount));
        if (p.transaction_date) detailParts.push("on " + String(p.transaction_date));
        if (p.description) detailParts.push("— " + String(p.description));
        summary.textContent = detailParts.join(" ");

        var statusBadge = document.createElement("span");
        if (status === "conflict") {
            statusBadge.className = "badge bg-danger-subtle text-danger-emphasis border";
            statusBadge.textContent = "Revision conflict";
        } else if (status === "blocked_by_dependency") {
            statusBadge.className = "badge bg-danger-subtle text-danger-emphasis border";
            statusBadge.textContent = "Blocked by earlier failure";
        } else if (status === "forbidden") {
            statusBadge.className = "badge bg-danger-subtle text-danger-emphasis border";
            statusBadge.textContent = "Permission denied";
        } else if (status === "error") {
            statusBadge.className = "badge bg-danger-subtle text-danger-emphasis border";
            statusBadge.textContent = "Validation / sync error";
        } else {
            statusBadge.className = "badge bg-warning-subtle text-warning-emphasis border";
            statusBadge.textContent = "Pending sync";
        }

        topRow.appendChild(summary);
        topRow.appendChild(statusBadge);
        card.appendChild(topRow);

        var meta = document.createElement("div");
        meta.className = "text-secondary small mt-1";
        var scopeInfo = "Scope: " + (p.scope || "personal");
        if (item.base_revision !== null && item.base_revision !== undefined) {
            scopeInfo += " | Base revision: " + String(item.base_revision);
        }
        if (p.manual_rate) {
            scopeInfo += " | Manual rate: " + String(p.manual_rate);
        }
        meta.textContent = scopeInfo;
        card.appendChild(meta);

        if (item.last_error) {
            var errBox = document.createElement("div");
            errBox.className = "text-danger small mt-1";
            errBox.textContent = "Server message: " + String(item.last_error);
            card.appendChild(errBox);
        }

        var btnGroup = document.createElement("div");
        btnGroup.className = "d-flex flex-wrap gap-2 mt-2";

        var retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn btn-outline-primary btn-sm py-0 px-2";
        retryBtn.textContent = "Retry sync";
        retryBtn.addEventListener("click", function () {
            item.sync_status = "pending";
            delete item.last_error;
            saveMutationToQueue(userId, item, { isSyncStatusUpdate: true }).then(function () {
                flushMutationQueue(userId, {
                    retryFailed: true,
                    retryMutationId: item.mutation_id,
                });
            });
        });
        btnGroup.appendChild(retryBtn);

        if (status === "conflict" && item.server_revision) {
            var adoptRevBtn = document.createElement("button");
            adoptRevBtn.type = "button";
            adoptRevBtn.className = "btn btn-outline-secondary btn-sm py-0 px-2";
            adoptRevBtn.textContent =
                "Rebase onto server rev " + String(item.server_revision) + " & retry";
            adoptRevBtn.addEventListener("click", function () {
                var oldMutationId = item.original_mutation_id || item.mutation_id;
                item.mutation_id = generateUuid();
                item.base_revision = item.server_revision;
                item.sync_status = "pending";
                delete item.last_error;
                removeMutationFromQueue(userId, oldMutationId).then(function () {
                    saveMutationToQueue(userId, item).then(function () {
                        flushMutationQueue(userId, {
                            retryFailed: true,
                            retryMutationId: item.mutation_id,
                        });
                    });
                });
            });
            item.original_mutation_id = item.mutation_id;
            btnGroup.appendChild(adoptRevBtn);
        }

        if (
            displaySource.operation !== "delete_transaction" &&
            document.getElementById("offline-entry-form")
        ) {
            var editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "btn btn-outline-dark btn-sm py-0 px-2";
            editBtn.textContent = "Edit in form";
            editBtn.addEventListener("click", function () {
                loadMutationIntoOfflineForm(userId, item);
            });
            btnGroup.appendChild(editBtn);
        }

        var discardBtn = document.createElement("button");
        discardBtn.type = "button";
        discardBtn.className = "btn btn-outline-danger btn-sm py-0 px-2";
        discardBtn.textContent = "Discard";
        discardBtn.addEventListener("click", function () {
            removeMutationFromQueue(userId, item.mutation_id).then(function () {
                updateUiStatus(userId);
                if (isOfflineShellPage()) {
                    var identity = getActiveIdentity();
                    renderOfflinePage(identity.userId, identity.username);
                }
            });
        });
        btnGroup.appendChild(discardBtn);

        card.appendChild(btnGroup);
        return card;
    }

    function updateUiStatus(userId, explicitSyncOutcome) {
        if (tabInvalidated) {
            return Promise.resolve();
        }
        var runGen = tabGeneration;
        var banner = document.getElementById("offline-sync-banner");
        var bannerText = document.getElementById("offline-sync-banner-text");
        var badge = document.getElementById("offline-queue-badge");
        var queuedCountBadge = document.getElementById("offline-queued-count-badge");

        if (!userId) {
            if (isOfflineShellPage() && banner && bannerText) {
                setBannerState(
                    "warning",
                    "No active offline user profile is stored in this browser. Sign in while online to enable offline access."
                );
            }
            return Promise.resolve();
        }

        return getQueuedMutations(userId).then(function (items) {
            if (!isGenerationValid(runGen, userId)) {
                return;
            }
            var count = items.length;
            var errorOrConflictCount = items.filter(function (i) {
                return isNonPendingFailureStatus(i.sync_status);
            }).length;
            var isOffline = navigator.onLine === false;

            if (badge) {
                if (count > 0) {
                    badge.textContent =
                        errorOrConflictCount > 0
                            ? count + " queued (" + errorOrConflictCount + " issue)"
                            : count + " queued";
                    badge.classList.remove("d-none");
                } else {
                    badge.classList.add("d-none");
                }
            }

            if (queuedCountBadge) {
                queuedCountBadge.textContent = count + (count === 1 ? " item" : " items");
            }

            renderQueueReviewPanel(userId, items);

            if (!banner || !bannerText) return;

            if (explicitSyncOutcome && explicitSyncOutcome.message) {
                var variant =
                    explicitSyncOutcome.status === "success"
                        ? "success"
                        : explicitSyncOutcome.status === "partial" ||
                          explicitSyncOutcome.status === "snapshot_refresh_failed"
                        ? "warning"
                        : explicitSyncOutcome.status === "sync_in_progress"
                        ? "info"
                        : "danger";
                setBannerState(variant, explicitSyncOutcome.message);
                return;
            }

            if (errorOrConflictCount > 0) {
                setBannerState(
                    "danger",
                    errorOrConflictCount +
                        " queued offline mutation(s) failed or conflicted during sync. Review, retry, or discard them below."
                );
            } else if (isOffline) {
                setBannerState(
                    "warning",
                    "You are offline. " +
                        (count > 0
                            ? count + " offline mutation(s) queued for synchronization."
                            : "New income and expense entries will be queued locally.")
                );
            } else if (count > 0) {
                setBannerState(
                    "info",
                    count + " queued offline change(s) ready to synchronize."
                );
            } else if (isOfflineShellPage()) {
                setBannerState(
                    "success",
                    "Connection available. All queued offline changes are synchronized."
                );
            } else {
                banner.classList.add("d-none");
            }
        });
    }

    function parseJsonResponseSafely(resp) {
        if (!resp) {
            return Promise.resolve({ __non_json: true, status_code: 0 });
        }
        if (resp.redirected && resp.url && resp.url.indexOf("/accounts/login") !== -1) {
            return Promise.resolve({
                __reauth_required: true,
                status_code: resp.status,
            });
        }
        if (resp.status === 401 || resp.status === 403) {
            return Promise.resolve({
                __reauth_required: true,
                status_code: resp.status,
            });
        }
        var contentType = (resp.headers && resp.headers.get && resp.headers.get("content-type")) || "";
        if (contentType.indexOf("application/json") === -1) {
            return Promise.resolve({
                __non_json: true,
                status_code: resp.status,
            });
        }
        return resp
            .json()
            .then(function (data) {
                return {
                    __ok: resp.ok,
                    status_code: resp.status,
                    data: data,
                };
            })
            .catch(function () {
                return {
                    __non_json: true,
                    status_code: resp.status,
                };
            });
    }

    /**
     * Verifies the authenticated server identity against `expectedUserId`
     * before an online tab resumes or synchronizes.
     * If the server reports a different authenticated user, immediately
     * invalidates the current tab and aborts.
     */
    function verifyServerIdentity(expectedUserId, options) {
        var opts = options || {};
        var key = String(expectedUserId || "").trim();
        var runGen = typeof opts.runGen === "number" ? opts.runGen : tabGeneration;

        if (!key || tabInvalidated) {
            return Promise.resolve({
                status: "tab_invalidated",
                verified: false,
                message: "Tab is invalidated or has no active user.",
            });
        }
        if (navigator.onLine === false) {
            return Promise.resolve({
                status: "offline",
                verified: false,
                message: "Browser is currently offline.",
            });
        }

        var baseStateUrl = getBodyAttr("data-sync-state-url") || "/api/sync/state/";
        var sep = baseStateUrl.indexOf("?") === -1 ? "?" : "&";
        var url = baseStateUrl + sep + "limit=1";
        var controller = createTrackedAbortController();
        var fetchOpts = {
            method: "GET",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        };
        if (controller) {
            fetchOpts.signal = controller.signal;
        }

        return fetch(url, fetchOpts)
            .then(function (resp) {
                releaseTrackedAbortController(controller);
                return parseJsonResponseSafely(resp);
            })
            .then(function (parsed) {
                if (!isGenerationValid(runGen, key)) {
                    return {
                        status: "tab_invalidated",
                        verified: false,
                        message: "Tab identity was superseded while checking server session.",
                    };
                }
                if (!parsed || parsed.__reauth_required) {
                    return {
                        status: "reauth_required",
                        verified: false,
                        message: "Session expired or unauthenticated. Please sign in to synchronize.",
                    };
                }
                if (parsed.__non_json || !parsed.__ok || !parsed.data) {
                    return {
                        status: "failed",
                        verified: false,
                        message: "Could not verify authenticated server identity.",
                    };
                }
                var data = parsed.data;
                if (String(data.user_id) !== key) {
                    invalidateCurrentTab("account_switched");
                    return {
                        status: "user_mismatch",
                        verified: false,
                        server_user_id: String(data.user_id),
                        message:
                            "Authenticated server user does not match active local profile. Tab locked and sync aborted to protect account isolation.",
                    };
                }
                return {
                    status: "verified",
                    verified: true,
                    user_id: String(data.user_id),
                    username: data.username || "",
                };
            })
            .catch(function () {
                releaseTrackedAbortController(controller);
                if (!isGenerationValid(runGen, key)) {
                    return {
                        status: "tab_invalidated",
                        verified: false,
                        message: "Tab identity was superseded.",
                    };
                }
                return {
                    status: "failed",
                    verified: false,
                    message: "Network error while verifying server identity.",
                };
            });
    }

    function pullServerSnapshot(userId, options) {
        var opts = options || {};
        var key = String(userId || "").trim();
        var runGen = typeof opts.runGen === "number" ? opts.runGen : tabGeneration;

        if (!key || tabInvalidated) {
            return Promise.resolve({ status: "tab_invalidated" });
        }
        if (navigator.onLine === false) {
            return Promise.resolve({ status: "offline" });
        }
        var baseStateUrl = getBodyAttr("data-sync-state-url") || "/api/sync/state/";
        var params = [];
        if (opts.limit) params.push("limit=" + encodeURIComponent(opts.limit));
        if (opts.offset) params.push("offset=" + encodeURIComponent(opts.offset));
        if (opts.since) params.push("since=" + encodeURIComponent(opts.since));
        var url =
            baseStateUrl +
            (params.length ? (baseStateUrl.indexOf("?") === -1 ? "?" : "&") + params.join("&") : "");

        var controller = createTrackedAbortController();
        var fetchOpts = {
            method: "GET",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        };
        if (controller) {
            fetchOpts.signal = controller.signal;
        }

        return fetch(url, fetchOpts)
            .then(function (resp) {
                releaseTrackedAbortController(controller);
                return parseJsonResponseSafely(resp);
            })
            .then(function (parsed) {
                if (!isGenerationValid(runGen, key)) {
                    return {
                        status: "tab_invalidated",
                        message: "Tab identity was superseded while downloading server snapshot.",
                    };
                }
                if (!parsed || parsed.__reauth_required) {
                    return {
                        status: "reauth_required",
                        message: "Session expired or unauthenticated. Please sign in to synchronize.",
                    };
                }
                if (parsed.__non_json || !parsed.__ok || !parsed.data) {
                    return {
                        status: "failed",
                        message: "Could not retrieve sync state from server.",
                    };
                }
                var data = parsed.data;
                if (String(data.user_id) !== key) {
                    invalidateCurrentTab("account_switched");
                    return {
                        status: "user_mismatch",
                        message:
                            "Authenticated server user does not match active local profile. Sync aborted to protect account isolation.",
                    };
                }

                if (opts.appendTransactions) {
                    return getStateSnapshot(key).then(function (existing) {
                        if (!isGenerationValid(runGen, key)) {
                            return { status: "tab_invalidated" };
                        }
                        if (existing && Array.isArray(existing.transactions)) {
                            var byId = {};
                            var merged = [];
                            existing.transactions.concat(data.transactions || []).forEach(function (tx) {
                                if (!byId[tx.id]) {
                                    byId[tx.id] = true;
                                    merged.push(tx);
                                }
                            });
                            data.transactions = merged;
                            data.returned_transactions_count = merged.length;
                        }
                        return saveStateSnapshot(key, data).then(function (saved) {
                            if (!saved) {
                                return {
                                    status: "failed",
                                    message: "Could not persist server snapshot to local storage.",
                                };
                            }
                            return { status: "success", snapshot: data };
                        });
                    });
                }

                return saveStateSnapshot(key, data).then(function (saved) {
                    if (!saved) {
                        return {
                            status: "failed",
                            message: "Could not persist server snapshot to local storage.",
                        };
                    }
                    return { status: "success", snapshot: data };
                });
            })
            .catch(function () {
                releaseTrackedAbortController(controller);
                if (!isGenerationValid(runGen, key)) {
                    return { status: "tab_invalidated" };
                }
                return {
                    status: "failed",
                    message: "Network error while fetching server snapshot.",
                };
            });
    }

    /**
     * Synchronizes the user's mutation queue sequentially in batches of at most
     * `MAX_SYNC_BATCH_SIZE` (100) mutations, preserving `queue_seq` dependency
     * order, coordinating across tabs, stopping dependent mutations on the same
     * entity when an earlier mutation fails, replacing acknowledged items that
     * were edited in flight with a new mutation ID, and verifying snapshot
     * refresh before reporting full success.
     */
    function flushMutationQueue(userId, options) {
        var opts = options || {};
        var key = String(userId || "").trim();

        if (tabInvalidated) {
            return Promise.resolve({
                status: "tab_invalidated",
                message: "This tab has been locked due to an account change or sign-out.",
            });
        }
        if (!key) {
            return Promise.resolve({
                status: "no_user",
                message: "No active user session for synchronization.",
            });
        }

        var activeCheck = getActiveIdentity();
        if (activeCheck.invalidated || (activeCheck.userId && String(activeCheck.userId) !== key)) {
            invalidateCurrentTab(activeCheck.reason || "account_switched");
            return Promise.resolve({
                status: "tab_invalidated",
                message: "Active account changed. Synchronization aborted.",
            });
        }

        if (navigator.onLine === false) {
            var offlineOutcome = {
                status: "offline",
                message: "Cannot synchronize while offline. Changes remain queued locally.",
            };
            return updateUiStatus(key, offlineOutcome).then(function () {
                return offlineOutcome;
            });
        }

        if (inTabSyncLocks[key]) {
            var inTabBusy = {
                status: "sync_in_progress",
                message: "Synchronization is already in progress in this tab.",
            };
            return Promise.resolve(inTabBusy);
        }

        if (!acquireCrossTabSyncLock(key)) {
            var crossTabBusy = {
                status: "sync_in_progress",
                message: "Synchronization is already in progress in another tab.",
            };
            return updateUiStatus(key, crossTabBusy).then(function () {
                return crossTabBusy;
            });
        }

        inTabSyncLocks[key] = true;
        var runGen = tabGeneration;
        var mutationsUrl = getBodyAttr("data-sync-mutations-url") || "/api/sync/mutations/";

        function releaseLocks() {
            delete inTabSyncLocks[key];
            releaseCrossTabSyncLock(key);
        }

        var precheckPromise = opts.requireIdentityPrecheck
            ? verifyServerIdentity(key, { runGen: runGen })
            : Promise.resolve({ status: "skipped", verified: true });

        return precheckPromise
            .then(function (precheck) {
                if (!isGenerationValid(runGen, key)) {
                    return {
                        status: "tab_invalidated",
                        message: "Tab identity was superseded before synchronization started.",
                    };
                }
                if (opts.requireIdentityPrecheck && !precheck.verified) {
                    if (precheck.status !== "user_mismatch") {
                        return updateUiStatus(key, precheck).then(function () {
                            return precheck;
                        });
                    }
                    return precheck;
                }

                return getQueuedMutations(key).then(function (items) {
                    if (!isGenerationValid(runGen, key)) {
                        return { status: "tab_invalidated" };
                    }

                    if (!items.length) {
                        return pullServerSnapshot(key, { runGen: runGen }).then(function (pullRes) {
                            if (!isGenerationValid(runGen, key)) {
                                return { status: "tab_invalidated" };
                            }
                            if (pullRes.status === "success") {
                                var emptyOutcome = {
                                    status: "success",
                                    applied_count: 0,
                                    error_count: 0,
                                    conflict_count: 0,
                                    blocked_count: 0,
                                    pending_count: 0,
                                    batches_processed: 0,
                                    message: "Offline snapshot is up to date.",
                                };
                                return updateUiStatus(key).then(function () {
                                    if (isOfflineShellPage()) {
                                        var id = getActiveIdentity();
                                        return renderOfflinePage(id.userId, id.username).then(
                                            function () {
                                                return emptyOutcome;
                                            }
                                        );
                                    }
                                    return emptyOutcome;
                                });
                            }
                            if (pullRes.status !== "user_mismatch") {
                                return updateUiStatus(key, pullRes).then(function () {
                                    return pullRes;
                                });
                            }
                            return pullRes;
                        });
                    }

                    var retryFailed = Boolean(opts.retryFailed);
                    var retryMutationId = opts.retryMutationId ? String(opts.retryMutationId) : "";
                    var retryTargetEntityId = "";

                    if (retryFailed && retryMutationId) {
                        items.forEach(function (it) {
                            if (it.mutation_id === retryMutationId) {
                                retryTargetEntityId = String(it.entity_id || "").trim();
                            }
                        });
                    }

                    var blockedEntities = {};
                    var preExistingFailedCount = 0;
                    var preExistingConflictCount = 0;
                    var newlyBlockedPromises = [];
                    var playableItems = [];

                    items.forEach(function (item) {
                        var entityKey = String(item.entity_id || "").trim();
                        var st = item.sync_status || "pending";

                        if (retryFailed) {
                            if (
                                !retryMutationId ||
                                item.mutation_id === retryMutationId ||
                                (retryTargetEntityId &&
                                    entityKey === retryTargetEntityId &&
                                    st === "blocked_by_dependency")
                            ) {
                                st = "pending";
                                item.sync_status = "pending";
                                delete item.last_error;
                            }
                        }

                        if (isNonPendingFailureStatus(st)) {
                            if (st === "conflict") {
                                preExistingConflictCount += 1;
                            } else {
                                preExistingFailedCount += 1;
                            }
                            if (entityKey) {
                                blockedEntities[entityKey] = true;
                            }
                            return;
                        }

                        if (entityKey && blockedEntities[entityKey]) {
                            var blockedMsg =
                                "Skipped because an earlier queued operation on the same record failed or conflicted.";
                            item.sync_status = "blocked_by_dependency";
                            item.last_error = blockedMsg;
                            preExistingFailedCount += 1;
                            newlyBlockedPromises.push(
                                updateQueueItemStatusIfUnchanged(
                                    key,
                                    item.mutation_id,
                                    item.local_version,
                                    item.local_fingerprint,
                                    {
                                        sync_status: "blocked_by_dependency",
                                        last_error: blockedMsg,
                                    }
                                )
                            );
                            return;
                        }

                        playableItems.push(item);
                    });

                    return Promise.all(newlyBlockedPromises).then(function () {
                        if (!playableItems.length) {
                            var noPlayableOutcome = {
                                status: "failed",
                                applied_count: 0,
                                error_count: preExistingFailedCount,
                                conflict_count: preExistingConflictCount,
                                blocked_count: newlyBlockedPromises.length,
                                batches_processed: 0,
                                message:
                                    preExistingFailedCount +
                                    preExistingConflictCount +
                                    " queued offline mutation(s) failed or conflicted. Review, retry, or discard them below.",
                            };
                            return updateUiStatus(key, noPlayableOutcome).then(function () {
                                return noPlayableOutcome;
                            });
                        }

                        var remainingQueue = playableItems.slice();
                        var totalApplied = 0;
                        var totalErrors = preExistingFailedCount;
                        var totalConflicts = preExistingConflictCount;
                        var totalBlocked = newlyBlockedPromises.length;
                        var totalFollowupsCreated = 0;
                        var batchesProcessed = 0;
                        var allBatchResults = [];
                        var estimatedTotalBatches = Math.ceil(
                            remainingQueue.length / MAX_SYNC_BATCH_SIZE
                        );

                        function processNextBatch() {
                            if (!isGenerationValid(runGen, key)) {
                                return Promise.resolve({
                                    status: "tab_invalidated",
                                    applied_count: totalApplied,
                                    error_count: totalErrors,
                                    conflict_count: totalConflicts,
                                    blocked_count: totalBlocked,
                                    batches_processed: batchesProcessed,
                                });
                            }

                            if (navigator.onLine === false) {
                                var midOfflineOutcome = {
                                    status: totalApplied > 0 ? "partial" : "offline",
                                    applied_count: totalApplied,
                                    error_count: totalErrors,
                                    conflict_count: totalConflicts,
                                    blocked_count: totalBlocked,
                                    batches_processed: batchesProcessed,
                                    message:
                                        "Connection lost during synchronization. Remaining queued changes are preserved locally.",
                                };
                                return updateUiStatus(key, midOfflineOutcome).then(function () {
                                    return midOfflineOutcome;
                                });
                            }

                            if (!renewCrossTabSyncLock(key)) {
                                var lockLostOutcome = {
                                    status: totalApplied > 0 ? "partial" : "sync_in_progress",
                                    applied_count: totalApplied,
                                    error_count: totalErrors,
                                    conflict_count: totalConflicts,
                                    blocked_count: totalBlocked,
                                    batches_processed: batchesProcessed,
                                    message:
                                        "Synchronization lock transferred to another tab. Remaining items are preserved.",
                                };
                                return Promise.resolve(lockLostOutcome);
                            }

                            var crossBatchBlockedPromises = [];
                            var nextEligibleQueue = [];
                            remainingQueue.forEach(function (candidate) {
                                var candEntity = String(candidate.entity_id || "").trim();
                                if (candEntity && blockedEntities[candEntity]) {
                                    var depMsg =
                                        "Skipped because an earlier queued operation on the same record failed or conflicted.";
                                    totalBlocked += 1;
                                    totalErrors += 1;
                                    allBatchResults.push({
                                        mutation_id: candidate.mutation_id,
                                        entity_id: candEntity,
                                        status: "blocked_by_dependency",
                                        error: depMsg,
                                        message: depMsg,
                                    });
                                    crossBatchBlockedPromises.push(
                                        updateQueueItemStatusIfUnchanged(
                                            key,
                                            candidate.mutation_id,
                                            candidate.local_version,
                                            candidate.local_fingerprint,
                                            {
                                                sync_status: "blocked_by_dependency",
                                                last_error: depMsg,
                                            }
                                        )
                                    );
                                } else {
                                    nextEligibleQueue.push(candidate);
                                }
                            });
                            remainingQueue = nextEligibleQueue;

                            return Promise.all(crossBatchBlockedPromises).then(function () {
                                if (!remainingQueue.length) {
                                    return finalizeSyncRun();
                                }

                                var currentBatch = remainingQueue.slice(0, MAX_SYNC_BATCH_SIZE);
                                remainingQueue = remainingQueue.slice(MAX_SYNC_BATCH_SIZE);
                                var isFinalBatch = remainingQueue.length === 0;
                                batchesProcessed += 1;
                                var totalBatchesNow = Math.max(
                                    estimatedTotalBatches,
                                    batchesProcessed +
                                        Math.ceil(remainingQueue.length / MAX_SYNC_BATCH_SIZE)
                                );

                                setBannerState(
                                    "info",
                                    "Synchronizing batch " +
                                        batchesProcessed +
                                        " of " +
                                        totalBatchesNow +
                                        " (" +
                                        currentBatch.length +
                                        " item(s))..."
                                );
                                if (typeof opts.onProgress === "function") {
                                    try {
                                        opts.onProgress({
                                            batch_index: batchesProcessed,
                                            total_batches: totalBatchesNow,
                                            batch_size: currentBatch.length,
                                            applied_so_far: totalApplied,
                                        });
                                    } catch (e) {}
                                }

                                return markBatchInFlight(key, currentBatch).then(function (
                                    inFlightRecords
                                ) {
                                    var sentMetaById = {};
                                    var wireMutations = inFlightRecords.map(function (baseReq) {
                                        sentMetaById[baseReq.mutation_id] = {
                                            mutation_id: baseReq.mutation_id,
                                            local_version: baseReq.local_version,
                                            local_fingerprint: baseReq.local_fingerprint,
                                            entity_id: String(baseReq.entity_id || "").trim(),
                                            base_revision: baseReq.base_revision,
                                        };
                                        return {
                                            mutation_id: baseReq.mutation_id,
                                            queue_seq: baseReq.queue_seq,
                                            operation: baseReq.operation,
                                            entity_id: baseReq.entity_id,
                                            base_revision: baseReq.base_revision,
                                            queued_at: baseReq.queued_at,
                                            payload: baseReq.payload,
                                        };
                                    });
                                    var batchMutationIds = Object.keys(sentMetaById);

                                    var controller = createTrackedAbortController();
                                    var fetchOpts = {
                                        method: "POST",
                                        credentials: "same-origin",
                                        headers: {
                                            "Content-Type": "application/json",
                                            Accept: "application/json",
                                            "X-CSRFToken": getCsrfToken(),
                                        },
                                        body: JSON.stringify({
                                            expected_user_id: parseInt(key, 10),
                                            is_final_batch: isFinalBatch,
                                            mutations: wireMutations,
                                        }),
                                    };
                                    if (controller) {
                                        fetchOpts.signal = controller.signal;
                                    }

                                    return fetch(mutationsUrl, fetchOpts)
                                        .then(function (resp) {
                                            releaseTrackedAbortController(controller);
                                            return parseJsonResponseSafely(resp);
                                        })
                                        .then(function (parsed) {
                                            if (!isGenerationValid(runGen, key)) {
                                                clearInMemoryInFlight(key, batchMutationIds);
                                                return {
                                                    status: "tab_invalidated",
                                                    applied_count: totalApplied,
                                                    error_count: totalErrors,
                                                    conflict_count: totalConflicts,
                                                    blocked_count: totalBlocked,
                                                    batches_processed: batchesProcessed - 1,
                                                };
                                            }

                                            if (!parsed || parsed.__reauth_required) {
                                                clearInMemoryInFlight(key, batchMutationIds);
                                                var reauthOutcome = {
                                                    status: "reauth_required",
                                                    applied_count: totalApplied,
                                                    error_count: totalErrors,
                                                    conflict_count: totalConflicts,
                                                    blocked_count: totalBlocked,
                                                    batches_processed: batchesProcessed - 1,
                                                    message:
                                                        "Authentication required. Please sign in again before synchronizing queued offline changes.",
                                                };
                                                return updateUiStatus(key, reauthOutcome).then(
                                                    function () {
                                                        return reauthOutcome;
                                                    }
                                                );
                                            }

                                            if (parsed.__non_json) {
                                                clearInMemoryInFlight(key, batchMutationIds);
                                                var nonJsonOutcome = {
                                                    status: totalApplied > 0 ? "partial" : "failed",
                                                    applied_count: totalApplied,
                                                    error_count: totalErrors,
                                                    conflict_count: totalConflicts,
                                                    blocked_count: totalBlocked,
                                                    batches_processed: batchesProcessed - 1,
                                                    message:
                                                        "Unexpected non-JSON response from server. Unacknowledged queued changes remain stored locally.",
                                                };
                                                return updateUiStatus(key, nonJsonOutcome).then(
                                                    function () {
                                                        return nonJsonOutcome;
                                                    }
                                                );
                                            }

                                            var data = parsed.data || {};
                                            if (
                                                parsed.status_code === 409 ||
                                                data.batch_status === "user_mismatch"
                                            ) {
                                                clearInMemoryInFlight(key, batchMutationIds);
                                                invalidateCurrentTab("account_switched");
                                                return {
                                                    status: "user_mismatch",
                                                    applied_count: totalApplied,
                                                    error_count: totalErrors,
                                                    conflict_count: totalConflicts,
                                                    blocked_count: totalBlocked,
                                                    batches_processed: batchesProcessed - 1,
                                                    message:
                                                        data.error ||
                                                        "Authenticated user mismatch detected. Queued changes were not uploaded.",
                                                };
                                            }

                                            if (!parsed.__ok || !Array.isArray(data.results)) {
                                                clearInMemoryInFlight(key, batchMutationIds);
                                                var errOutcome = {
                                                    status: totalApplied > 0 ? "partial" : "failed",
                                                    applied_count: totalApplied,
                                                    error_count: totalErrors,
                                                    conflict_count: totalConflicts,
                                                    blocked_count: totalBlocked,
                                                    batches_processed: batchesProcessed - 1,
                                                    message:
                                                        data.error ||
                                                        "Synchronization batch request failed. Queued changes remain stored locally.",
                                                };
                                                return updateUiStatus(key, errOutcome).then(
                                                    function () {
                                                        return errOutcome;
                                                    }
                                                );
                                            }

                                            var acknowledgedIds = {};
                                            var dbOps = data.results.map(function (r) {
                                                var mid = r.mutation_id;
                                                var meta = sentMetaById[mid];
                                                if (!meta) return Promise.resolve();

                                                acknowledgedIds[mid] = true;
                                                allBatchResults.push(r);

                                                if (r.status === "applied" || r.idempotent_replay) {
                                                    totalApplied += 1;
                                                    // Also update any matching entity items in remainingQueue if their base_revision was null
                                                    if (
                                                        meta.entity_id &&
                                                        typeof r.revision === "number"
                                                    ) {
                                                        remainingQueue.forEach(function (rem) {
                                                            if (
                                                                String(rem.entity_id || "").trim() ===
                                                                    meta.entity_id &&
                                                                (rem.base_revision === null ||
                                                                    rem.base_revision === undefined ||
                                                                    rem.base_revision ===
                                                                        meta.base_revision)
                                                            ) {
                                                                rem.base_revision = r.revision;
                                                                if (
                                                                    rem.operation ===
                                                                    "create_transaction"
                                                                ) {
                                                                    rem.operation =
                                                                        "update_transaction";
                                                                }
                                                                rem.local_fingerprint =
                                                                    computeMutationFingerprint(rem);
                                                            }
                                                        });
                                                    }
                                                    return acknowledgeAppliedMutation(
                                                        key,
                                                        mid,
                                                        meta.local_version,
                                                        meta.local_fingerprint,
                                                        r
                                                    ).then(function (ackRes) {
                                                        if (
                                                            ackRes &&
                                                            ackRes.replaced_with_followup
                                                        ) {
                                                            totalFollowupsCreated += 1;
                                                        }
                                                        return ackRes;
                                                    });
                                                }

                                                if (r.status === "conflict") {
                                                    totalConflicts += 1;
                                                } else if (r.status === "blocked_by_dependency") {
                                                    totalBlocked += 1;
                                                    totalErrors += 1;
                                                } else {
                                                    totalErrors += 1;
                                                }

                                                if (meta.entity_id) {
                                                    blockedEntities[meta.entity_id] = true;
                                                }

                                                var errMsg =
                                                    r.error ||
                                                    r.message ||
                                                    "Synchronization failed.";
                                                return updateQueueItemStatusIfUnchanged(
                                                    key,
                                                    mid,
                                                    meta.local_version,
                                                    meta.local_fingerprint,
                                                    {
                                                        sync_status: r.status || "error",
                                                        last_error: errMsg,
                                                        server_revision:
                                                            r.server_revision !== undefined
                                                                ? r.server_revision
                                                                : null,
                                                        server_is_deleted:
                                                            r.server_is_deleted !== undefined
                                                                ? r.server_is_deleted
                                                                : null,
                                                        server_record:
                                                            r.server_record !== undefined
                                                                ? r.server_record
                                                                : null,
                                                    }
                                                );
                                            });

                                            currentBatch.forEach(function (sentItem) {
                                                if (!acknowledgedIds[sentItem.mutation_id]) {
                                                    clearInMemoryInFlight(key, [
                                                        sentItem.mutation_id,
                                                    ]);
                                                    var unackMeta =
                                                        sentMetaById[sentItem.mutation_id];
                                                    if (unackMeta && unackMeta.entity_id) {
                                                        blockedEntities[unackMeta.entity_id] = true;
                                                    }
                                                    totalErrors += 1;
                                                }
                                            });

                                            return Promise.all(dbOps).then(function () {
                                                broadcastSignal({
                                                    type: "queue_updated",
                                                    user_id: key,
                                                });
                                                return processNextBatch();
                                            });
                                        })
                                        .catch(function () {
                                            releaseTrackedAbortController(controller);
                                            clearInMemoryInFlight(key, batchMutationIds);
                                            if (!isGenerationValid(runGen, key)) {
                                                return { status: "tab_invalidated" };
                                            }
                                            var netOutcome = {
                                                status: totalApplied > 0 ? "partial" : "failed",
                                                applied_count: totalApplied,
                                                error_count: totalErrors,
                                                conflict_count: totalConflicts,
                                                blocked_count: totalBlocked,
                                                batches_processed: batchesProcessed - 1,
                                                message:
                                                    "Network error during synchronization. Unacknowledged and remaining queued changes remain safely stored locally.",
                                            };
                                            return updateUiStatus(key, netOutcome).then(function () {
                                                return netOutcome;
                                            });
                                        });
                                });
                            });
                        }

                        function finalizeSyncRun() {
                            if (!isGenerationValid(runGen, key)) {
                                return Promise.resolve({ status: "tab_invalidated" });
                            }

                            return Promise.all([
                                pullServerSnapshot(key, { runGen: runGen }),
                                getQueuedMutations(key),
                            ]).then(function (finalResults) {
                                var pullRes = finalResults[0];
                                var remainingInQueue = finalResults[1] || [];

                                if (!isGenerationValid(runGen, key)) {
                                    return { status: "tab_invalidated" };
                                }
                                if (pullRes.status === "user_mismatch") {
                                    return pullRes;
                                }

                                var pendingRemainingCount = remainingInQueue.filter(function (it) {
                                    return !isNonPendingFailureStatus(it.sync_status);
                                }).length;

                                var allQueueSucceeded =
                                    totalErrors === 0 &&
                                    totalConflicts === 0 &&
                                    totalBlocked === 0;
                                var finalStatus = "";
                                var message = "";

                                if (
                                    allQueueSucceeded &&
                                    (pendingRemainingCount > 0 || totalFollowupsCreated > 0)
                                ) {
                                    finalStatus = "partial";
                                    message =
                                        "Applied " +
                                        totalApplied +
                                        " offline change(s); " +
                                        pendingRemainingCount +
                                        " newer local edit(s) made during synchronization remain queued.";
                                } else if (allQueueSucceeded) {
                                    if (pullRes.status === "success") {
                                        finalStatus = "success";
                                        message =
                                            "Synchronized " +
                                            totalApplied +
                                            " offline change(s) across " +
                                            batchesProcessed +
                                            " batch(es) with the server.";
                                    } else {
                                        finalStatus = "snapshot_refresh_failed";
                                        message =
                                            "Uploaded " +
                                            totalApplied +
                                            " offline change(s), but failed to refresh the local snapshot from the server (" +
                                            (pullRes.message || pullRes.status) +
                                            ").";
                                    }
                                } else if (totalApplied > 0) {
                                    finalStatus = "partial";
                                    message =
                                        "Partial sync: " +
                                        totalApplied +
                                        " applied, " +
                                        totalConflicts +
                                        " conflict(s), " +
                                        totalErrors +
                                        " error(s)" +
                                        (totalBlocked > 0
                                            ? " (" + totalBlocked + " blocked by dependency)"
                                            : "") +
                                        ". Unapplied items remain in your queue for review.";
                                } else {
                                    finalStatus = "failed";
                                    message =
                                        "Synchronization failed (" +
                                        totalConflicts +
                                        " conflict(s), " +
                                        totalErrors +
                                        " error(s)). Review the queued items below.";
                                }

                                var outcome = {
                                    status: finalStatus,
                                    snapshot_refresh_status: pullRes.status,
                                    applied_count: totalApplied,
                                    error_count: totalErrors,
                                    conflict_count: totalConflicts,
                                    blocked_count: totalBlocked,
                                    pending_count: pendingRemainingCount,
                                    followup_count: totalFollowupsCreated,
                                     has_pending_followup:
                                        pendingRemainingCount > 0 || totalFollowupsCreated > 0,
                                    batches_processed: batchesProcessed,
                                    results: allBatchResults,
                                    message: message,
                                };

                                return updateUiStatus(key, outcome).then(function () {
                                    if (isOfflineShellPage() && isGenerationValid(runGen, key)) {
                                        var id = getActiveIdentity();
                                        return renderOfflinePage(id.userId, id.username).then(
                                            function () {
                                                return outcome;
                                            }
                                        );
                                    }
                                    return outcome;
                                });
                            });
                        }

                        return processNextBatch();
                    });
                });
            })
            .then(
                function (res) {
                    releaseLocks();
                    return res;
                },
                function (err) {
                    releaseLocks();
                    throw err;
                }
            );
    }

    function enqueueTransactionForm(formElement) {
        if (tabInvalidated) return Promise.resolve(null);
        var identity = getActiveIdentity();
        if (!identity.userId || identity.invalidated) return Promise.resolve(null);

        var action =
            (formElement.querySelector('[name="action"]') || {}).value || "create_transaction";
        var entityId =
            (formElement.querySelector('[name="transaction_id"]') || {}).value || generateUuid();
        var baseRevRaw = (formElement.querySelector('[name="expected_revision"]') || {}).value;
        var baseRevision = null;
        if (baseRevRaw !== undefined && baseRevRaw !== null && String(baseRevRaw).trim() !== "") {
            var parsedRev = parseInt(String(baseRevRaw).trim(), 10);
            if (!isNaN(parsedRev) && parsedRev >= 1) {
                baseRevision = parsedRev;
            }
        }

        var walletSelect = formElement.querySelector('[name="wallet"]');
        var selectedWalletOpt =
            walletSelect && walletSelect.selectedOptions && walletSelect.selectedOptions.length
                ? walletSelect.selectedOptions[0]
                : null;
        var sharedSpaceId = selectedWalletOpt
            ? selectedWalletOpt.getAttribute("data-shared-space-id") || null
            : null;
        var scopeVal =
            (formElement.querySelector('[name="scope"]') || {}).value ||
            (selectedWalletOpt ? selectedWalletOpt.getAttribute("data-scope") : "") ||
            "personal";
        var paidByRaw = (formElement.querySelector('[name="paid_by"]') || {}).value || "";
        var manualRateRaw = (formElement.querySelector('[name="manual_rate"]') || {}).value || "";

        var mutation = {
            mutation_id: generateUuid(),
            operation: action,
            entity_id: entityId,
            base_revision: baseRevision,
            sync_status: "pending",
            queued_at: new Date().toISOString(),
            payload: {
                transaction_type: (formElement.querySelector('[name="transaction_type"]') || {})
                    .value,
                amount: ((formElement.querySelector('[name="amount"]') || {}).value || "").trim(),
                wallet_id: (walletSelect || {}).value,
                category_id: (formElement.querySelector('[name="category"]') || {}).value || null,
                transaction_date: (formElement.querySelector('[name="transaction_date"]') || {})
                    .value,
                scope: scopeVal,
                shared_space_id: scopeVal === "shared" ? sharedSpaceId : null,
                paid_by_id: paidByRaw ? parseInt(paidByRaw, 10) : null,
                manual_rate: manualRateRaw.trim() || null,
                description: (formElement.querySelector('[name="description"]') || {}).value || "",
                notes: (formElement.querySelector('[name="notes"]') || {}).value || "",
            },
        };

        return saveMutationToQueue(identity.userId, mutation).then(function (saved) {
            if (!saved) return null;
            updateUiStatus(identity.userId);
            var modalEl = document.getElementById("transactionModal");
            if (modalEl && window.bootstrap && window.bootstrap.Modal) {
                var instance = window.bootstrap.Modal.getInstance(modalEl);
                if (instance) instance.hide();
            }
            return saved;
        });
    }

    function loadMutationIntoOfflineForm(userId, item) {
        if (tabInvalidated) return;
        var form = document.getElementById("offline-entry-form");
        if (!form || !item) return;

        var source = item.staged_followup || item;
        var p = source.payload || item.payload;
        if (!p) return;

        form.setAttribute("data-editing-mutation-id", item.mutation_id);
        form.setAttribute(
            "data-editing-operation",
            source.operation || item.operation || "create_transaction"
        );
        form.setAttribute("data-editing-entity-id", item.entity_id || "");
        form.setAttribute(
            "data-editing-base-revision",
            item.base_revision !== null && item.base_revision !== undefined
                ? String(item.base_revision)
                : ""
        );

        var typeEl = document.getElementById("offline-tx-type");
        var dateEl = document.getElementById("offline-tx-date");
        var walletEl = document.getElementById("offline-tx-wallet");
        var amountEl = document.getElementById("offline-tx-amount");
        var catEl = document.getElementById("offline-tx-category");
        var scopeEl = document.getElementById("offline-tx-scope");
        var spaceEl = document.getElementById("offline-tx-shared-space");
        var paidByEl = document.getElementById("offline-tx-paid-by");
        var manualRateEl = document.getElementById("offline-tx-manual-rate");
        var descEl = document.getElementById("offline-tx-description");
        var notesEl = document.getElementById("offline-tx-notes");
        var submitBtn = document.getElementById("btn-offline-queue-submit");

        if (typeEl && p.transaction_type) typeEl.value = p.transaction_type;
        if (dateEl && p.transaction_date) dateEl.value = p.transaction_date;
        if (walletEl && p.wallet_id) walletEl.value = p.wallet_id;
        if (amountEl && p.amount) amountEl.value = p.amount;
        if (catEl) catEl.value = p.category_id || "";
        if (scopeEl && p.scope) scopeEl.value = p.scope;
        if (spaceEl) spaceEl.value = p.shared_space_id || "";
        if (paidByEl) paidByEl.value = p.paid_by_id ? String(p.paid_by_id) : "";
        if (manualRateEl) manualRateEl.value = p.manual_rate || "";
        if (descEl) descEl.value = p.description || "";
        if (notesEl) notesEl.value = p.notes || "";
        if (submitBtn) submitBtn.textContent = "Update queued transaction";
        if (typeof form.scrollIntoView === "function") {
            form.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
    }

    function renderOfflinePage(userId, username) {
        if (!isOfflineShellPage() || tabInvalidated) return Promise.resolve();
        var runGen = tabGeneration;

        var userBadge = document.getElementById("offline-active-user-badge");
        var noSnapshotAlert = document.getElementById("offline-no-snapshot-alert");
        var lastSyncBadge = document.getElementById("offline-last-sync-badge");
        var boundedNotice = document.getElementById("offline-bounded-notice");
        var boundedNoticeText = document.getElementById("offline-bounded-notice-text");
        var loadMoreBtn = document.getElementById("btn-offline-load-more");

        if (!userId) {
            if (userBadge) {
                userBadge.textContent = "No active offline profile loaded";
            }
            if (noSnapshotAlert) {
                noSnapshotAlert.classList.remove("d-none");
            }
            return Promise.resolve();
        }

        offlineRenderedUserId = String(userId).trim();

        return Promise.all([getStateSnapshot(userId), getQueuedMutations(userId)]).then(function (
            results
        ) {
            if (!isGenerationValid(runGen, userId)) {
                return;
            }
            var snapshot = results[0];
            var queuedItems = results[1] || [];

            var displayUsername = (snapshot && snapshot.username) || username || "User #" + userId;
            if (userBadge) {
                userBadge.textContent = "Offline profile: " + displayUsername + " (ID " + userId + ")";
            }

            if (lastSyncBadge) {
                lastSyncBadge.textContent =
                    snapshot && snapshot.last_sync_at
                        ? "Last confirmed sync: " + snapshot.last_sync_at
                        : "Last confirmed sync: Not yet recorded";
            }

            if (!snapshot) {
                if (noSnapshotAlert) noSnapshotAlert.classList.remove("d-none");
            } else {
                if (noSnapshotAlert) noSnapshotAlert.classList.add("d-none");
            }

            if (boundedNotice && boundedNoticeText) {
                var totalCount =
                    snapshot && typeof snapshot.total_transactions_count === "number"
                        ? snapshot.total_transactions_count
                        : snapshot && Array.isArray(snapshot.transactions)
                        ? snapshot.transactions.length
                        : 0;
                var loadedCount =
                    snapshot && Array.isArray(snapshot.transactions)
                        ? snapshot.transactions.length
                        : 0;
                var hasMore = Boolean(snapshot && snapshot.has_more_transactions);

                if (snapshot && (hasMore || snapshot.is_bounded_snapshot || totalCount > loadedCount)) {
                    boundedNotice.classList.remove("d-none");
                    boundedNoticeText.textContent =
                        "Bounded local snapshot: showing " +
                        loadedCount +
                        " of " +
                        totalCount +
                        " total server transactions.";
                    if (loadMoreBtn) {
                        if (hasMore && navigator.onLine !== false) {
                            loadMoreBtn.classList.remove("d-none");
                            loadMoreBtn.setAttribute(
                                "data-next-offset",
                                String(snapshot.next_offset || loadedCount)
                            );
                        } else {
                            loadMoreBtn.classList.add("d-none");
                        }
                    }
                } else {
                    boundedNotice.classList.add("d-none");
                }
            }

            populateOfflineFormControls(snapshot);
            renderOfflineQueuedList(userId, queuedItems);
            renderOfflineWallets(snapshot ? snapshot.wallets : []);
            renderOfflineCategories(snapshot ? snapshot.categories : []);
            renderOfflineTransactions(userId, snapshot ? snapshot.transactions : []);
        });
    }

    function populateOfflineFormControls(snapshot) {
        if (tabInvalidated) return;
        var walletSelect = document.getElementById("offline-tx-wallet");
        var catSelect = document.getElementById("offline-tx-category");
        var scopeSelect = document.getElementById("offline-tx-scope");
        var spaceSelect = document.getElementById("offline-tx-shared-space");
        var paidBySelect = document.getElementById("offline-tx-paid-by");
        var dateInput = document.getElementById("offline-tx-date");

        if (dateInput && !dateInput.value) {
            dateInput.value = new Date().toISOString().slice(0, 10);
        }

        if (walletSelect) {
            var currentWallet = walletSelect.value;
            while (walletSelect.firstChild) {
                walletSelect.removeChild(walletSelect.firstChild);
            }
            var defaultOpt = document.createElement("option");
            defaultOpt.value = "";
            defaultOpt.textContent = "Select wallet";
            walletSelect.appendChild(defaultOpt);

            var wallets = (snapshot && snapshot.wallets) || [];
            wallets
                .filter(function (w) {
                    return !w.is_archived;
                })
                .forEach(function (w) {
                    var opt = document.createElement("option");
                    opt.value = w.id;
                    opt.textContent =
                        w.name + " (" + w.currency_code + " — " + w.ownership_scope + ")";
                    opt.setAttribute("data-decimal-places", String(w.decimal_places));
                    opt.setAttribute("data-currency-code", w.currency_code);
                    opt.setAttribute("data-scope", w.ownership_scope);
                    if (w.shared_space_id) {
                        opt.setAttribute("data-shared-space-id", w.shared_space_id);
                    }
                    walletSelect.appendChild(opt);
                });
            if (currentWallet) walletSelect.value = currentWallet;
        }

        if (catSelect) {
            var currentCat = catSelect.value;
            while (catSelect.firstChild) {
                catSelect.removeChild(catSelect.firstChild);
            }
            var uncatOpt = document.createElement("option");
            uncatOpt.value = "";
            uncatOpt.textContent = "Uncategorized";
            catSelect.appendChild(uncatOpt);

            var categories = (snapshot && snapshot.categories) || [];
            categories
                .filter(function (c) {
                    return !c.is_archived;
                })
                .forEach(function (c) {
                    var opt = document.createElement("option");
                    opt.value = c.id;
                    opt.textContent = c.name + " (" + c.scope + ")";
                    catSelect.appendChild(opt);
                });
            if (currentCat) catSelect.value = currentCat;
        }

        if (spaceSelect) {
            var currentSpace = spaceSelect.value;
            while (spaceSelect.firstChild) {
                spaceSelect.removeChild(spaceSelect.firstChild);
            }
            var autoOpt = document.createElement("option");
            autoOpt.value = "";
            autoOpt.textContent = "Auto-detect from wallet";
            spaceSelect.appendChild(autoOpt);

            var spaces = (snapshot && snapshot.shared_spaces) || [];
            spaces.forEach(function (sp) {
                var opt = document.createElement("option");
                opt.value = sp.id;
                opt.textContent = sp.name + " (" + sp.base_currency_code + ")";
                spaceSelect.appendChild(opt);
            });
            if (currentSpace) {
                spaceSelect.value = currentSpace;
            } else if (snapshot && snapshot.active_shared_space_id) {
                spaceSelect.value = snapshot.active_shared_space_id;
            }
        }

        if (paidBySelect) {
            var currentPaidBy = paidBySelect.value;
            while (paidBySelect.firstChild) {
                paidBySelect.removeChild(paidBySelect.firstChild);
            }
            var selfOpt = document.createElement("option");
            selfOpt.value = snapshot && snapshot.user_id ? String(snapshot.user_id) : "";
            selfOpt.textContent =
                snapshot && snapshot.username
                    ? snapshot.username + " (current user)"
                    : "Current user (default)";
            paidBySelect.appendChild(selfOpt);

            var seenMembers = {};
            if (snapshot && snapshot.user_id) {
                seenMembers[String(snapshot.user_id)] = true;
            }
            var sharedSpaces = (snapshot && snapshot.shared_spaces) || [];
            sharedSpaces.forEach(function (sp) {
                (sp.members || []).forEach(function (m) {
                    var mid = String(m.id);
                    if (!seenMembers[mid]) {
                        seenMembers[mid] = true;
                        var opt = document.createElement("option");
                        opt.value = mid;
                        opt.textContent = m.username + " (" + sp.name + ")";
                        paidBySelect.appendChild(opt);
                    }
                });
            });
            if (currentPaidBy) paidBySelect.value = currentPaidBy;
        }

        if (
            scopeSelect &&
            snapshot &&
            snapshot.active_scope &&
            !scopeSelect.getAttribute("data-user-touched")
        ) {
            scopeSelect.value = snapshot.active_scope;
        }
    }

    function renderOfflineQueuedList(userId, items) {
        if (tabInvalidated) return;
        var container = document.getElementById("offline-queued-list");
        var countBadge = document.getElementById("offline-queued-count-badge");
        if (countBadge) {
            countBadge.textContent = items.length + (items.length === 1 ? " item" : " items");
        }
        if (!container) return;

        while (container.firstChild) {
            container.removeChild(container.firstChild);
        }

        if (!items.length) {
            var empty = document.createElement("p");
            empty.className = "text-secondary small mb-0";
            empty.textContent = "No pending offline mutations.";
            container.appendChild(empty);
            return;
        }

        items.forEach(function (item) {
            container.appendChild(buildQueuedMutationCard(userId, item));
        });
    }

    function renderOfflineWallets(wallets) {
        if (tabInvalidated) return;
        var container = document.getElementById("offline-wallets-list");
        if (!container) return;

        while (container.firstChild) {
            container.removeChild(container.firstChild);
        }

        var activeWallets = (wallets || []).filter(function (w) {
            return !w.is_archived;
        });

        if (!activeWallets.length) {
            var empty = document.createElement("p");
            empty.className = "text-secondary small mb-0";
            empty.textContent = "No synced wallets available in local snapshot.";
            container.appendChild(empty);
            return;
        }

        activeWallets.forEach(function (w) {
            var row = document.createElement("div");
            row.className =
                "border rounded p-2 d-flex align-items-center justify-content-between gap-2";

            var left = document.createElement("div");
            var nameEl = document.createElement("div");
            nameEl.className = "small fw-semibold";
            nameEl.textContent = w.name;
            var metaEl = document.createElement("div");
            metaEl.className = "text-secondary small";
            metaEl.textContent =
                w.ownership_scope +
                " | " +
                w.currency_code +
                " (" +
                w.decimal_places +
                " dec) | rev " +
                w.revision;
            left.appendChild(nameEl);
            left.appendChild(metaEl);

            var right = document.createElement("div");
            right.className = "small fw-semibold font-monospace";
            right.textContent = w.current_balance + " " + w.currency_code;

            row.appendChild(left);
            row.appendChild(right);
            container.appendChild(row);
        });
    }

    function renderOfflineCategories(categories) {
        if (tabInvalidated) return;
        var container = document.getElementById("offline-categories-list");
        if (!container) return;

        while (container.firstChild) {
            container.removeChild(container.firstChild);
        }

        var activeCats = (categories || []).filter(function (c) {
            return !c.is_archived;
        });

        if (!activeCats.length) {
            var empty = document.createElement("p");
            empty.className = "text-secondary small mb-0";
            empty.textContent = "No synced categories available in local snapshot.";
            container.appendChild(empty);
            return;
        }

        activeCats.forEach(function (c) {
            var badge = document.createElement("span");
            badge.className = "badge bg-light text-dark border fw-normal";
            badge.textContent = c.name + " (" + c.kind + ", " + c.scope + ")";
            container.appendChild(badge);
        });
    }

    function renderOfflineTransactions(userId, transactions) {
        if (tabInvalidated) return;
        var tbody = document.getElementById("offline-transactions-tbody");
        var summaryBadge = document.getElementById("offline-tx-summary-badge");
        if (!tbody) return;

        while (tbody.firstChild) {
            tbody.removeChild(tbody.firstChild);
        }

        var visibleTxs = (transactions || []).filter(function (tx) {
            return !tx.is_deleted;
        });

        if (summaryBadge) {
            summaryBadge.textContent = visibleTxs.length + " synced";
        }

        if (!visibleTxs.length) {
            var trEmpty = document.createElement("tr");
            var tdEmpty = document.createElement("td");
            tdEmpty.colSpan = 6;
            tdEmpty.className = "text-secondary small py-3 text-center";
            tdEmpty.textContent = "No synced transactions available in local storage.";
            trEmpty.appendChild(tdEmpty);
            tbody.appendChild(trEmpty);
            return;
        }

        visibleTxs.forEach(function (tx) {
            var tr = document.createElement("tr");

            var tdDate = document.createElement("td");
            tdDate.className = "small text-nowrap";
            tdDate.textContent = tx.transaction_date;
            tr.appendChild(tdDate);

            var tdDesc = document.createElement("td");
            var descMain = document.createElement("div");
            descMain.className = "small fw-medium";
            descMain.textContent = tx.description || tx.category_name || tx.transaction_type;
            var descSub = document.createElement("div");
            descSub.className = "text-secondary small";
            descSub.textContent =
                (tx.category_name || "Uncategorized") + " | rev " + String(tx.revision);
            tdDesc.appendChild(descMain);
            tdDesc.appendChild(descSub);
            tr.appendChild(tdDesc);

            var tdWallet = document.createElement("td");
            tdWallet.className = "small";
            tdWallet.textContent = tx.wallet_name;
            tr.appendChild(tdWallet);

            var tdScope = document.createElement("td");
            tdScope.className = "small";
            var scopeText = tx.scope;
            if (tx.scope === "shared" && tx.paid_by_username) {
                scopeText += " (paid by " + tx.paid_by_username + ")";
            }
            tdScope.textContent = scopeText;
            tr.appendChild(tdScope);

            var tdAmt = document.createElement("td");
            tdAmt.className =
                "small text-end font-monospace fw-semibold " +
                (tx.transaction_type === "income" ? "text-success" : "text-danger");
            var prefix = tx.transaction_type === "income" ? "+" : "-";
            tdAmt.textContent = prefix + tx.amount + " " + tx.currency_code;
            tr.appendChild(tdAmt);

            var tdAct = document.createElement("td");
            tdAct.className = "text-end text-nowrap";
            if (tx.transaction_type === "income" || tx.transaction_type === "expense") {
                var editBtn = document.createElement("button");
                editBtn.type = "button";
                editBtn.className = "btn btn-outline-secondary btn-sm py-0 px-2 me-1";
                editBtn.textContent = "Queue edit";
                editBtn.addEventListener("click", function () {
                    loadSyncedTxIntoOfflineForm(tx);
                });
                tdAct.appendChild(editBtn);

                var delBtn = document.createElement("button");
                delBtn.type = "button";
                delBtn.className = "btn btn-outline-danger btn-sm py-0 px-2";
                delBtn.textContent = "Queue delete";
                delBtn.addEventListener("click", function () {
                    if (tabInvalidated) return;
                    var delMut = {
                        mutation_id: generateUuid(),
                        operation: "delete_transaction",
                        entity_id: tx.id,
                        base_revision: tx.revision,
                        sync_status: "pending",
                        queued_at: new Date().toISOString(),
                        payload: {
                            description: tx.description || tx.category_name || tx.id,
                            amount: tx.amount,
                            transaction_date: tx.transaction_date,
                            transaction_type: tx.transaction_type,
                            scope: tx.scope,
                        },
                    };
                    saveMutationToQueue(userId, delMut).then(function () {
                        updateUiStatus(userId);
                        var identity = getActiveIdentity();
                        renderOfflinePage(identity.userId, identity.username);
                    });
                });
                tdAct.appendChild(delBtn);
            } else {
                var naSpan = document.createElement("span");
                naSpan.className = "text-secondary small";
                naSpan.textContent = "Transfer leg";
                tdAct.appendChild(naSpan);
            }
            tr.appendChild(tdAct);

            tbody.appendChild(tr);
        });
    }

    function loadSyncedTxIntoOfflineForm(tx) {
        if (tabInvalidated) return;
        var form = document.getElementById("offline-entry-form");
        if (!form || !tx) return;

        form.setAttribute("data-editing-mutation-id", "");
        form.setAttribute("data-editing-operation", "update_transaction");
        form.setAttribute("data-editing-entity-id", tx.id);
        form.setAttribute("data-editing-base-revision", String(tx.revision));

        var typeEl = document.getElementById("offline-tx-type");
        var dateEl = document.getElementById("offline-tx-date");
        var walletEl = document.getElementById("offline-tx-wallet");
        var amountEl = document.getElementById("offline-tx-amount");
        var catEl = document.getElementById("offline-tx-category");
        var scopeEl = document.getElementById("offline-tx-scope");
        var spaceEl = document.getElementById("offline-tx-shared-space");
        var paidByEl = document.getElementById("offline-tx-paid-by");
        var descEl = document.getElementById("offline-tx-description");
        var notesEl = document.getElementById("offline-tx-notes");
        var submitBtn = document.getElementById("btn-offline-queue-submit");

        if (typeEl) typeEl.value = tx.transaction_type;
        if (dateEl) dateEl.value = tx.transaction_date;
        if (walletEl) walletEl.value = tx.wallet_id;
        if (amountEl) amountEl.value = tx.amount;
        if (catEl) catEl.value = tx.category_id || "";
        if (scopeEl) scopeEl.value = tx.scope || "personal";
        if (spaceEl) spaceEl.value = tx.shared_space_id || "";
        if (paidByEl) paidByEl.value = tx.paid_by_id ? String(tx.paid_by_id) : "";
        if (descEl) descEl.value = tx.description || "";
        if (notesEl) notesEl.value = tx.notes || "";
        if (submitBtn) {
            submitBtn.textContent = "Queue update (base rev " + String(tx.revision) + ")";
        }
        if (typeof form.scrollIntoView === "function") {
            form.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
    }

    function bindOfflinePageEvents() {
        if (!isOfflineShellPage()) return;

        var form = document.getElementById("offline-entry-form");
        var walletSelect = document.getElementById("offline-tx-wallet");
        var scopeSelect = document.getElementById("offline-tx-scope");
        var spaceSelect = document.getElementById("offline-tx-shared-space");
        var hintEl = document.getElementById("offline-wallet-precision-hint");
        var feedbackEl = document.getElementById("offline-entry-feedback");
        var loadMoreBtn = document.getElementById("btn-offline-load-more");

        function showFeedback(variant, text) {
            if (!feedbackEl) return;
            feedbackEl.classList.remove("d-none", "alert-danger", "alert-success", "alert-warning");
            feedbackEl.classList.add("alert-" + variant);
            feedbackEl.textContent = text;
        }

        if (walletSelect) {
            walletSelect.addEventListener("change", function () {
                if (tabInvalidated) return;
                var opt =
                    walletSelect.selectedOptions && walletSelect.selectedOptions.length
                        ? walletSelect.selectedOptions[0]
                        : null;
                if (!opt || !opt.value) {
                    if (hintEl) hintEl.textContent = "";
                    return;
                }
                var places = parseInt(opt.getAttribute("data-decimal-places") || "2", 10);
                var code = opt.getAttribute("data-currency-code") || "";
                var wScope = opt.getAttribute("data-scope") || "personal";
                var wSpaceId = opt.getAttribute("data-shared-space-id") || "";
                if (hintEl) {
                    hintEl.textContent =
                        code + " allows up to " + places + " decimal place(s).";
                }
                if (scopeSelect) {
                    scopeSelect.value = wScope;
                }
                if (spaceSelect && wSpaceId) {
                    spaceSelect.value = wSpaceId;
                }
            });
        }

        if (loadMoreBtn) {
            loadMoreBtn.addEventListener("click", function () {
                if (tabInvalidated) return;
                var identity = getActiveIdentity();
                if (!identity.userId || identity.invalidated) return;
                var nextOffset = parseInt(
                    loadMoreBtn.getAttribute("data-next-offset") || "0",
                    10
                );
                pullServerSnapshot(identity.userId, {
                    offset: nextOffset,
                    limit: 200,
                    appendTransactions: true,
                }).then(function () {
                    if (!tabInvalidated) {
                        renderOfflinePage(identity.userId, identity.username);
                    }
                });
            });
        }

        if (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                if (tabInvalidated) {
                    showFeedback(
                        "danger",
                        "This tab is locked because the account session changed or signed out. Sign in while online first."
                    );
                    return;
                }
                var identity = getActiveIdentity();
                if (!identity.userId || identity.invalidated) {
                    showFeedback(
                        "danger",
                        "No active offline user profile is available. Sign in while online first."
                    );
                    return;
                }

                var opt =
                    walletSelect &&
                    walletSelect.selectedOptions &&
                    walletSelect.selectedOptions.length
                        ? walletSelect.selectedOptions[0]
                        : null;
                var walletId = walletSelect ? walletSelect.value : "";
                if (!walletId || !opt) {
                    showFeedback("danger", "Please select a wallet from your downloaded snapshot.");
                    return;
                }

                var places = parseInt(opt.getAttribute("data-decimal-places") || "2", 10);
                var code = opt.getAttribute("data-currency-code") || "Currency";
                var amountRaw = (
                    (document.getElementById("offline-tx-amount") || {}).value || ""
                ).trim();

                if (!isValidCurrencyAmountString(amountRaw, places)) {
                    showFeedback(
                        "danger",
                        places === 0
                            ? code + " does not allow decimal places. Enter a positive whole amount."
                            : "Enter a positive decimal amount for " +
                                  code +
                                  " with at most " +
                                  places +
                                  " decimal places."
                    );
                    return;
                }

                var manualRateRaw = (
                    (document.getElementById("offline-tx-manual-rate") || {}).value || ""
                ).trim();
                if (!isValidManualRateString(manualRateRaw)) {
                    showFeedback(
                        "danger",
                        "Manual exchange rate must be a positive decimal with up to 8 decimal places."
                    );
                    return;
                }

                var dateVal = (
                    (document.getElementById("offline-tx-date") || {}).value || ""
                ).trim();
                if (!dateVal) {
                    showFeedback("danger", "Please select a transaction date.");
                    return;
                }

                var editingMutationId = form.getAttribute("data-editing-mutation-id") || "";
                var editingOp =
                    form.getAttribute("data-editing-operation") || "create_transaction";
                var editingEntityId =
                    form.getAttribute("data-editing-entity-id") || generateUuid();
                var editingBaseRevRaw = form.getAttribute("data-editing-base-revision") || "";
                var baseRev =
                    editingBaseRevRaw !== "" ? parseInt(editingBaseRevRaw, 10) : null;

                var scopeVal =
                    (scopeSelect && scopeSelect.value) ||
                    opt.getAttribute("data-scope") ||
                    "personal";
                var sharedSpaceVal =
                    scopeVal === "shared"
                        ? (spaceSelect && spaceSelect.value) ||
                          opt.getAttribute("data-shared-space-id") ||
                          null
                        : null;
                var paidByVal = (document.getElementById("offline-tx-paid-by") || {}).value || "";

                var mutation = {
                    mutation_id: editingMutationId || generateUuid(),
                    operation: editingOp,
                    entity_id: editingEntityId,
                    base_revision: baseRev,
                    sync_status: "pending",
                    queued_at: new Date().toISOString(),
                    payload: {
                        transaction_type: (
                            document.getElementById("offline-tx-type") || {}
                        ).value || "expense",
                        amount: amountRaw,
                        wallet_id: walletId,
                        category_id:
                            (document.getElementById("offline-tx-category") || {}).value || null,
                        transaction_date: dateVal,
                        scope: scopeVal,
                        shared_space_id: sharedSpaceVal,
                        paid_by_id: paidByVal ? parseInt(paidByVal, 10) : null,
                        manual_rate: manualRateRaw || null,
                        description: (
                            (document.getElementById("offline-tx-description") || {}).value || ""
                        ).trim(),
                        notes: (
                            (document.getElementById("offline-tx-notes") || {}).value || ""
                        ).trim(),
                    },
                };

                saveMutationToQueue(identity.userId, mutation).then(function (saved) {
                    if (!saved || tabInvalidated) return;
                    form.setAttribute("data-editing-mutation-id", "");
                    form.setAttribute("data-editing-operation", "create_transaction");
                    form.setAttribute("data-editing-entity-id", "");
                    form.setAttribute("data-editing-base-revision", "");
                    var amountInput = document.getElementById("offline-tx-amount");
                    var descInput = document.getElementById("offline-tx-description");
                    var notesInput = document.getElementById("offline-tx-notes");
                    var manualRateInput = document.getElementById("offline-tx-manual-rate");
                    var submitBtn = document.getElementById("btn-offline-queue-submit");
                    if (amountInput) amountInput.value = "";
                    if (descInput) descInput.value = "";
                    if (notesInput) notesInput.value = "";
                    if (manualRateInput) manualRateInput.value = "";
                    if (submitBtn) submitBtn.textContent = "Queue offline transaction";

                    showFeedback(
                        "success",
                        "Transaction queued locally in your isolated account queue."
                    );
                    updateUiStatus(identity.userId);
                    renderOfflinePage(identity.userId, identity.username);
                });
            });
        }
    }

    function resumeOnlineTab() {
        if (tabInvalidated || navigator.onLine === false) {
            return Promise.resolve({ status: tabInvalidated ? "tab_invalidated" : "offline" });
        }
        var active = getActiveIdentity();
        if (!active.userId || active.invalidated) {
            if (active.invalidated) {
                invalidateCurrentTab(active.reason || "session_invalidated");
            }
            return Promise.resolve({ status: "no_user" });
        }
        var runGen = tabGeneration;
        return verifyServerIdentity(active.userId, { runGen: runGen }).then(function (verifyRes) {
            if (!isGenerationValid(runGen, active.userId)) {
                return { status: "tab_invalidated" };
            }
            if (!verifyRes.verified) {
                if (verifyRes.status !== "user_mismatch") {
                    updateUiStatus(active.userId, verifyRes);
                }
                return verifyRes;
            }
            updateUiStatus(active.userId);
            return flushMutationQueue(active.userId, { requireIdentityPrecheck: false });
        });
    }

    window.BudgetOfflineSync = {
        MAX_SYNC_BATCH_SIZE: MAX_SYNC_BATCH_SIZE,
        initializePageIdentity: initializePageIdentity,
        getActiveIdentity: getActiveIdentity,
        verifyServerIdentity: verifyServerIdentity,
        invalidateCurrentTab: invalidateCurrentTab,
        resumeOnlineTab: resumeOnlineTab,
        enqueueTransactionForm: enqueueTransactionForm,
        saveMutationToQueue: saveMutationToQueue,
        removeMutationFromQueue: removeMutationFromQueue,
        computeMutationFingerprint: computeMutationFingerprint,
        flushMutationQueue: function (options) {
            var identity = getActiveIdentity();
            return flushMutationQueue(identity.userId, options);
        },
        flushMutationQueueForUser: flushMutationQueue,
        clearCurrentUserCache: function () {
            var identity = getActiveIdentity();
            return deleteUserDatabase(identity.userId);
        },
        getQueuedMutations: function (explicitUserId) {
            var uid = explicitUserId || getActiveIdentity().userId;
            return getQueuedMutations(uid);
        },
        getStateSnapshot: function (explicitUserId) {
            var uid = explicitUserId || getActiveIdentity().userId;
            return getStateSnapshot(uid);
        },
        saveStateSnapshot: saveStateSnapshot,
        renderOfflinePage: renderOfflinePage,
        isValidCurrencyAmountString: isValidCurrencyAmountString,
        isTabInvalidated: function () {
            return tabInvalidated;
        },
        getTabGeneration: function () {
            return tabGeneration;
        },
    };

    document.addEventListener("DOMContentLoaded", function () {
        if ("serviceWorker" in navigator) {
            navigator.serviceWorker
                .register("/service-worker.js", { scope: "/" })
                .catch(function () {});
        }

        var identity = initializePageIdentity();
        bindOfflinePageEvents();

        if (isOfflineShellPage() && !identity.invalidated) {
            renderOfflinePage(identity.userId, identity.username);
        }

        if (!identity.userId || identity.invalidated) return;

        updateUiStatus(identity.userId);
        if (getBodyAttr("data-disable-auto-sync-on-load") !== "true") {
            flushMutationQueue(identity.userId, {
                requireIdentityPrecheck: isOfflineShellPage(),
            });
        }

        window.addEventListener("online", function () {
            resumeOnlineTab();
        });
        window.addEventListener("offline", function () {
            if (tabInvalidated) return;
            var active = getActiveIdentity();
            if (!active.userId || active.invalidated) return;
            updateUiStatus(active.userId);
            if (isOfflineShellPage()) {
                renderOfflinePage(active.userId, active.username);
            }
        });
        document.addEventListener("visibilitychange", function () {
            if (
                document.visibilityState === "visible" &&
                navigator.onLine !== false &&
                !tabInvalidated
            ) {
                var active = getActiveIdentity();
                if (active.invalidated) {
                    invalidateCurrentTab(active.reason || "session_invalidated");
                }
            }
        });

        var syncBtn = document.getElementById("btn-sync-now");
        if (syncBtn) {
            syncBtn.addEventListener("click", function () {
                if (tabInvalidated) return;
                var active = getActiveIdentity();
                if (!active.userId || active.invalidated) return;
                flushMutationQueue(active.userId, { requireIdentityPrecheck: true });
            });
        }

        var settingsSyncBtn = document.getElementById("btn-settings-sync");
        if (settingsSyncBtn) {
            settingsSyncBtn.addEventListener("click", function () {
                if (tabInvalidated) return;
                var active = getActiveIdentity();
                if (!active.userId || active.invalidated) return;
                var note = document.getElementById("settings-last-sync-note");
                flushMutationQueue(active.userId, { requireIdentityPrecheck: true }).then(
                    function (outcome) {
                        if (!note || !outcome) return;
                        if (outcome.status === "success") {
                            note.textContent = "Last server sync: Just now";
                        } else if (outcome.message) {
                            note.textContent = "Sync status: " + outcome.message;
                        }
                    }
                );
            });
        }

        var logoutForms = document.querySelectorAll("#logout-form, #sidebar-logout-form");
        Array.prototype.forEach.call(logoutForms, function (form) {
            form.addEventListener("submit", function (event) {
                if (form.getAttribute("data-confirmed-logout") === "true") {
                    return;
                }
                event.preventDefault();
                var active = getActiveIdentity();
                var logoutUserId = active.userId || tabInitialDomUserId;
                getQueuedMutations(logoutUserId).then(function (items) {
                    if (items.length > 0) {
                        var proceed = window.confirm(
                            "You have " +
                                items.length +
                                " unsynced offline change(s). Logging out will clear local offline data for your account. Continue logging out?"
                        );
                        if (!proceed) {
                            return;
                        }
                    }
                    deleteUserDatabase(logoutUserId).then(function (delResult) {
                        if (
                            delResult &&
                            (delResult.status === "blocked" || delResult.status === "error")
                        ) {
                            var forceProceed = window.confirm(
                                delResult.message +
                                    " Do you still want to sign out of the server session?"
                            );
                            if (!forceProceed) {
                                return;
                            }
                        }
                        try {
                            localStorage.removeItem(STORAGE_ACTIVE_USER_ID);
                            localStorage.removeItem(STORAGE_ACTIVE_USERNAME);
                            localStorage.setItem(STORAGE_AUTH_STATE, "logged_out");
                        } catch (e) {}
                        broadcastSignal({ type: "logout", user_id: logoutUserId });
                        form.setAttribute("data-confirmed-logout", "true");
                        form.submit();
                    });
                });
            });
        });
    });
})();
