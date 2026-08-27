(function () {
  var MOBILE_MEDIA_QUERY = "(max-width: 640px)";
  var WIDGET_SELECTOR = "[data-archive-date-widget]";
  var viewportBound = false;

  var DATE_COMPONENT_FIELD_NAMES = [
    "date_start_day",
    "date_start_month",
    "date_start_year",
    "date_end_day",
    "date_end_month",
    "date_end_year",
  ];

  var ISO_FIELD_NAMES = ["date_start", "date_end"];

  function getWidgetRoots() {
    return Array.prototype.slice.call(
      document.querySelectorAll(WIDGET_SELECTOR)
    );
  }

  function resolveWidget(hint) {
    if (hint && hint.nodeType === 1) {
      if (hint.matches && hint.matches(WIDGET_SELECTOR)) {
        return hint;
      }
      var scoped = hint.closest ? hint.closest(WIDGET_SELECTOR) : null;
      if (scoped) return scoped;
    }
    var widgets = getWidgetRoots();
    if (widgets.length === 1) {
      return widgets[0];
    }
    var unprefixedEntry = document.getElementById("archiveDateEntry");
    if (unprefixedEntry) {
      return unprefixedEntry.closest
        ? unprefixedEntry.closest(WIDGET_SELECTOR) || unprefixedEntry
        : unprefixedEntry;
    }
    return null;
  }

  function getDateEntryRoot(widget) {
    if (!widget) return null;
    if (widget.classList && widget.classList.contains("archive-date-entry")) {
      return widget;
    }
    return widget.querySelector(".archive-date-entry") || widget;
  }

  function getPrecisionSelect(widget) {
    if (!widget) return null;
    return widget.querySelector('select[name="date_precision"]');
  }

  function precisionValue(widget) {
    var precisionEl = getPrecisionSelect(widget);
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

  function updateOneDateUi(widget) {
    widget = resolveWidget(widget);
    if (!widget) return;
    var entry = getDateEntryRoot(widget);
    if (!entry) return;

    var precision = precisionValue(widget);
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

  function updateDateUi(widget) {
    if (widget) {
      updateOneDateUi(widget);
      return;
    }
    getWidgetRoots().forEach(updateOneDateUi);
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

  function prepareSubmission(widget) {
    updateOneDateUi(widget);
  }

  function handleViewportChange(widget) {
    widget = resolveWidget(widget);
    if (!widget) return;
    var entry = getDateEntryRoot(widget);
    if (!entry) return;

    var nowMobile = isMobileUi();
    var previousMode = entry.dataset.dateUiMode;
    var nowMode = nowMobile ? "mobile" : "desktop";

    if (!previousMode || previousMode === nowMode) {
      updateOneDateUi(widget);
      return;
    }

    var values = extractLogicalValues(entry, previousMode);
    applyLogicalValues(entry, nowMode, values);
    updateOneDateUi(widget);
  }

  function collectArchiveDateMeta(target, widgetHint) {
    var widget = resolveWidget(widgetHint);
    if (!widget) return;

    prepareSubmission(widget);

    var precisionEl = getPrecisionSelect(widget);
    if (precisionEl) {
      target.date_precision = precisionEl.value || "UNKNOWN";
    }

    var entry = getDateEntryRoot(widget);
    if (!entry) return;

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

  function bindFormSubmission(widget) {
    var form = widget.closest("form");
    if (!form || form.dataset.dateEntrySubmitBound === "1") return;
    form.dataset.dateEntrySubmitBound = "1";
    form.addEventListener("submit", function () {
      form.querySelectorAll(WIDGET_SELECTOR).forEach(prepareSubmission);
    });
  }

  function bindViewportChange() {
    if (viewportBound || !window.matchMedia) return;
    viewportBound = true;
    var media = window.matchMedia(MOBILE_MEDIA_QUERY);
    var onChange = function () {
      getWidgetRoots().forEach(handleViewportChange);
    };
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", onChange);
    } else if (typeof media.addListener === "function") {
      media.addListener(onChange);
    }
  }

  function initOneWidget(widget) {
    if (!widget || widget.dataset.dateEntryInit === "1") return;

    var precisionEl = getPrecisionSelect(widget);
    if (!precisionEl) return;

    widget.dataset.dateEntryInit = "1";
    var entry = getDateEntryRoot(widget);
    var areas = getUiAreas(entry);
    bindArchiveDateDigitInputs(areas.mobile);
    bindArchiveDateDigitInputs(areas.desktop);
    bindFormSubmission(widget);

    precisionEl.addEventListener("change", function () {
      updateOneDateUi(widget);
    });

    handleViewportChange(widget);
  }

  function initArchiveDateEntry() {
    getWidgetRoots().forEach(initOneWidget);
    bindViewportChange();
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
