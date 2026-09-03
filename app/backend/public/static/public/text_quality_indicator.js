(function () {
  function closeAll(exceptWrap) {
    document.querySelectorAll("[data-text-quality-indicator]").forEach(function (root) {
      var wrap = root.querySelector(".text-quality-indicator__info-wrap");
      var button = root.querySelector(".text-quality-indicator__info");
      if (!wrap || wrap === exceptWrap) {
        return;
      }
      wrap.classList.remove("is-open");
      if (button) {
        button.setAttribute("aria-expanded", "false");
      }
    });
  }

  function initRoot(root) {
    if (root.getAttribute("data-text-quality-ready") === "1") {
      return;
    }
    root.setAttribute("data-text-quality-ready", "1");
    var wrap = root.querySelector(".text-quality-indicator__info-wrap");
    var button = root.querySelector(".text-quality-indicator__info");
    if (!wrap || !button) {
      return;
    }
    button.addEventListener("click", function (event) {
      event.preventDefault();
      var open = wrap.classList.toggle("is-open");
      button.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        closeAll(wrap);
      }
    });
  }

  document.querySelectorAll("[data-text-quality-indicator]").forEach(initRoot);

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-text-quality-indicator]")) {
      return;
    }
    closeAll(null);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeAll(null);
    }
  });
})();
