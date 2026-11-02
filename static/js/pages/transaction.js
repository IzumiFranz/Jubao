document.addEventListener("DOMContentLoaded", function () {
    var transactionForm = document.getElementById("transaction-form");
    if (!transactionForm) {
        return;
    }

    var actionInput = document.getElementById("tx-form-action");
    var idInput = document.getElementById("tx-form-id");
    var revisionInput = document.getElementById("tx-form-revision");
    var modalTitle = document.getElementById("transactionModalLabel");
    var submitBtn = document.getElementById("tx-submit-btn");

    var typeSelect = document.getElementById("tx-type");
    var amountInput = document.getElementById("tx-amount");
    var amountHelp = document.getElementById("tx-amount-help");
    var amountFeedback = document.getElementById("tx-amount-feedback");
    var walletSelect = document.getElementById("tx-wallet");
    var dateInput = document.getElementById("tx-date");
    var categorySelect = document.getElementById("tx-category");
    var scopeSelect = document.getElementById("tx-scope");
    var paidBySelect = document.getElementById("tx-paid-by");
    var manualRateInput = document.getElementById("tx-manual-rate");
    var descriptionInput = document.getElementById("tx-description");
    var notesInput = document.getElementById("tx-notes");
    var modalElement = document.getElementById("transactionModal");

    function getSelectedWalletPrecision() {
        if (!walletSelect || !walletSelect.selectedOptions.length) {
            return { places: 2, code: "" };
        }
        var opt = walletSelect.selectedOptions[0];
        var rawPlaces = opt.getAttribute("data-decimal-places");
        var code = opt.getAttribute("data-currency-code") || "";
        var places = rawPlaces !== null && rawPlaces !== "" ? parseInt(rawPlaces, 10) : 2;
        if (isNaN(places)) {
            places = 2;
        }
        return { places: places, code: code };
    }

    function updatePrecisionHint() {
        var info = getSelectedWalletPrecision();
        if (!amountInput || !amountHelp) {
            return;
        }
        if (info.places === 0) {
            amountInput.placeholder = "0";
            amountHelp.textContent = (info.code || "Selected currency") + " uses whole units (0 decimal places).";
        } else if (info.places === 3) {
            amountInput.placeholder = "0.000";
            amountHelp.textContent = (info.code || "Selected currency") + " supports up to 3 decimal places.";
        } else {
            amountInput.placeholder = "0.00";
            amountHelp.textContent = (info.code || "Selected currency") + " supports up to 2 decimal places.";
        }

        if (walletSelect && walletSelect.selectedOptions.length) {
            var walletScope = walletSelect.selectedOptions[0].getAttribute("data-scope");
            if (walletScope && scopeSelect) {
                scopeSelect.value = walletScope;
            }
        }
    }

    function isValidCurrencyAmount(rawValue, places) {
        if (!rawValue) {
            return false;
        }
        var val = String(rawValue).trim();
        if (!val) {
            return false;
        }
        if (val.indexOf(",") !== -1) {
            if (!/^\d{1,3}(?:,\d{3})+(?:\.\d+)?$/.test(val)) {
                return false;
            }
            val = val.replace(/,/g, "");
        }
        var regex = places === 0
            ? /^\d{1,15}(\.0+)?$/
            : new RegExp("^\\d{1,15}(\\.\\d{1," + places + "}0*)?$");
        if (!regex.test(val)) {
            return false;
        }
        var parts = val.split(".");
        var intPart = parts[0].replace(/^0+/, "") || "0";
        var fracPart = (parts[1] || "").replace(/0+$/, "");
        return intPart !== "0" || fracPart.length > 0;
    }

    function formatAmountInputOnBlur() {
        if (!amountInput) return;
        var raw = amountInput.value.trim();
        if (!raw) return;
        var info = getSelectedWalletPrecision();
        if (!isValidCurrencyAmount(raw, info.places)) return;
        var uncomma = raw.replace(/,/g, "");
        var parts = uncomma.split(".");
        var intPart = parts[0];
        var fracPart = parts.length > 1 ? parts[1] : null;
        var cleanInt = intPart.replace(/^0+/, "") || "0";
        var groupedInt = cleanInt.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        amountInput.value = fracPart !== null ? groupedInt + "." + fracPart : groupedInt;
    }

    function validateDecimalAmount() {
        if (!amountInput) {
            return true;
        }
        var rawValue = amountInput.value.trim();
        if (rawValue === "") {
            amountInput.classList.remove("is-invalid");
            return false;
        }
        var info = getSelectedWalletPrecision();
        if (isValidCurrencyAmount(rawValue, info.places)) {
            amountInput.classList.remove("is-invalid");
            return true;
        }
        amountInput.classList.add("is-invalid");
        if (amountFeedback) {
            amountFeedback.textContent = info.places === 0
                ? (info.code || "Currency") + " does not allow decimal places."
                : "Enter a positive amount with at most " + info.places + " decimal places.";
        }
        return false;
    }

    if (walletSelect) {
        walletSelect.addEventListener("change", function () {
            updatePrecisionHint();
            if (amountInput && amountInput.value.trim() !== "") {
                validateDecimalAmount();
            }
        });
    }

    if (amountInput) {
        amountInput.addEventListener("blur", function () {
            var valid = validateDecimalAmount();
            if (valid) {
                formatAmountInputOnBlur();
            }
        });
        amountInput.addEventListener("input", function () {
            if (amountInput.classList.contains("is-invalid")) {
                validateDecimalAmount();
            }
        });
    }

    document.querySelectorAll(".btn-edit-tx").forEach(function (btn) {
        btn.addEventListener("click", function () {
            if (actionInput) actionInput.value = "update_transaction";
            if (idInput) idInput.value = btn.getAttribute("data-tx-id") || "";
            if (revisionInput) revisionInput.value = btn.getAttribute("data-tx-revision") || "";
            if (modalTitle) modalTitle.textContent = "Edit transaction";
            if (submitBtn) submitBtn.textContent = "Update transaction";

            if (typeSelect) typeSelect.value = btn.getAttribute("data-tx-type") || "expense";
            if (walletSelect) walletSelect.value = btn.getAttribute("data-tx-wallet") || "";
            updatePrecisionHint();
            if (amountInput) amountInput.value = btn.getAttribute("data-tx-amount") || "";
            if (categorySelect) categorySelect.value = btn.getAttribute("data-tx-category") || "";
            if (dateInput) dateInput.value = btn.getAttribute("data-tx-date") || "";
            if (scopeSelect) scopeSelect.value = btn.getAttribute("data-tx-scope") || "personal";
            if (paidBySelect) paidBySelect.value = btn.getAttribute("data-tx-paid-by") || "";
            if (manualRateInput) manualRateInput.value = "";
            if (descriptionInput) descriptionInput.value = btn.getAttribute("data-tx-description") || "";
            if (notesInput) notesInput.value = btn.getAttribute("data-tx-notes") || "";
        });
    });

    if (modalElement) {
        modalElement.addEventListener("hidden.bs.modal", function () {
            if (actionInput) actionInput.value = "create_transaction";
            if (idInput) idInput.value = "";
            if (revisionInput) revisionInput.value = "";
            if (modalTitle) modalTitle.textContent = "Add transaction";
            if (submitBtn) submitBtn.textContent = "Save transaction";
            if (amountInput) {
                amountInput.value = "";
                amountInput.classList.remove("is-invalid");
            }
            if (paidBySelect) paidBySelect.value = "";
            if (manualRateInput) manualRateInput.value = "";
            if (descriptionInput) descriptionInput.value = "";
            if (notesInput) notesInput.value = "";
        });
    }

    transactionForm.addEventListener("submit", function (event) {
        if (!validateDecimalAmount()) {
            event.preventDefault();
            return;
        }
        if (
            typeof navigator !== "undefined" &&
            navigator.onLine === false &&
            window.BudgetOfflineSync &&
            typeof window.BudgetOfflineSync.enqueueTransactionForm === "function"
        ) {
            event.preventDefault();
            window.BudgetOfflineSync.enqueueTransactionForm(transactionForm);
        }
    });

    // Transfer modal auto-reopen and keyboard focus management
    var transferModalEl = document.getElementById("transferModal");
    if (transferModalEl) {
        function focusSensibleModalElement() {
            var target = transferModalEl.querySelector(".is-invalid, input:not([type=hidden]):not([readonly]), select, button:not(.btn-close), .alert-danger") || transferModalEl;
            if (target && typeof target.focus === "function") {
                target.focus();
            }
        }
        if (transferModalEl.getAttribute("data-auto-open") === "true") {
            var bs = typeof bootstrap !== "undefined" ? bootstrap : window.bootstrap;
            if (bs && bs.Modal) {
                var transferModal = bs.Modal.getOrCreateInstance(transferModalEl);
                transferModal.show();
            }
            setTimeout(focusSensibleModalElement, 150);
            setTimeout(focusSensibleModalElement, 350);
        }
        transferModalEl.addEventListener("shown.bs.modal", focusSensibleModalElement);
    }

    updatePrecisionHint();

    // Initialize Bootstrap Popovers for valuation & scope chips (touch & click friendly)
    var bsLib = typeof bootstrap !== "undefined" ? bootstrap : window.bootstrap;
    var popoverInstances = [];
    if (bsLib && bsLib.Popover) {
        document.querySelectorAll('[data-bs-toggle="popover"]').forEach(function (el) {
            var content = el.getAttribute("data-bs-content") || el.getAttribute("title") || "";
            var inst = bsLib.Popover.getOrCreateInstance(el, {
                trigger: "click",
                placement: "bottom",
                fallbackPlacements: ["top"],
                boundary: "viewport",
                content: content,
                html: false
            });
            popoverInstances.push({ el: el, instance: inst });
        });

        // Dismiss open popovers on Escape key or outside tap/click
        document.addEventListener("keydown", function (evt) {
            if (evt.key === "Escape") {
                popoverInstances.forEach(function (item) {
                    item.instance.hide();
                });
            }
        });
        document.addEventListener("click", function (evt) {
            popoverInstances.forEach(function (item) {
                var tip = item.instance.tip;
                if (!item.el.contains(evt.target) && (!tip || !tip.contains(evt.target))) {
                    item.instance.hide();
                }
            });
        });
    }

    // Clickable "Uncategorized" placeholder opens Edit modal focused on category
    document.querySelectorAll(".btn-edit-tx-category").forEach(function (catBtn) {
        catBtn.addEventListener("click", function () {
            var txId = catBtn.getAttribute("data-tx-id");
            var rowEditBtn = document.querySelector('.btn-edit-tx[data-tx-id="' + txId + '"]');
            if (rowEditBtn) {
                rowEditBtn.click();
                setTimeout(function () {
                    if (categorySelect && typeof categorySelect.focus === "function") {
                        categorySelect.focus();
                    }
                }, 250);
            }
        });
    });

    // Delete confirmation dialog before submitting delete form
    var pendingDeleteForm = null;
    var deleteModalEl = document.getElementById("deleteConfirmModal");
    var deleteDescEl = document.getElementById("delete-confirm-desc");
    var deleteAmountEl = document.getElementById("delete-confirm-amount");
    var confirmDeleteBtn = document.getElementById("btn-confirm-delete-tx");

    document.querySelectorAll(".tx-delete-trigger").forEach(function (btn) {
        btn.addEventListener("click", function (evt) {
            var form = btn.closest("form.tx-delete-form");
            if (!form || !deleteModalEl || !(bsLib && bsLib.Modal)) {
                return;
            }
            evt.preventDefault();
            pendingDeleteForm = form;
            if (deleteDescEl) {
                deleteDescEl.textContent = btn.getAttribute("data-delete-desc") || "Transaction";
            }
            if (deleteAmountEl) {
                deleteAmountEl.textContent = btn.getAttribute("data-delete-amount") || "";
            }
            var delModal = bsLib.Modal.getOrCreateInstance(deleteModalEl);
            delModal.show();
        });
    });

    if (confirmDeleteBtn) {
        confirmDeleteBtn.addEventListener("click", function () {
            if (pendingDeleteForm) {
                pendingDeleteForm.submit();
            }
        });
    }

    // Populate per-card transaction counts and stale-rate banner age details.
    var stripEl = document.getElementById("tx-summary-strip");
    var totalCountAttr = stripEl ? parseInt(stripEl.getAttribute("data-total-count") || "0", 10) : 0;
    var dataRows = Array.prototype.slice.call(document.querySelectorAll("tr.tx-data-row"));

    if (dataRows.length > 0) {
        var incomeCount = 0;
        var expenseCount = 0;
        var staleAges = [];

        dataRows.forEach(function (row) {
            var rType = row.getAttribute("data-tx-row-type") || "";
            if (rType === "income") {
                incomeCount += 1;
            } else if (rType === "expense") {
                expenseCount += 1;
            }
            if (row.getAttribute("data-tx-is-stale") === "true") {
                var staleLabel = row.getAttribute("data-tx-stale-label") || "";
                var match = staleLabel.match(/(\d+h\s+old)/i);
                if (match && match[1] && staleAges.indexOf(match[1]) === -1) {
                    staleAges.push(match[1]);
                }
            }
        });

        if (totalCountAttr <= dataRows.length) {
            var incCountEl = document.getElementById("summary-income-count");
            var expCountEl = document.getElementById("summary-expense-count");
            var netCountEl = document.getElementById("summary-net-count");
            var netTxCount = incomeCount + expenseCount;
            if (incCountEl) {
                incCountEl.textContent = incomeCount + " transaction" + (incomeCount === 1 ? "" : "s");
            }
            if (expCountEl) {
                expCountEl.textContent = expenseCount + " transaction" + (expenseCount === 1 ? "" : "s");
            }
            if (netCountEl) {
                netCountEl.textContent = netTxCount + " transaction" + (netTxCount === 1 ? "" : "s");
            }
        }

        if (staleAges.length > 0) {
            var staleText = document.getElementById("stale-rates-banner-text");
            if (staleText) {
                staleText.textContent = "Some exchange rates are " + staleAges[0] + ".";
            }
        }
    }

    // Auto-submit filter form via GET: debounced for search input, immediate for selects and date inputs
    var filterForm = document.getElementById("transaction-filter-form");
    if (filterForm) {
        var qInput = document.getElementById("filter-search-notes");
        var startInput = document.getElementById("filter-start-date");
        var endInput = document.getElementById("filter-end-date");
        var walletFilter = document.getElementById("filter-wallet");
        var typeFilter = document.getElementById("filter-type");
        var applyCol = document.getElementById("apply-filters-col");
        var searchCol = document.getElementById("search-filter-col");
        var filterCollapseEl = document.getElementById("transaction-filter-collapse");
        var filterToggleBtn = document.getElementById("btn-toggle-filters");

        if (filterCollapseEl) {
            filterCollapseEl.addEventListener("show.bs.collapse", function () {
                filterForm.classList.add("tx-filters-expanded");
            });
            filterCollapseEl.addEventListener("hide.bs.collapse", function () {
                filterForm.classList.remove("tx-filters-expanded");
            });
        }

        function updateActiveFilterBadge() {
            if (!filterToggleBtn) return;
            var filterFields = [
                qInput,
                startInput,
                endInput,
                walletFilter,
                typeFilter,
                document.getElementById("filter-scope"),
                document.getElementById("filter-category"),
                document.getElementById("filter-currency")
            ];
            var activeCount = 0;
            filterFields.forEach(function (field) {
                if (field && String(field.value || "").trim() !== "") {
                    activeCount += 1;
                }
            });
            var badge = document.getElementById("active-filters-indicator");
            if (activeCount > 0) {
                if (!badge) {
                    badge = document.createElement("span");
                    badge.id = "active-filters-indicator";
                    badge.className = "tx-chip tx-chip-shared ms-1 py-0 px-2";
                    var chevron = filterToggleBtn.querySelector(".filter-chevron");
                    if (chevron) {
                        filterToggleBtn.insertBefore(badge, chevron);
                    } else {
                        filterToggleBtn.appendChild(badge);
                    }
                }
                badge.textContent = String(activeCount);
            } else if (badge) {
                badge.remove();
            }
        }

        var searchDebounceTimeout = null;
        if (qInput) {
            var shouldRefocusSearch = false;
            try {
                if (sessionStorage.getItem("tx_search_autofocus") === "1") {
                    shouldRefocusSearch = true;
                    sessionStorage.removeItem("tx_search_autofocus");
                }
            } catch (e) {}
            var urlParams = new URLSearchParams(window.location.search);
            if (urlParams.has("q") && (!transferModalEl || transferModalEl.getAttribute("data-auto-open") !== "true")) {
                shouldRefocusSearch = true;
            }
            if (shouldRefocusSearch) {
                qInput.focus();
                var valLen = (qInput.value || "").length;
                try {
                    qInput.setSelectionRange(valLen, valLen);
                } catch (e) {}
            }

            qInput.addEventListener("input", function () {
                clearTimeout(searchDebounceTimeout);
                searchDebounceTimeout = setTimeout(function () {
                    try {
                        sessionStorage.setItem("tx_search_autofocus", "1");
                    } catch (e) {}
                    filterForm.submit();
                    qInput.focus();
                    var curLen = (qInput.value || "").length;
                    try {
                        qInput.setSelectionRange(curLen, curLen);
                    } catch (e) {}
                }, 400);
            });
        }

        function updateDateApplyVisibility() {
            var startDirty = startInput && startInput.value !== startInput.defaultValue;
            var endDirty = endInput && endInput.value !== endInput.defaultValue;
            if (applyCol) {
                if (startDirty || endDirty) {
                    applyCol.classList.remove("d-none");
                    applyCol.classList.add("d-flex");
                    if (searchCol) {
                        searchCol.classList.remove("col-lg-4");
                        searchCol.classList.add("col-lg-3");
                    }
                } else {
                    applyCol.classList.add("d-none");
                    applyCol.classList.remove("d-flex");
                    if (searchCol) {
                        searchCol.classList.add("col-lg-4");
                        searchCol.classList.remove("col-lg-3");
                    }
                }
            }
            updateActiveFilterBadge();
        }

        [startInput, endInput].forEach(function (dateEl) {
            if (!dateEl) return;
            dateEl.addEventListener("input", updateDateApplyVisibility);
        });

        [startInput, endInput, walletFilter, typeFilter].forEach(function (el) {
            if (!el) return;
            el.addEventListener("change", function () {
                updateActiveFilterBadge();
                filterForm.submit();
            });
        });

        [
            document.getElementById("filter-scope"),
            document.getElementById("filter-category"),
            document.getElementById("filter-currency")
        ].forEach(function (el) {
            if (!el) return;
            el.addEventListener("change", updateActiveFilterBadge);
        });

        // Month picker button & native month input integration
        var monthPickerBtn = document.getElementById("btn-month-picker-label");
        var nativeMonthInput = document.getElementById("native-month-input");

        if (monthPickerBtn && nativeMonthInput) {
            monthPickerBtn.addEventListener("click", function (e) {
                e.preventDefault();
                if (typeof nativeMonthInput.showPicker === "function") {
                    try {
                        nativeMonthInput.showPicker();
                    } catch (err) {
                        nativeMonthInput.click();
                    }
                } else {
                    nativeMonthInput.click();
                    nativeMonthInput.focus();
                }
            });

            nativeMonthInput.addEventListener("change", function () {
                var chosen = (nativeMonthInput.value || "").trim();
                if (chosen) {
                    var url = new URL(window.location.href);
                    url.searchParams.set("month", chosen);
                    url.searchParams.delete("page");
                    url.searchParams.delete("start_date");
                    url.searchParams.delete("end_date");
                    window.location.href = url.pathname + url.search;
                }
            });
        }
    }
});
