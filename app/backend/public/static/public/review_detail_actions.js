(function () {
  "use strict";

  var ASYNC_HEADER = "XMLHttpRequest";

  function closest(el, selector) {
    while (el && el.nodeType === 1) {
      if (el.matches(selector)) {
        return el;
      }
      el = el.parentElement;
    }
    return null;
  }

  function csrfTokenFrom(form) {
    var input = form.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function setFeedback(card, message, kind) {
    var el = card.querySelector(".review-feedback");
    if (!el) {
      return;
    }
    el.textContent = message || "";
    el.classList.remove(
      "review-feedback--ok",
      "review-feedback--error",
      "review-feedback--busy"
    );
    if (kind) {
      el.classList.add("review-feedback--" + kind);
    }
  }

  function actionButtons(card) {
    return Array.prototype.slice.call(
      card.querySelectorAll("[data-review-action]")
    );
  }

  function setBusy(card, busy) {
    card.classList.toggle("review-result-card--busy", busy);
    actionButtons(card).forEach(function (btn) {
      if (busy) {
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
      } else {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      }
    });
    var textarea = card.querySelector("textarea[name=text]");
    if (textarea) {
      textarea.disabled = !!busy;
    }
  }

  function setVerificationBadge(card, status, label) {
    var badge = card.querySelector(".badge-verify");
    if (!badge) {
      return;
    }
    badge.textContent = label;
    badge.classList.remove("badge-ok", "badge-warn", "badge-bad");
    if (status === "VERIFIED") {
      badge.classList.add("badge-ok");
    } else if (status === "REJECTED") {
      badge.classList.add("badge-bad");
    } else {
      badge.classList.add("badge-warn");
    }
  }

  function applyVerifiedUi(card) {
    var textForm = card.querySelector("[data-review-text-form]");
    var verifyZone = card.querySelector(".review-verify-zone");
    var pendingBadge = card.querySelector(".review-pending-badge");
    var verifyBtn = card.querySelector('[data-review-action="verify"]');
    var saveBtn = card.querySelector('[data-review-action="save"]');
    var verifiedLabel =
      card.getAttribute("data-label-verified") || "אושר";
    var verifiedEditUrl = textForm
      ? textForm.getAttribute("data-verified-edit-url")
      : null;
    var verifiedEditTitle = textForm
      ? textForm.getAttribute("data-verified-edit-title")
      : null;
    var verifiedSaveLabel = textForm
      ? textForm.getAttribute("data-verified-save-label")
      : null;

    card.classList.remove("review-result-card--pending");
    card.removeAttribute("data-pending-review");
    card.setAttribute("data-verification-status", "VERIFIED");
    setVerificationBadge(card, "VERIFIED", verifiedLabel);
    if (pendingBadge) {
      pendingBadge.remove();
    }
    if (verifyZone) {
      verifyZone.remove();
    }
    if (verifyBtn) {
      verifyBtn.remove();
    }
    if (textForm && verifiedEditUrl) {
      textForm.action = verifiedEditUrl;
      textForm.removeAttribute("data-review-text-form");
      textForm.classList.remove("review-pending-text-form");
      textForm.removeAttribute("data-verified-edit-url");
      textForm.removeAttribute("data-verified-edit-title");
      textForm.removeAttribute("data-verified-save-label");

      var title = textForm.querySelector(".review-verified-edit-title");
      var textarea = textForm.querySelector("textarea[name=text]");
      if (!title) {
        title = document.createElement("div");
        title.className = "review-zone-title review-verified-edit-title";
        title.style.marginBottom = "8px";
        if (textarea && textarea.parentNode === textForm) {
          textForm.insertBefore(title, textarea);
        } else {
          textForm.appendChild(title);
        }
      }
      if (verifiedEditTitle) {
        title.textContent = verifiedEditTitle;
      }

      if (saveBtn) {
        if (verifiedSaveLabel) {
          saveBtn.textContent = verifiedSaveLabel;
        }
        saveBtn.removeAttribute("data-review-action");
        // setBusy(true) disabled this before data-review-action was removed;
        // re-enable so post-verify full-page save still works.
        saveBtn.disabled = false;
        saveBtn.removeAttribute("aria-busy");
      }
      if (textarea) {
        textarea.disabled = false;
      }
    }
  }

  function applyRejectedUi(card) {
    var rejectedLabel =
      card.getAttribute("data-label-rejected") || "נדחה בבקרה";
    card.setAttribute("data-verification-status", "REJECTED");
    setVerificationBadge(card, "REJECTED", rejectedLabel);
    var rejectForm = card.querySelector("[data-review-reject-form]");
    if (rejectForm) {
      var zone = closest(rejectForm, ".review-verify-zone");
      if (zone) {
        zone.remove();
      } else {
        rejectForm.remove();
      }
    }
  }

  function parseJsonSafe(text) {
    try {
      return JSON.parse(text);
    } catch (err) {
      return null;
    }
  }

  function actionFromSubmitter(submitter, form) {
    if (submitter && submitter.getAttribute) {
      var explicit = submitter.getAttribute("data-review-action");
      if (explicit) {
        return explicit;
      }
      if (submitter.getAttribute("formaction")) {
        return "verify";
      }
    }
    if (form.hasAttribute("data-review-reject-form")) {
      return "reject";
    }
    return "save";
  }

  function requestUrl(form, submitter) {
    if (submitter && submitter.getAttribute && submitter.getAttribute("formaction")) {
      return submitter.getAttribute("formaction");
    }
    return form.getAttribute("action");
  }

  function asyncFailureMessage(payload) {
    if (payload.data && payload.data.error) {
      return String(payload.data.error).slice(0, 300);
    }
    // Avoid dumping HTML (login/403 pages) into aria-live feedback.
    if (payload.res && payload.res.status === 403) {
      return "אין הרשאה.";
    }
    if (payload.res && payload.res.status === 404) {
      return "לא נמצא.";
    }
    return "הפעולה נכשלה. נסו שוב.";
  }

  function onSubmit(event) {
    var form = event.target;
    if (!form || !form.matches) {
      return;
    }
    var isTextForm = form.matches("[data-review-text-form]");
    var isRejectForm = form.matches("[data-review-reject-form]");
    if (!isTextForm && !isRejectForm) {
      return;
    }

    var card = closest(form, "[data-review-card]");
    if (!card) {
      return;
    }

    var submitter = event.submitter || document.activeElement;
    var action = actionFromSubmitter(submitter, form);
    var url = requestUrl(form, submitter);
    if (!url) {
      return;
    }

    event.preventDefault();
    if (card.getAttribute("data-review-busy") === "1") {
      return;
    }

    // Capture payload before disabling controls — disabled fields are omitted
    // from FormData constructed from a form.
    var body = new FormData(form);

    card.setAttribute("data-review-busy", "1");
    setBusy(card, true);
    setFeedback(card, "שולח…", "busy");

    fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: {
        "X-Requested-With": ASYNC_HEADER,
        "X-CSRFToken": csrfTokenFrom(form),
      },
    })
      .then(function (res) {
        return res.text().then(function (text) {
          return { res: res, data: parseJsonSafe(text), text: text };
        });
      })
      .then(function (payload) {
        var data = payload.data;
        if (!payload.res.ok || !data || !data.ok) {
          throw new Error(asyncFailureMessage(payload));
        }

        if (action === "save") {
          setFeedback(
            card,
            data.text_saved ? "הטקסט נשמר." : "אין שינוי לשמירה.",
            "ok"
          );
          return;
        }
        if (action === "verify") {
          applyVerifiedUi(card);
          setFeedback(card, "התעתוק אושר.", "ok");
          return;
        }
        if (action === "reject") {
          applyRejectedUi(card);
          setFeedback(card, "התעתוק נדחה.", "ok");
        }
      })
      .catch(function (err) {
        setFeedback(
          card,
          (err && err.message) || "הפעולה נכשלה. נסו שוב.",
          "error"
        );
      })
      .then(function () {
        card.removeAttribute("data-review-busy");
        setBusy(card, false);
      });
  }

  document.addEventListener("submit", onSubmit, false);
})();
