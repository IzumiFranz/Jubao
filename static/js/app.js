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
});
