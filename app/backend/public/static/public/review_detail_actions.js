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

  function reviewCardByResultId(resultId) {
    if (resultId == null || resultId === "") {
      return null;
    }
    return document.querySelector(
      '[data-review-card][data-result-id="' + String(resultId) + '"]'
    );
  }

  function applyAuthoritativeReviewCards(cards) {
    // Replace only cards in the server payload. Omitted cards keep local edits.
    if (!Array.isArray(cards)) {
      return false;
    }
    var i;
    var item;
    var existing;
    var wrap;
    var next;
    var replaced = 0;
    for (i = 0; i < cards.length; i++) {
      item = cards[i];
      if (!item || item.html == null) {
        continue;
      }
      existing = reviewCardByResultId(item.result_id);
      if (!existing) {
        continue;
      }
      wrap = document.createElement("div");
      wrap.innerHTML = String(item.html).trim();
      next = wrap.querySelector("[data-review-card]");
      if (!next) {
        continue;
      }
      existing.replaceWith(next);
      replaced += 1;
    }
    return replaced > 0;
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

  function isReviewTextarea(el) {
    return !!(el && el.matches && el.matches("textarea.review-textarea"));
  }

  function reviewFormUserEditedFlag(form) {
    if (!form || !form.querySelector) {
      return null;
    }
    return form.querySelector('input[name="text_was_user_edited"]');
  }

  function isFormMarkedUserEdited(form) {
    var flag = reviewFormUserEditedFlag(form);
    return !!(flag && flag.value === "1");
  }

  function markReviewTextareaFormUserEdited(textarea) {
    if (!isReviewTextarea(textarea)) {
      return;
    }
    var flag = reviewFormUserEditedFlag(textarea.form);
    if (flag) {
      flag.value = "1";
    }
  }

  function isBrowserAutofillInputType(inputType) {
    return (
      inputType === "insertFromAutoComplete" ||
      inputType === "insertReplacementText"
    );
  }

  function isContentMutationInputType(inputType) {
    return (
      inputType === "insertText" ||
      inputType === "insertLineBreak" ||
      inputType === "insertParagraph" ||
      inputType === "insertFromPaste" ||
      inputType === "insertFromDrop" ||
      inputType === "insertCompositionText" ||
      inputType === "deleteContentBackward" ||
      inputType === "deleteContentForward" ||
      inputType === "deleteByCut" ||
      inputType === "historyUndo" ||
      inputType === "historyRedo"
    );
  }

  function shouldMarkReviewTextareaDirty(inputType) {
    if (isBrowserAutofillInputType(inputType)) {
      return false;
    }
    return isContentMutationInputType(inputType);
  }

  function restoreReviewTextareasFromServerDefault(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var textareas = scope.querySelectorAll("textarea.review-textarea");
    var i;
    var textarea;
    for (i = 0; i < textareas.length; i++) {
      textarea = textareas[i];
      if (isFormMarkedUserEdited(textarea.form)) {
        continue;
      }
      if (textarea.value !== textarea.defaultValue) {
        textarea.value = textarea.defaultValue;
      }
    }
  }

  function onReviewTextareaContentMutation(event) {
    var target = event.target;
    if (!isReviewTextarea(target)) {
      return;
    }
    if (!shouldMarkReviewTextareaDirty(event.inputType)) {
      return;
    }
    markReviewTextareaFormUserEdited(target);
  }

  function onPageShow() {
    restoreReviewTextareasFromServerDefault(document);
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
    var resultId = card.getAttribute("data-result-id");

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
        if (!applyAuthoritativeReviewCards(data.cards)) {
          throw new Error("הפעולה נכשלה. נסו שוב.");
        }
        var updated = reviewCardByResultId(resultId);
        if (!updated) {
          throw new Error("הפעולה נכשלה. נסו שוב.");
        }
        if (action === "save") {
          setFeedback(
            updated,
            data.text_saved ? "הטקסט נשמר." : "אין שינוי לשמירה.",
            "ok"
          );
          return;
        }
        if (action === "verify") {
          setFeedback(updated, "התעתוק אושר.", "ok");
          return;
        }
        if (action === "reject") {
          setFeedback(updated, "התעתוק נדחה.", "ok");
        }
      })
      .catch(function (err) {
        var current = reviewCardByResultId(resultId) || card;
        setFeedback(
          current,
          (err && err.message) || "הפעולה נכשלה. נסו שוב.",
          "error"
        );
      })
      .then(function () {
        var current = reviewCardByResultId(resultId) || card;
        current.removeAttribute("data-review-busy");
        setBusy(current, false);
      });
  }

  restoreReviewTextareasFromServerDefault(document);
  window.addEventListener("pageshow", onPageShow, false);
  document.addEventListener("beforeinput", onReviewTextareaContentMutation, false);
  document.addEventListener("input", onReviewTextareaContentMutation, false);
  document.addEventListener("submit", onSubmit, false);
})();
