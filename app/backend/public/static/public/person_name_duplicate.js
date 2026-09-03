(function (global) {
  const ERROR_CODE = "PERSON_NAME_CANDIDATES";

  function isConflictPayload(payload) {
    return Boolean(
      payload &&
        payload.error_code === ERROR_CODE &&
        Array.isArray(payload.person_name_conflicts)
    );
  }

  function collectForceCreateKeys(root) {
    const scope = root || document;
    return Array.from(
      scope.querySelectorAll('input[name="force_create_person"]:checked')
    )
      .map((input) => String(input.value || "").trim().toLowerCase())
      .filter((key) => /^[0-9a-f]{64}$/.test(key));
  }

  function appendText(el, text) {
    el.appendChild(document.createTextNode(text));
  }

  function renderConflicts(host, payload) {
    if (!host) return;
    host.replaceChildren();
    host.hidden = false;
    host.classList.add("is-visible");

    const lead = document.createElement("p");
    lead.className = "person-name-duplicate-warning__lead";
    appendText(
      lead,
      payload.error ||
        "נמצאו רשומות אדם קיימות עם אותו שם. בחרו אדם קיים בבורר והסירו את השם משדה ההוספה, או אשרו יצירת אדם חדש במכוון עבור כל שם מסומן."
    );
    host.appendChild(lead);

    const tokens = document.createElement("ul");
    tokens.className = "person-name-duplicate-warning__tokens";
    (payload.person_name_conflicts || []).forEach((conflict) => {
      const item = document.createElement("li");
      item.className = "person-name-duplicate-warning__token";

      const nameLine = document.createElement("p");
      appendText(nameLine, "שם שהוזן: ");
      const strong = document.createElement("strong");
      appendText(strong, conflict.submitted_name || "");
      nameLine.appendChild(strong);
      item.appendChild(nameLine);

      const candidates = document.createElement("ul");
      candidates.className = "person-name-duplicate-warning__candidates";
      (conflict.candidates || []).forEach((candidate) => {
        const row = document.createElement("li");
        appendText(row, candidate.name || "");
        if (candidate.aliases && candidate.aliases.length) {
          appendText(row, " (" + candidate.aliases.join(", ") + ")");
        }
        appendText(row, " — מזהה " + String(candidate.id) + " — ");
        const link = document.createElement("a");
        link.href = "/archive/manage/people/" + String(candidate.id) + "/edit/";
        appendText(link, "עריכת אדם");
        row.appendChild(link);
        candidates.appendChild(row);
      });
      item.appendChild(candidates);

      const label = document.createElement("label");
      label.className = "person-name-duplicate-warning__ack";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "force_create_person";
      checkbox.value = conflict.token_key || "";
      checkbox.checked = conflict.needs_confirmation === false;
      label.appendChild(checkbox);
      appendText(
        label,
        ' יצירת אדם חדש בכל זאת עבור "' + (conflict.submitted_name || "") + '"'
      );
      item.appendChild(label);
      tokens.appendChild(item);
    });
    host.appendChild(tokens);
  }

  global.vsArchivePersonNameDuplicate = {
    ERROR_CODE: ERROR_CODE,
    isConflictPayload: isConflictPayload,
    collectForceCreateKeys: collectForceCreateKeys,
    renderConflicts: renderConflicts,
  };
})(window);
