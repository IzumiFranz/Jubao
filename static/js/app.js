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

    // 5. Monetary input grouping helpers
    function getSelectDecimalPlaces(selectEl, fallback) {
        if (!selectEl) return typeof fallback === "number" ? fallback : 2;
        var opt =
            selectEl.selectedOptions && selectEl.selectedOptions.length
                ? selectEl.selectedOptions[0]
                : null;
        if (opt && opt.hasAttribute("data-decimal-places")) {
            var p = parseInt(opt.getAttribute("data-decimal-places"), 10);
            if (!isNaN(p)) return p;
        }
        return typeof fallback === "number" ? fallback : 2;
    }

    function setupMonetaryFieldGrouping(inputEl, getPlacesFn, options) {
        if (!inputEl) return;
        var allowNegative = !!(options && options.allowNegative);
        var allowZero = !!(options && options.allowZero);

        inputEl.addEventListener("blur", function () {
            var val = inputEl.value.trim();
            if (!val) {
                return;
            }

            var isNeg = false;
            if (val.charAt(0) === "-") {
                if (!allowNegative) return;
                isNeg = true;
                val = val.substring(1).trim();
            } else if (val.charAt(0) === "+") {
                val = val.substring(1).trim();
            }

            if (!val) return;

            var hasComma = val.indexOf(",") !== -1;
            if (hasComma) {
                if (!/^\d{1,3}(?:,\d{3})+(?:\.\d+)?$/.test(val)) {
                    return;
                }
            } else {
                if (!/^\d+(?:\.\d+)?$/.test(val)) {
                    return;
                }
            }

            var uncomma = val.replace(/,/g, "");
            var parts = uncomma.split(".");
            var intPart = parts[0];
            var fracPart = parts.length > 1 ? parts[1] : null;
            var places = typeof getPlacesFn === "function" ? getPlacesFn() : 2;

            if (places === 0) {
                if (fracPart !== null && !/^0*$/.test(fracPart)) {
                    return;
                }
            } else {
                if (fracPart !== null && fracPart.length > places) {
                    return;
                }
            }

            var intAllZeros = /^0*$/.test(intPart);
            var fracAllZeros = fracPart === null || /^0*$/.test(fracPart);
            if (intAllZeros && fracAllZeros && !allowZero) {
                return;
            }

            var cleanInt = intPart.replace(/^0+/, "") || "0";
            var groupedInt = cleanInt.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
            var result = fracPart !== null ? groupedInt + "." + fracPart : groupedInt;
            if (isNeg && !(intAllZeros && fracAllZeros)) {
                result = "-" + result;
            }
            inputEl.value = result;
        });
    }

    // Savings goal Target amount grouping
    var targetAmountInput = document.getElementById("id_target_amount");
    if (targetAmountInput) {
        var currencySelect = document.getElementById("id_currency");

        function getTargetCurrencyDecimals() {
            return getSelectDecimalPlaces(currencySelect, 2);
        }

        function updateTargetPlaceholder() {
            var places = getTargetCurrencyDecimals();
            if (places === 0) {
                targetAmountInput.placeholder = "0";
            } else if (places === 3) {
                targetAmountInput.placeholder = "0.000";
            } else {
                targetAmountInput.placeholder = "0.00";
            }
        }

        if (currencySelect) {
            currencySelect.addEventListener("change", updateTargetPlaceholder);
            updateTargetPlaceholder();
        }

        setupMonetaryFieldGrouping(targetAmountInput, getTargetCurrencyDecimals);
    }

    // Edit savings goal inputs
    document.querySelectorAll('input[name="target_amount"]').forEach(function (input) {
        if (input.id === "id_target_amount") return;
        var row = input.closest("tr, form");
        var sel = row ? row.querySelector('select[name="currency"]') : null;
        setupMonetaryFieldGrouping(input, function () {
            return getSelectDecimalPlaces(sel, 2);
        });
    });

    // Wallet opening balance
    var openingBalInput = document.getElementById("id_opening_balance");
    if (openingBalInput) {
        var walletCurrSel = document.getElementById("id_currency");
        setupMonetaryFieldGrouping(
            openingBalInput,
            function () {
                return getSelectDecimalPlaces(walletCurrSel, 2);
            },
            { allowNegative: true, allowZero: true }
        );
    }

    // Category budget limit amount
    var limitAmountInput = document.getElementById("id_limit_amount");
    if (limitAmountInput) {
        var budgetCurrSel = document.getElementById("id_currency");
        setupMonetaryFieldGrouping(limitAmountInput, function () {
            return getSelectDecimalPlaces(budgetCurrSel, 2);
        });
    }

    // Savings contribution amount
    var contributionAmountInput = document.getElementById("id_amount");
    if (contributionAmountInput) {
        var goalSelect = document.getElementById("id_goal");
        setupMonetaryFieldGrouping(contributionAmountInput, function () {
            return getSelectDecimalPlaces(goalSelect, 2);
        });
    }

    // Transfer inputs
    var amountSentInput = document.getElementById("id_amount_sent");
    var amountRecvInput = document.getElementById("id_amount_received");
    var feeAmountInput = document.getElementById("id_fee_amount");
    var srcWalletSel = document.getElementById("id_source_wallet");
    var dstWalletSel = document.getElementById("id_destination_wallet");

    if (amountSentInput) {
        setupMonetaryFieldGrouping(amountSentInput, function () {
            return getSelectDecimalPlaces(srcWalletSel, 2);
        });
    }
    if (amountRecvInput) {
        setupMonetaryFieldGrouping(amountRecvInput, function () {
            return getSelectDecimalPlaces(dstWalletSel, 2);
        });
    }
    if (feeAmountInput) {
        setupMonetaryFieldGrouping(
            feeAmountInput,
            function () {
                return getSelectDecimalPlaces(srcWalletSel, 2);
            },
            { allowZero: true }
        );
    }

    // Edit transfer accordion forms
    document.querySelectorAll("form").forEach(function (formEl) {
        var editSent = formEl.querySelector('input[name="amount_sent"]');
        var editRecv = formEl.querySelector('input[name="amount_received"]');
        var editFee = formEl.querySelector('input[name="fee_amount"]');
        if (editSent && editSent.id !== "id_amount_sent") {
            var srcSel = formEl.querySelector('select[name="source_wallet"]');
            setupMonetaryFieldGrouping(editSent, function () {
                return getSelectDecimalPlaces(srcSel, 2);
            });
        }
        if (editRecv && editRecv.id !== "id_amount_received") {
            var dstSel = formEl.querySelector('select[name="destination_wallet"]');
            setupMonetaryFieldGrouping(editRecv, function () {
                return getSelectDecimalPlaces(dstSel, 2);
            });
        }
        if (editFee && editFee.id !== "id_fee_amount") {
            var srcSelFee = formEl.querySelector('select[name="source_wallet"]');
            setupMonetaryFieldGrouping(
                editFee,
                function () {
                    return getSelectDecimalPlaces(srcSelFee, 2);
                },
                { allowZero: true }
            );
        }
    });

    // Currency converter amount
    var convAmountInput = document.getElementById("converter-amount");
    if (convAmountInput) {
        var convFromSel = document.getElementById("converter-from");
        setupMonetaryFieldGrouping(convAmountInput, function () {
            return getSelectDecimalPlaces(convFromSel, 2);
        });
    }
});
