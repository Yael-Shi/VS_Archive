(function () {
  if (window.__vsArchiveSearchableMultiSelectBound) {
    return;
  }
  window.__vsArchiveSearchableMultiSelectBound = true;

  function applyFilter(field) {
    const searchInput = field.querySelector("[data-searchable-multi-select-search]");
    const select = field.querySelector("[data-searchable-multi-select-control]");
    const emptyMessage = field.querySelector("[data-searchable-multi-select-empty]");
    if (!searchInput || !select || !emptyMessage) {
      return;
    }

    const query = searchInput.value.trim().toLocaleLowerCase();
    let matchCount = 0;

    Array.from(select.options).forEach(function (option) {
      const matches =
        !query || option.textContent.toLocaleLowerCase().includes(query);

      if (matches) {
        matchCount += 1;
      }

      const shouldHide = !matches && !option.selected;
      option.hidden = shouldHide;
      option.style.display = shouldHide ? "none" : "";
    });

    emptyMessage.hidden = !query || matchCount > 0;
  }

  function bindField(field) {
    if (field.dataset.searchableMultiSelectReady === "1") {
      return;
    }
    field.dataset.searchableMultiSelectReady = "1";

    const searchInput = field.querySelector("[data-searchable-multi-select-search]");
    const select = field.querySelector("[data-searchable-multi-select-control]");
    if (!searchInput || !select) {
      return;
    }

    function onFilter() {
      applyFilter(field);
    }

    searchInput.addEventListener("input", onFilter);
    select.addEventListener("change", onFilter);
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        select.focus();
      }
    });
  }

  function boot() {
    document.querySelectorAll("[data-searchable-multi-select]").forEach(bindField);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
