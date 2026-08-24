(function () {
  "use strict";

  var DESKTOP_MEDIA_QUERY = "(min-width: 1180px)";
  var FALLBACK_ACTION_SPACING_PX = 16;
  var MAX_SIGNATURE_WIDTH_PX = 410;

  /*
   * Floor for a still-recognizable 16:9 watermark at opacity 0.22.
   * 160px wide => 90px tall, ~39% of the 410px cap.
   */
  var MIN_SIGNATURE_WIDTH_PX = 160;

  var ACTION_GROUP_SELECTORS = [
    ".archive-detail-manual-text-navigation-actions",
    ".archive-detail-manual-text-staff-management-actions",
  ];

  function qs(selector) {
    return document.querySelector(selector);
  }

  function isDesktop() {
    return window.matchMedia(DESKTOP_MEDIA_QUERY).matches;
  }

  function readSpacingPx() {
    var styles = window.getComputedStyle(document.documentElement);
    var parsed = parseFloat(styles.getPropertyValue("--space-4"));

    if (isNaN(parsed)) {
      return FALLBACK_ACTION_SPACING_PX;
    }

    return parsed;
  }

  function readBorderWidths(element) {
    var styles = window.getComputedStyle(element);
    var left = parseFloat(styles.borderLeftWidth);
    var top = parseFloat(styles.borderTopWidth);
    var bottom = parseFloat(styles.borderBottomWidth);

    return {
      left: isNaN(left) ? 0 : left,
      top: isNaN(top) ? 0 : top,
      bottom: isNaN(bottom) ? 0 : bottom,
    };
  }

  function overlapsSide(rect, sideLeft, sideRight) {
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      rect.left < sideRight &&
      rect.right > sideLeft
    );
  }

  function relevantActionBottom(sideLeft, sideRight) {
    var bottom = null;
    var i;

    for (i = 0; i < ACTION_GROUP_SELECTORS.length; i += 1) {
      var group = qs(ACTION_GROUP_SELECTORS[i]);

      if (!group) {
        continue;
      }

      var rect = group.getBoundingClientRect();

      if (!overlapsSide(rect, sideLeft, sideRight)) {
        continue;
      }

      if (bottom === null || rect.bottom > bottom) {
        bottom = rect.bottom;
      }
    }

    return bottom;
  }

  function clearInlinePosition(signature) {
    signature.style.left = "";
    signature.style.top = "";
    signature.style.width = "";
    signature.style.transform = "";
  }

  function hideSignature(signature) {
    signature.style.display = "none";
    clearInlinePosition(signature);
  }

  function clamp(value, min, max) {
    if (value < min) {
      return min;
    }

    if (value > max) {
      return max;
    }

    return value;
  }

  function positionSignature(page, body, signature) {
    if (!isDesktop()) {
      hideSignature(signature);
      return;
    }

    var cardRect = page.getBoundingClientRect();
    var bodyRect = body.getBoundingClientRect();
    var borders = readBorderWidths(page);
    var spacing = readSpacingPx();

    var sideLeft = cardRect.left;
    var sideRight = bodyRect.left;
    var sideWidth = sideRight - sideLeft;
    var availableWidth = sideWidth - 2 * spacing;

    if (availableWidth < MIN_SIGNATURE_WIDTH_PX) {
      hideSignature(signature);
      return;
    }

    /*
     * The signature is absolutely positioned in the MANUAL_TEXT card's
     * padding-box coordinate system. Keep the card height fractional so zoom
     * and device-pixel rounding cannot create false fit failures.
     */
    var cardHeight = cardRect.height - borders.top - borders.bottom;
    var maxBottomInCard = cardHeight - spacing;

    var safeTopInCard = spacing;
    var actionBottom = relevantActionBottom(sideLeft, sideRight);

    if (actionBottom !== null) {
      var actionTopInCard =
        actionBottom - cardRect.top - borders.top + spacing;

      if (actionTopInCard > safeTopInCard) {
        safeTopInCard = actionTopInCard;
      }
    }

    var availableHeight = maxBottomInCard - safeTopInCard;

    if (!(availableHeight > 0)) {
      hideSignature(signature);
      return;
    }

    var fittingWidth = Math.min(
      MAX_SIGNATURE_WIDTH_PX,
      availableWidth,
      availableHeight * 16 / 9
    );

    if (fittingWidth < MIN_SIGNATURE_WIDTH_PX) {
      hideSignature(signature);
      return;
    }

    var sideCenterInCard =
      sideLeft + sideWidth / 2 - cardRect.left - borders.left;

    /*
     * Derive height from the same fractional 16:9 width used for fitting.
     * Do not use integer-rounded DOM height measurements here: browser zoom
     * previously caused a fitting signature to be rejected by fractions of
     * a CSS pixel.
     */
    var signatureHeight = fittingWidth * 9 / 16;

    var preferredViewportTop =
      (window.innerHeight - signatureHeight) / 2;

    var desiredTopInCard =
      preferredViewportTop - cardRect.top - borders.top;

    var maxTopInCard =
      cardHeight - signatureHeight - spacing;

    if (safeTopInCard > maxTopInCard) {
      hideSignature(signature);
      return;
    }

    var finalTopInCard = clamp(
      desiredTopInCard,
      safeTopInCard,
      maxTopInCard
    );

    signature.style.display = "";
    signature.style.width = fittingWidth + "px";
    signature.style.left = sideCenterInCard + "px";
    signature.style.top = finalTopInCard + "px";
    signature.style.transform = "translateX(-50%)";
  }

  function bindMediaListener(media, onChange) {
    if (!media) {
      return;
    }

    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", onChange);
    } else if (typeof media.addListener === "function") {
      media.addListener(onChange);
    }
  }

  function initManualTextSignature() {
    var page = qs(".archive-detail-page--manual-text");
    var body = qs(".archive-detail-manual-text-body");
    var signature = qs(".archive-detail-manual-text-signature");
    var actionTop = qs(".archive-detail-manual-text-top");

    if (!page || !body || !signature) {
      return;
    }

    var frame = 0;

    function schedulePosition() {
      if (frame) {
        return;
      }

      frame = window.requestAnimationFrame(function () {
        frame = 0;
        positionSignature(page, body, signature);
      });
    }

    if (typeof ResizeObserver === "function") {
      var observer = new ResizeObserver(schedulePosition);
      observer.observe(page);
      observer.observe(body);

      if (actionTop) {
        observer.observe(actionTop);
      }

      var i;
      for (i = 0; i < ACTION_GROUP_SELECTORS.length; i += 1) {
        var group = qs(ACTION_GROUP_SELECTORS[i]);

        if (group) {
          observer.observe(group);
        }
      }
    }

    window.addEventListener("resize", schedulePosition);
    window.addEventListener("scroll", schedulePosition, { passive: true });
    bindMediaListener(
      window.matchMedia(DESKTOP_MEDIA_QUERY),
      schedulePosition
    );

    schedulePosition();
  }

  if (document.readyState === "loading") {
    document.addEventListener(
      "DOMContentLoaded",
      initManualTextSignature
    );
  } else {
    initManualTextSignature();
  }
})();
