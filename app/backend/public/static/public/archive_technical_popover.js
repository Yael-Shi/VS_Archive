(function () {
  "use strict";

  var ROOT_SELECTOR = "[data-archive-technical-popover]";

  function parts(root) {
    return {
      trigger: root.querySelector("[data-archive-technical-toggle]"),
      panel: root.querySelector("[data-archive-technical-panel]"),
      close: root.querySelector("[data-archive-technical-close]"),
    };
  }

  function close(root, focusTrigger) {
    var found = parts(root);
    if (!found.trigger || !found.panel) {
      return;
    }
    root.classList.remove("is-open");
    found.panel.hidden = true;
    found.trigger.setAttribute("aria-expanded", "false");
    if (focusTrigger) {
      found.trigger.focus();
    }
  }

  function closeAll(except, focusTrigger) {
    document.querySelectorAll(ROOT_SELECTOR).forEach(function (root) {
      if (root !== except) {
        close(root, false);
        return;
      }
      if (focusTrigger) {
        close(root, true);
      }
    });
  }

  function open(root) {
    var found = parts(root);
    if (!found.trigger || !found.panel) {
      return;
    }
    closeAll(root, false);
    root.classList.add("is-open");
    found.panel.hidden = false;
    found.trigger.setAttribute("aria-expanded", "true");
    if (found.close) {
      found.close.focus();
    }
  }

  function initRoot(root) {
    if (root.getAttribute("data-archive-technical-ready") === "1") {
      return;
    }
    root.setAttribute("data-archive-technical-ready", "1");
    var found = parts(root);
    if (!found.trigger || !found.panel) {
      return;
    }
    found.panel.hidden = true;
    found.trigger.setAttribute("aria-expanded", "false");

    found.trigger.addEventListener("click", function (event) {
      event.preventDefault();
      if (root.classList.contains("is-open")) {
        close(root, true);
      } else {
        open(root);
      }
    });

    if (found.close) {
      found.close.addEventListener("click", function (event) {
        event.preventDefault();
        close(root, true);
      });
    }
  }

  document.querySelectorAll(ROOT_SELECTOR).forEach(initRoot);

  document.addEventListener("click", function (event) {
    if (event.target.closest(ROOT_SELECTOR)) {
      return;
    }
    closeAll(null, false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var openRoot = document.querySelector(ROOT_SELECTOR + ".is-open");
    if (!openRoot) {
      return;
    }
    closeAll(openRoot, true);
  });

  document.addEventListener("focusin", function (event) {
    if (event.target.closest(ROOT_SELECTOR)) {
      return;
    }
    closeAll(null, false);
  });
})();
