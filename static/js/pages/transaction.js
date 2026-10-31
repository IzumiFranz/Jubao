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
});
