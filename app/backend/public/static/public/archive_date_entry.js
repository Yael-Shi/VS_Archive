(function () {
  var MOBILE_MEDIA_QUERY = "(max-width: 640px)";

  var DATE_COMPONENT_FIELD_NAMES = [
    "date_start_day",
    "date_start_month",
    "date_start_year",
    "date_end_day",
    "date_end_month",
    "date_end_year",
  ];

  var ISO_FIELD_NAMES = ["date_start", "date_end"];

  function getDateEntryRoot() {
    return document.getElementById("archiveDateEntry");
  }

  function getPrecisionSelect(entry) {
    if (!entry) return null;
    var form = entry.closest("form");
    if (form) {
      return form.querySelector('select[name="date_precision"]');
    }
    return document.getElementById("date_precision");
  }

  function precisionValue(entry) {
    var precisionEl = getPrecisionSelect(entry);
    return precisionEl ? precisionEl.value : "UNKNOWN";
  }

  function isMobileUi() {
    return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
  }

  function getUiAreas(entry) {
    return {
      desktop: entry.querySelector('[data-date-ui="desktop"]'),
      mobile: entry.querySelector('[data-date-ui="mobile"]'),
    };
  }

  function sanitizeArchiveDateDigits(rawValue, maxLength) {
    var digits = String(rawValue == null ? "" : rawValue).replace(/\D/g, "");
    if (typeof maxLength === "number" && maxLength > 0) {
      return digits.slice(0, maxLength);
    }
    return digits;
  }

  function maxLengthForField(field) {
    return field.maxLength > 0 ? field.maxLength : null;
  }

  function bindArchiveDateDigitInput(field) {
    if (field.dataset.dateDigitInputInit === "1") {
      return;
    }
    field.dataset.dateDigitInputInit = "1";
    field.addEventListener("input", function () {
      var sanitized = sanitizeArchiveDateDigits(
        field.value,
        maxLengthForField(field)
      );
      if (field.value !== sanitized) {
        field.value = sanitized;
      }
    });
  }

  function bindArchiveDateDigitInputs(area) {
    if (!area) return;
    area
      .querySelectorAll('input[type="text"][inputmode="numeric"]')
      .forEach(bindArchiveDateDigitInput);
  }

  function splitIsoDate(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || "").trim());
    if (!match) {
      return { year: "", month: "", day: "" };
    }
    return {
      year: match[1],
      month: String(parseInt(match[2], 10)),
      day: String(parseInt(match[3], 10)),
    };
  }

  function mergeIsoIntoValues(values) {
    if (values.date_start && !values.date_start_year) {
      var startParts = splitIsoDate(values.date_start);
      values.date_start_year = startParts.year;
      values.date_start_month = startParts.month;
      values.date_start_day = startParts.day;
    }
    if (values.date_end && !values.date_end_year) {
      var endParts = splitIsoDate(values.date_end);
      values.date_end_year = endParts.year;
      values.date_end_month = endParts.month;
      values.date_end_day = endParts.day;
    }
    return values;
  }

  function setFieldsDisabled(container, disabled) {
    if (!container) return;
    container.querySelectorAll("input, select, textarea").forEach(function (field) {
      field.disabled = disabled;
    });
  }

  function setPrecisionGroupState(group, active) {
    group.hidden = !active;
    setFieldsDisabled(group, !active);
  }

  function updateDateUi() {
    var entry = getDateEntryRoot();
    if (!entry) return;

    var precision = precisionValue(entry);
    var mobile = isMobileUi();
    var areas = getUiAreas(entry);
    var activeMode = mobile ? "mobile" : "desktop";

    ["desktop", "mobile"].forEach(function (mode) {
      var area = areas[mode];
      if (!area) return;

      var areaActive = mode === activeMode;
      area.hidden = !areaActive;

      area.querySelectorAll("[data-date-precision-group]").forEach(function (group) {
        var groupPrecision = group.getAttribute("data-date-precision-group") || "";
        var groupActive =
          areaActive && precision !== "UNKNOWN" && groupPrecision === precision;
        setPrecisionGroupState(group, groupActive);
      });

      if (!areaActive) {
        setFieldsDisabled(area, true);
      }
    });

    entry.dataset.dateUiMode = activeMode;
  }

  function extractLogicalValues(entry, uiMode) {
    var area = entry.querySelector('[data-date-ui="' + uiMode + '"]');
    var values = {
      date_start_year: "",
      date_start_month: "",
      date_start_day: "",
      date_end_year: "",
      date_end_month: "",
      date_end_day: "",
      date_start: "",
      date_end: "",
    };
    if (!area) return values;

    if (uiMode === "desktop") {
      area.querySelectorAll("input[name]").forEach(function (field) {
        if (field.disabled) return;
        var name = field.name;
        if (!(name in values)) return;
        values[name] = field.value.trim();
      });
      return values;
    }

    DATE_COMPONENT_FIELD_NAMES.forEach(function (name) {
      var field = area.querySelector('input[name="' + name + '"]:not([disabled])');
      if (field) {
        values[name] = field.value.trim();
      }
    });
    return values;
  }

  function applyLogicalValues(entry, uiMode, values) {
    var area = entry.querySelector('[data-date-ui="' + uiMode + '"]');
    if (!area) return;

    values = mergeIsoIntoValues(Object.assign({}, values));

    if (uiMode === "desktop") {
      area.querySelectorAll("input, select, textarea").forEach(function (field) {
        var name = field.name;
        if (!name || !(name in values)) return;
        field.value = values[name] || "";
      });
      area.querySelectorAll('[data-date-native="start-date"], [data-date-native="end-date"]').forEach(
        function (field) {
          if (field.getAttribute("data-date-native") === "end-date") {
            field.value = values.date_end || "";
          } else {
            field.value = values.date_start || "";
          }
        }
      );
      return;
    }

    DATE_COMPONENT_FIELD_NAMES.forEach(function (name) {
      area.querySelectorAll('input[name="' + name + '"]').forEach(function (field) {
        field.value = values[name] || "";
      });
    });
  }

  function prepareSubmission(entry) {
    updateDateUi();
  }

  function handleViewportChange(entry) {
    var nowMobile = isMobileUi();
    var previousMode = entry.dataset.dateUiMode;
    var nowMode = nowMobile ? "mobile" : "desktop";

    if (!previousMode || previousMode === nowMode) {
      updateDateUi();
      return;
    }

    var values = extractLogicalValues(entry, previousMode);
    applyLogicalValues(entry, nowMode, values);
    updateDateUi();
  }

  function collectArchiveDateMeta(target) {
    var entry = getDateEntryRoot();
    if (!entry) return;

    prepareSubmission(entry);

    var precisionEl = getPrecisionSelect(entry);
    if (precisionEl) {
      target.date_precision = precisionEl.value || "UNKNOWN";
    }

    var activeUi = isMobileUi() ? "mobile" : "desktop";
    var area = entry.querySelector('[data-date-ui="' + activeUi + '"]');
    if (!area) return;

    DATE_COMPONENT_FIELD_NAMES.concat(ISO_FIELD_NAMES).forEach(function (name) {
      var field = area.querySelector('input[name="' + name + '"]:not([disabled])');
      if (!field) return;
      var value = field.value.trim();
      if (value) target[name] = value;
    });
  }

  function bindFormSubmission(entry) {
    var form = entry.closest("form");
    if (!form || form.dataset.dateEntrySubmitBound === "1") return;
    form.dataset.dateEntrySubmitBound = "1";
    form.addEventListener("submit", function () {
      prepareSubmission(entry);
    });
  }

  function initArchiveDateEntry() {
    var entry = getDateEntryRoot();
    if (!entry || entry.dataset.dateEntryInit === "1") return;

    var precisionEl = getPrecisionSelect(entry);
    if (!precisionEl) return;

    entry.dataset.dateEntryInit = "1";
    var areas = getUiAreas(entry);
    bindArchiveDateDigitInputs(areas.mobile);
    bindArchiveDateDigitInputs(areas.desktop);
    bindFormSubmission(entry);

    precisionEl.addEventListener("change", updateDateUi);

    if (window.matchMedia) {
      var media = window.matchMedia(MOBILE_MEDIA_QUERY);
      var onChange = function () {
        handleViewportChange(entry);
      };
      if (typeof media.addEventListener === "function") {
        media.addEventListener("change", onChange);
      } else if (typeof media.addListener === "function") {
        media.addListener(onChange);
      }
    }

    handleViewportChange(entry);
  }

  document.addEventListener("DOMContentLoaded", initArchiveDateEntry);

  window.vsArchiveDateEntry = {
    update: updateDateUi,
    collectMeta: collectArchiveDateMeta,
    sanitizeDigits: sanitizeArchiveDateDigits,
    prepareSubmission: prepareSubmission,
    mobileMediaQuery: MOBILE_MEDIA_QUERY,
  };
})();
