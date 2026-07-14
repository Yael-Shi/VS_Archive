(function () {
  function getDateEntryRoot() {
    return document.getElementById("archiveDateEntry");
  }

  function getPrecisionSelect(entry) {
    if (!entry) return null;
    const form = entry.closest("form");
    if (form) {
      return form.querySelector('select[name="date_precision"]');
    }
    return document.getElementById("date_precision");
  }

  function precisionValue(entry) {
    const precisionEl = getPrecisionSelect(entry);
    return precisionEl ? precisionEl.value : "UNKNOWN";
  }

  function setGroupVisible(group, visible) {
    group.hidden = !visible;
    group.querySelectorAll("input, select, textarea").forEach((field) => {
      field.disabled = !visible;
    });
  }

  function updateArchiveDateEntry() {
    const entry = getDateEntryRoot();
    if (!entry) return;

    const precision = precisionValue(entry);
    const showMonth =
      precision === "MONTH" ||
      precision === "EXACT_DAY" ||
      precision === "RANGE" ||
      precision === "RANGE_MONTH";
    const showDay = precision === "EXACT_DAY" || precision === "RANGE";

    entry.querySelectorAll("[data-date-precision-group]").forEach((group) => {
      const allowed = (group.getAttribute("data-date-precision-group") || "")
        .split(/\s+/)
        .filter(Boolean);
      const groupActive = allowed.includes(precision);
      setGroupVisible(group, groupActive);

      if (!groupActive) {
        return;
      }

      group.querySelectorAll('[data-date-part="month"]').forEach((part) => {
        part.hidden = !showMonth;
        part.querySelectorAll("input").forEach((field) => {
          field.disabled = !showMonth;
        });
      });

      group.querySelectorAll('[data-date-part="day"]').forEach((part) => {
        part.hidden = !showDay;
        part.querySelectorAll("input").forEach((field) => {
          field.disabled = !showDay;
        });
      });
    });
  }

  function collectArchiveDateMeta(target) {
    const entry = getDateEntryRoot();
    if (!entry) return;

    const precisionEl = getPrecisionSelect(entry);
    if (precisionEl) {
      target.date_precision = precisionEl.value || "UNKNOWN";
    }

    const fieldNames = [
      "date_start_year",
      "date_start_month",
      "date_start_day",
      "date_end_year",
      "date_end_month",
      "date_end_day",
    ];

    fieldNames.forEach((name) => {
      const field = entry.querySelector(`input[name="${name}"]:not([disabled])`);
      if (!field) return;
      const value = field.value.trim();
      if (value) target[name] = value;
    });
  }

  function initArchiveDateEntry() {
    const entry = getDateEntryRoot();
    if (!entry || entry.dataset.dateEntryInit === "1") return;

    const precisionEl = getPrecisionSelect(entry);
    if (!precisionEl) return;

    entry.dataset.dateEntryInit = "1";
    precisionEl.addEventListener("change", updateArchiveDateEntry);
    updateArchiveDateEntry();
  }

  document.addEventListener("DOMContentLoaded", initArchiveDateEntry);

  window.vsArchiveDateEntry = {
    update: updateArchiveDateEntry,
    collectMeta: collectArchiveDateMeta,
  };
})();
