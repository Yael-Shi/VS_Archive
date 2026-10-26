(function () {
  function setCollapsed(root, collapsed) {
    var button = root.querySelector("[data-name-list-toggle]");
    if (!button) {
      return;
    }
    root.classList.toggle("is-collapsed", collapsed);
    button.hidden = false;
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    var hiddenCount = button.getAttribute("data-hidden-count") || "0";
    button.textContent = collapsed ? "ראו עוד (" + hiddenCount + ")" : "הצג פחות";
  }

  document.querySelectorAll(".expandable-name-list").forEach(function (root) {
    setCollapsed(root, true);
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-name-list-toggle]");
    if (!button) {
      return;
    }
    var root = button.closest(".expandable-name-list");
    if (!root) {
      return;
    }
    setCollapsed(root, !root.classList.contains("is-collapsed"));
  });
})();
