document.addEventListener("DOMContentLoaded", function () {
    // 1. Sidebar navigation offcanvas setup (independent of missing sidebar)
    var sidebar = document.getElementById("appSidebar");
    var toggleButtons = document.querySelectorAll('[data-bs-target="#appSidebar"][data-bs-toggle="offcanvas"]');

    if (sidebar && toggleButtons.length) {
        toggleButtons.forEach(function (btn) {
            btn.setAttribute("aria-expanded", "false");
        });

        sidebar.addEventListener("shown.bs.offcanvas", function () {
            toggleButtons.forEach(function (btn) {
                btn.setAttribute("aria-expanded", "true");
            });
        });

        sidebar.addEventListener("hidden.bs.offcanvas", function () {
            toggleButtons.forEach(function (btn) {
                btn.setAttribute("aria-expanded", "false");
            });
        });
    }

    // 2. Top-bar preference forms offline handling
    var topBarForms = [
        document.getElementById("workspace-view-form"),
        document.getElementById("display-currency-form")
    ];
    topBarForms.forEach(function (form) {
        if (!form) return;
        var select = form.querySelector("select");
        if (select) {
            select.dataset.savedValue = select.value;
            select.addEventListener("focus", function () {
                select.dataset.savedValue = select.value;
            });
        }
        form.addEventListener("submit", function (evt) {
            if (typeof navigator !== "undefined" && navigator.onLine === false) {
                evt.preventDefault();
                if (select && select.dataset.savedValue !== undefined) {
                    select.value = select.dataset.savedValue;
                }
                alert("Workspace view and display currency preferences require an active internet connection to save to your account.");
            }
        });
    });

    // 3. Personal transaction sharing controls (honestly online-only)
    function updateSharingControlsConnectivity() {
        var isOffline = typeof navigator !== "undefined" && navigator.onLine === false;
        var sharingBtns = document.querySelectorAll('.sharing-action-btn, [data-online-only="true"] button, button[data-online-only="true"]');
        var alertBoxes = document.querySelectorAll('.sharing-offline-alert');

        sharingBtns.forEach(function (btn) {
            btn.disabled = isOffline;
            if (isOffline) {
                btn.setAttribute("data-offline-disabled", "true");
            } else {
                btn.removeAttribute("data-offline-disabled");
            }
        });

        alertBoxes.forEach(function (el) {
            if (isOffline) {
                el.classList.remove("d-none");
            } else {
                el.classList.add("d-none");
            }
        });
    }

    function initSharingOnlineControls() {
        var sharingForms = document.querySelectorAll('.sharing-action-form, form[data-online-only="true"]');

        sharingForms.forEach(function (form) {
            form.addEventListener("submit", function (evt) {
                if (typeof navigator !== "undefined" && navigator.onLine === false) {
                    evt.preventDefault();
                    evt.stopImmediatePropagation();
                    // Show guidance without clearing inputs
                    alert("Sharing personal transactions requires an internet connection and server confirmation.");
                    return false;
                }
            });
        });

        window.addEventListener("online", updateSharingControlsConnectivity);
        window.addEventListener("offline", updateSharingControlsConnectivity);
        updateSharingControlsConnectivity();
    }
    initSharingOnlineControls();

    // 4. Initialize Bootstrap Toasts for Django messages
    function initToastNotifications() {
        var toastEls = document.querySelectorAll(".app-toast:not([data-toast-initialized])");
        if (!toastEls.length) return;

        var prefersReducedMotion = false;
        try {
            prefersReducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        } catch (e) {
            prefersReducedMotion = false;
        }

        var hasBootstrap = typeof bootstrap !== "undefined" && typeof bootstrap.Toast !== "undefined";

        toastEls.forEach(function (toastEl) {
            toastEl.setAttribute("data-toast-initialized", "true");
            var autohideAttr = toastEl.getAttribute("data-bs-autohide");
            var autohide = autohideAttr !== "false";
            var delay = parseInt(toastEl.getAttribute("data-bs-delay"), 10) || 4000;

            var toast = null;
            if (hasBootstrap) {
                try {
                    toast = bootstrap.Toast.getOrCreateInstance(toastEl, {
                        animation: !prefersReducedMotion,
                        autohide: false
                    });
                } catch (err) {
                    console.error("Failed to initialize Bootstrap Toast:", err);
                    toast = null;
                }
            }

            var isServerRendered = toastEl.classList.contains("show");
            if (!isServerRendered) {
                if (toast) {
                    toast.show();
                } else {
                    toastEl.classList.add("show");
                }
            }

            function dismissToast() {
                if (toast) {
                    try {
                        toast.hide();
                        return;
                    } catch (e) {}
                }
                toastEl.classList.remove("show");
                toastEl.classList.add("hide");
                toastEl.style.display = "none";
            }

            // Fallback close button handler if Bootstrap JS is not loaded
            if (!hasBootstrap) {
                toastEl.querySelectorAll('[data-bs-dismiss="toast"]').forEach(function (btn) {
                    btn.addEventListener("click", function () {
                        if (cleanupTimer) cleanupTimer();
                        dismissToast();
                    });
                });
            }

            var cleanupTimer = null;
            if (autohide) {
                var remaining = delay;
                var startTime = Date.now();
                var timerId = null;
                var isHovered = false;
                var isFocused = false;

                function startTimer(duration) {
                    clearTimeout(timerId);
                    startTime = Date.now();
                    timerId = setTimeout(function () {
                        timerId = null;
                        dismissToast();
                    }, duration);
                }

                function pauseTimer() {
                    if (timerId !== null) {
                        clearTimeout(timerId);
                        timerId = null;
                        var elapsed = Date.now() - startTime;
                        remaining = Math.max(0, remaining - elapsed);
                    }
                }

                function resumeTimer() {
                    if (timerId === null && remaining > 0) {
                        startTimer(remaining);
                    }
                }

                cleanupTimer = function () {
                    clearTimeout(timerId);
                    timerId = null;
                    remaining = 0;
                };

                toastEl.addEventListener("mouseenter", function () {
                    isHovered = true;
                    pauseTimer();
                });

                toastEl.addEventListener("mouseleave", function () {
                    isHovered = false;
                    if (!isFocused) {
                        resumeTimer();
                    }
                });

                toastEl.addEventListener("focusin", function () {
                    isFocused = true;
                    pauseTimer();
                });

                toastEl.addEventListener("focusout", function (e) {
                    var nextFocus = e.relatedTarget;
                    if (nextFocus && toastEl.contains(nextFocus)) {
                        return;
                    }
                    isFocused = false;
                    if (!isHovered) {
                        resumeTimer();
                    }
                });

                toastEl.addEventListener("hide.bs.toast", cleanupTimer);

                try {
                    if (toastEl.matches(":hover")) isHovered = true;
                    if (toastEl.contains(document.activeElement)) isFocused = true;
                } catch (e) {}

                if (!isHovered && !isFocused) {
                    startTimer(delay);
                }
            }
        });
    }
    initToastNotifications();

    // Global helper for programmatic notifications
    window.showAppToast = function (message, level, options) {
        var container = document.getElementById("app-toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "app-toast-container";
            container.className = "toast-container position-fixed p-3 app-toast-container";
            container.setAttribute("aria-label", "Notifications");
            var mainContent = document.getElementById("main-content");
            if (mainContent) {
                mainContent.prepend(container);
            } else {
                document.body.appendChild(container);
            }
        }

        level = (level || "info").toLowerCase();
        var isAlert = level === "error" || level === "warning";
        var autohide = options && typeof options.autohide !== "undefined" ? options.autohide : !isAlert;
        var delay = options && options.delay ? options.delay : 4000;

        var toastEl = document.createElement("div");
        toastEl.className = "toast app-toast app-toast-" + level + " fade";
        toastEl.setAttribute("role", isAlert ? "alert" : "status");
        toastEl.setAttribute("aria-live", isAlert ? "assertive" : "polite");
        toastEl.setAttribute("aria-atomic", "true");
        toastEl.setAttribute("data-bs-autohide", autohide ? "true" : "false");
        toastEl.setAttribute("data-bs-delay", delay);

        var inner = document.createElement("div");
        inner.className = "d-flex align-items-center p-2";

        var body = document.createElement("div");
        body.className = "toast-body app-toast-body p-0 me-2";
        body.textContent = message;

        var closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "btn-close ms-auto flex-shrink-0";
        closeBtn.setAttribute("data-bs-dismiss", "toast");
        closeBtn.setAttribute("aria-label", "Close");

        inner.appendChild(body);
        inner.appendChild(closeBtn);
        toastEl.appendChild(inner);
        container.appendChild(toastEl);

        initToastNotifications();
        return toastEl;
    };
});
