"""Tests for prefixable archive date-entry widgets (IDs, labels, scoped JS)."""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

from django.contrib.auth.models import User
from django.template import TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase

from documents.models import ArchiveItem, PhotoContent
from documents.services.archive_date_input import archive_date_form_data
from documents.services.archive_item_validation import DATE_PRECISION_UI_CHOICES
from documents.services.archive_items import create_manual_text_archive_item
from documents.templatetags.archive_date_widget import archive_date_id_prefix


_DATE_POST_NAMES = (
    "date_precision",
    "date_start",
    "date_end",
    "date_start_day",
    "date_start_month",
    "date_start_year",
    "date_end_day",
    "date_end_month",
    "date_end_year",
)

_MINIDOM_JS = r"""
function parseAttrs(raw) {
  const attrs = {};
  const re = /([:@A-Za-z0-9_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+)))?/g;
  let match;
  while ((match = re.exec(raw || ""))) {
    attrs[match[1]] = match[2] != null ? match[2] : match[3] != null ? match[3] : match[4] != null ? match[4] : "";
  }
  return attrs;
}

const VOID_TAGS = new Set(["input", "br", "hr", "img", "meta", "link"]);

function createEl(tagName, attrs) {
  const el = {
    nodeType: 1,
    tagName: String(tagName).toUpperCase(),
    attrs: Object.assign({}, attrs || {}),
    children: [],
    parent: null,
    _listeners: {},
  };
  Object.defineProperty(el, "id", {
    get() { return this.attrs.id || ""; },
  });
  Object.defineProperty(el, "name", {
    get() { return this.attrs.name || ""; },
  });
  Object.defineProperty(el, "className", {
    get() { return this.attrs.class || ""; },
  });
  Object.defineProperty(el, "classList", {
    get() {
      const self = this;
      return {
        contains(token) {
          return self.className.split(/\s+/).filter(Boolean).includes(token);
        },
      };
    },
  });
  Object.defineProperty(el, "dataset", {
    get() {
      if (!this._dataset) {
        const data = {};
        for (const [key, value] of Object.entries(this.attrs)) {
          if (!key.startsWith("data-")) continue;
          const camel = key.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
          data[camel] = value;
        }
        this._dataset = new Proxy(data, {
          set: (obj, prop, value) => {
            obj[prop] = String(value);
            const attr = "data-" + String(prop).replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
            this.attrs[attr] = String(value);
            return true;
          },
        });
      }
      return this._dataset;
    },
  });
  Object.defineProperty(el, "disabled", {
    get() { return Object.prototype.hasOwnProperty.call(this.attrs, "disabled"); },
    set(value) {
      if (value) this.attrs.disabled = "";
      else delete this.attrs.disabled;
    },
  });
  Object.defineProperty(el, "hidden", {
    get() { return Object.prototype.hasOwnProperty.call(this.attrs, "hidden"); },
    set(value) {
      if (value) this.attrs.hidden = "";
      else delete this.attrs.hidden;
    },
  });
  Object.defineProperty(el, "value", {
    get() {
      if (this.tagName === "SELECT") {
        const selected = this.children.find((child) => child.attrs && Object.prototype.hasOwnProperty.call(child.attrs, "selected"));
        const option = selected || this.children.find((child) => child.tagName === "OPTION");
        return option ? (option.attrs.value || option.textContent || "") : (this.attrs.value || "");
      }
      return this.attrs.value || "";
    },
    set(value) {
      this.attrs.value = String(value);
    },
  });
  Object.defineProperty(el, "maxLength", {
    get() {
      const raw = this.attrs.maxlength;
      return raw ? Number(raw) : -1;
    },
  });
  Object.defineProperty(el, "textContent", {
    get() {
      return (this._text || "") + this.children.map((child) => child.textContent || "").join("");
    },
  });
  el.getAttribute = function (name) {
    if (!Object.prototype.hasOwnProperty.call(this.attrs, name)) return null;
    return this.attrs[name];
  };
  el.matches = function (selector) {
    return matchesSelector(this, selector);
  };
  el.closest = function (selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (matchesSelector(node, selector)) return node;
      node = node.parent;
    }
    return null;
  };
  el.querySelector = function (selector) {
    return this.querySelectorAll(selector)[0] || null;
  };
  el.querySelectorAll = function (selector) {
    return queryAll(this, selector, false);
  };
  el.addEventListener = function (type, fn) {
    this._listeners[type] = this._listeners[type] || [];
    this._listeners[type].push(fn);
  };
  return el;
}

function walk(node, visit) {
  for (const child of node.children || []) {
    visit(child);
    walk(child, visit);
  }
}

function parseCompound(selector) {
  const parsed = { tag: "*", id: "", classes: [], attrs: [], notAttrs: [] };
  let rest = selector.trim();
  const tagMatch = rest.match(/^[A-Za-z][A-Za-z0-9-]*/);
  if (tagMatch) {
    parsed.tag = tagMatch[0].toLowerCase();
    rest = rest.slice(tagMatch[0].length);
  }
  const tokenRe = /#([A-Za-z][\w-]*)|\.([A-Za-z][\w-]*)|\[([A-Za-z_:][\w:.-]*)(?:="([^"]*)")?\]|:not\(\[([A-Za-z_:][\w:.-]*)\]\)/g;
  let match;
  while ((match = tokenRe.exec(rest))) {
    if (match[1]) parsed.id = match[1];
    else if (match[2]) parsed.classes.push(match[2]);
    else if (match[3]) parsed.attrs.push({ name: match[3], value: match[4], hasValue: match[4] != null });
    else if (match[5]) parsed.notAttrs.push(match[5]);
  }
  return parsed;
}

function matchesCompound(el, parsed) {
  if (parsed.tag !== "*" && el.tagName.toLowerCase() !== parsed.tag) return false;
  if (parsed.id && el.id !== parsed.id) return false;
  const classes = el.className.split(/\s+/).filter(Boolean);
  for (const cls of parsed.classes) {
    if (!classes.includes(cls)) return false;
  }
  for (const attr of parsed.attrs) {
    if (!Object.prototype.hasOwnProperty.call(el.attrs, attr.name)) return false;
    if (attr.hasValue && String(el.attrs[attr.name]) !== attr.value) return false;
  }
  for (const name of parsed.notAttrs) {
    if (Object.prototype.hasOwnProperty.call(el.attrs, name)) return false;
  }
  return true;
}

function matchesSelector(el, selector) {
  const parts = selector.split(",").map((part) => part.trim()).filter(Boolean);
  return parts.some((part) => matchesCompound(el, parseCompound(part)));
}

function queryAll(root, selector, includeRoot) {
  const groups = selector.split(",").map((part) => part.trim()).filter(Boolean);
  const out = [];
  const seen = new Set();
  function consider(el) {
    if (seen.has(el)) return;
    if (groups.some((part) => matchesCompound(el, parseCompound(part)))) {
      seen.add(el);
      out.push(el);
    }
  }
  if (includeRoot && root.nodeType === 1) consider(root);
  walk(root, consider);
  out.forEach = Array.prototype.forEach;
  return out;
}

function parseFragment(html) {
  const root = createEl("fragment", {});
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<\/([A-Za-z][\w:-]*)\s*>|<([A-Za-z][\w:-]*)([^>]*)>|([^<]+)/g;
  let match;
  while ((match = re.exec(html))) {
    if (match[0].startsWith("<!--")) continue;
    if (match[1]) {
      const tag = match[1].toLowerCase();
      if (VOID_TAGS.has(tag)) continue;
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].tagName.toLowerCase() === tag) {
          stack.length = i;
          break;
        }
      }
      continue;
    }
    if (match[2]) {
      const tag = match[2].toLowerCase();
      let raw = match[3] || "";
      const selfClosing = /\/\s*$/.test(raw) || VOID_TAGS.has(tag);
      raw = raw.replace(/\/\s*$/, "");
      const el = createEl(tag, parseAttrs(raw));
      el.parent = stack[stack.length - 1];
      stack[stack.length - 1].children.push(el);
      if (!selfClosing) stack.push(el);
      continue;
    }
    const text = match[4];
    if (text && stack.length) {
      const parent = stack[stack.length - 1];
      parent._text = (parent._text || "") + text;
    }
  }
  return root;
}

function createDocument(html) {
  const fragment = parseFragment(html);
  const listeners = {};
  const document = {
    nodeType: 9,
    children: fragment.children,
    addEventListener(type, fn) {
      listeners[type] = listeners[type] || [];
      listeners[type].push(fn);
    },
    dispatchEvent(type) {
      (listeners[type] || []).forEach((fn) => fn());
    },
    getElementById(id) {
      let found = null;
      walk(fragment, (el) => {
        if (!found && el.id === id) found = el;
      });
      return found;
    },
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] || null;
    },
    querySelectorAll(selector) {
      return queryAll(fragment, selector, false);
    },
  };
  fragment.children.forEach((child) => { child.parent = null; });
  return document;
}
"""


def _empty_date_form_data(**overrides):
    data = archive_date_form_data(
        date_start=None,
        date_end=None,
        date_precision=ArchiveItem.DatePrecision.UNKNOWN,
    )
    data.update(overrides)
    return data


def render_archive_date_widget(*, prefix: str | None = "", form_data=None) -> str:
    context = {
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "form_data": form_data or _empty_date_form_data(),
    }
    if prefix is not None:
        context["date_widget_prefix"] = prefix
    return render_to_string(
        "documents/archive/archive_date_form_fields.html",
        context,
    )


class _IdLabelCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.label_fors: list[tuple[str, str]] = []
        self.labelledby: list[str] = []
        self.names: list[str] = []
        self._label_for: str | None = None
        self._label_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if "id" in attr_map:
            self.ids.append(attr_map["id"])
        if "name" in attr_map:
            self.names.append(attr_map["name"])
        if "aria-labelledby" in attr_map:
            self.labelledby.append(attr_map["aria-labelledby"])
        if tag == "label":
            self._label_for = attr_map.get("for")
            self._label_text = []

    def handle_data(self, data):
        if self._label_for is not None:
            self._label_text.append(data)

    def handle_endtag(self, tag):
        if tag == "label" and self._label_for is not None:
            self.label_fors.append((self._label_for, "".join(self._label_text).strip()))
            self._label_for = None
            self._label_text = []


def _collect(html: str) -> _IdLabelCollector:
    collector = _IdLabelCollector()
    collector.feed(html)
    collector.close()
    return collector


def _archive_date_js_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "public"
        / "static"
        / "public"
        / "archive_date_entry.js"
    )


class ArchiveDateIdPrefixFilterTests(SimpleTestCase):
    def test_empty_prefix_is_backward_compatible(self):
        self.assertEqual(archive_date_id_prefix(""), "")
        self.assertEqual(archive_date_id_prefix(None), "")

    def test_valid_prefix_adds_underscore(self):
        self.assertEqual(archive_date_id_prefix("item"), "item_")
        self.assertEqual(archive_date_id_prefix("photo-meta"), "photo-meta_")

    def test_invalid_prefix_raises(self):
        with self.assertRaises(TemplateSyntaxError):
            archive_date_id_prefix("1bad")
        with self.assertRaises(TemplateSyntaxError):
            archive_date_id_prefix("has space")


class ArchiveDatePrefixedWidgetRenderTests(SimpleTestCase):
    def test_unprefixed_widget_keeps_stable_ids_and_field_names(self):
        html = render_archive_date_widget(prefix="")
        collector = _collect(html)
        self.assertIn("date_precision", collector.ids)
        self.assertIn("archiveDateEntry", collector.ids)
        self.assertIn("archiveDateWidget", collector.ids)
        self.assertNotIn("item_date_precision", collector.ids)
        for name in _DATE_POST_NAMES:
            self.assertIn(name, collector.names)
            self.assertNotIn(f"item_{name}", collector.names)
        self.assertIn(("date_precision", "דיוק תאריך"), collector.label_fors)

    def test_omitted_prefix_matches_empty_prefix(self):
        html = render_archive_date_widget(prefix=None)
        self.assertIn('id="date_precision"', html)
        self.assertIn('id="archiveDateEntry"', html)
        self.assertIn('name="date_start_year"', html)

    def test_two_prefixed_widgets_render_unique_ids(self):
        item_html = render_archive_date_widget(prefix="item")
        photo_html = render_archive_date_widget(prefix="photo")
        item_ids = _collect(item_html).ids
        photo_ids = _collect(photo_html).ids
        self.assertTrue(item_ids)
        self.assertTrue(photo_ids)
        self.assertEqual(
            item_ids,
            [
                f"item_{value}"
                for value in _collect(render_archive_date_widget(prefix="")).ids
            ],
        )
        overlap = set(item_ids) & set(photo_ids)
        self.assertEqual(overlap, set())
        combined = _collect(item_html + photo_html)
        duplicates = sorted(
            {value for value in combined.ids if combined.ids.count(value) > 1}
        )
        self.assertEqual(duplicates, [])

    def test_prefixed_labels_target_matching_inputs(self):
        html = render_archive_date_widget(prefix="item")
        collector = _collect(html)
        id_set = set(collector.ids)
        self.assertTrue(collector.label_fors)
        for target, _text in collector.label_fors:
            self.assertTrue(target.startswith("item_"), msg=target)
            self.assertIn(target, id_set)
        for labelledby in collector.labelledby:
            self.assertTrue(labelledby.startswith("item_"), msg=labelledby)
            self.assertIn(labelledby, id_set)

    def test_prefixed_widgets_keep_unprefixed_post_field_names(self):
        html = render_archive_date_widget(prefix="photo")
        collector = _collect(html)
        for name in _DATE_POST_NAMES:
            self.assertIn(name, collector.names)
            self.assertNotIn(f"photo_{name}", collector.names)
        self.assertNotIn('name="photo_date_precision"', html)
        self.assertIn('name="date_precision"', html)

    def test_invalid_prefix_fails_at_render(self):
        with self.assertRaises(TemplateSyntaxError):
            render_archive_date_widget(prefix="bad prefix")


class ArchiveDatePrefixedWidgetPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_prefix_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _assert_unprefixed_single_widget(self, resp):
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertEqual(content.count('id="date_precision"'), 1)
        self.assertEqual(content.count('id="archiveDateEntry"'), 1)
        self.assertEqual(content.count('id="archiveDateWidget"'), 1)
        self.assertNotIn("item_date_precision", content)
        self.assertNotIn("photo_date_precision", content)
        self.assertIn('name="date_precision"', content)
        self.assertIn('name="date_start_year"', content)
        self.assertIn("archive_date_entry.js", content)
        self.assertIn("data-archive-date-widget", content)

    def test_upload_page_stays_unprefixed(self):
        self._assert_unprefixed_single_widget(self.client.get("/api/ui/upload/"))

    def test_manual_text_create_stays_unprefixed(self):
        self._assert_unprefixed_single_widget(
            self.client.get("/archive/manage/new/?item_type=manual_text")
        )

    def test_photo_create_stays_unprefixed(self):
        self._assert_unprefixed_single_widget(
            self.client.get("/archive/manage/new/?item_type=photo")
        )

    def test_manual_text_edit_stays_unprefixed(self):
        item = create_manual_text_archive_item(title="Unprefixed edit", body="x")
        self._assert_unprefixed_single_widget(
            self.client.get(f"/archive/manage/{item.id}/edit/")
        )

    def test_photo_item_edit_stays_unprefixed(self):
        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Unprefixed photo edit",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        PhotoContent.objects.create(
            archive_item=photo_item,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=100,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self._assert_unprefixed_single_widget(
            self.client.get(f"/archive/manage/{photo_item.id}/edit/")
        )

    def test_photo_add_stays_unprefixed(self):
        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Unprefixed photo add",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        PhotoContent.objects.create(
            archive_item=photo_item,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=100,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self._assert_unprefixed_single_widget(
            self.client.get(f"/archive/manage/{photo_item.id}/photos/add/")
        )


class ArchiveDateScopedCollectMetaTests(SimpleTestCase):
    def test_script_scopes_lookups_to_widget_roots(self):
        source = _archive_date_js_path().read_text(encoding="utf-8")
        self.assertIn("[data-archive-date-widget]", source)
        self.assertIn("function resolveWidget", source)
        self.assertIn("function initOneWidget", source)
        self.assertIn("getWidgetRoots().forEach(initOneWidget)", source)
        self.assertNotIn('document.getElementById("date_precision")', source)
        self.assertIn("collectMeta: collectArchiveDateMeta", source)

    def test_collect_meta_is_independent_for_two_prefixed_widgets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available for JavaScript execution checks")

        item_html = render_archive_date_widget(
            prefix="item",
            form_data=archive_date_form_data(
                date_start=date(1948, 1, 1),
                date_end=date(1948, 12, 31),
                date_precision=ArchiveItem.DatePrecision.YEAR,
            ),
        )
        photo_html = render_archive_date_widget(
            prefix="photo",
            form_data=archive_date_form_data(
                date_start=date(1952, 1, 1),
                date_end=date(1952, 12, 31),
                date_precision=ArchiveItem.DatePrecision.YEAR,
            ),
        )
        page_html = (
            f'<form id="item-form">{item_html}</form>'
            f'<form id="photo-form">{photo_html}</form>'
        )
        runner = (
            _MINIDOM_JS
            + r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const widgetJs = fs.readFileSync(process.argv[3], "utf8");
const document = createDocument(html);
const window = {
  matchMedia(query) {
    return {
      matches: false,
      addEventListener() {},
      addListener() {},
    };
  },
  vsArchiveDateEntry: null,
};
global.document = document;
global.window = window;
eval(widgetJs);
document.dispatchEvent("DOMContentLoaded");
const widgets = document.querySelectorAll("[data-archive-date-widget]");
if (widgets.length !== 2) {
  throw new Error("expected 2 widgets, got " + widgets.length);
}
const metaItem = {};
const metaPhoto = {};
window.vsArchiveDateEntry.collectMeta(metaItem, widgets[0]);
window.vsArchiveDateEntry.collectMeta(metaPhoto, widgets[1]);
const bare = {};
window.vsArchiveDateEntry.collectMeta(bare);
fs.writeFileSync(process.argv[4], JSON.stringify({
  metaItem,
  metaPhoto,
  bare,
  widgetCount: widgets.length,
}));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            html_path = tmp_path / "page.html"
            runner_path = tmp_path / "runner.js"
            out_path = tmp_path / "out.json"
            html_path.write_text(page_html, encoding="utf-8")
            runner_path.write_text(runner, encoding="utf-8")
            completed = subprocess.run(
                [
                    node,
                    str(runner_path),
                    str(html_path),
                    str(_archive_date_js_path()),
                    str(out_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.fail(
                    "node collectMeta runner failed:\n"
                    f"{completed.stderr}\n{completed.stdout}"
                )
            payload = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["widgetCount"], 2)
        self.assertEqual(payload["metaItem"]["date_precision"], "YEAR")
        self.assertEqual(payload["metaItem"]["date_start_year"], "1948")
        self.assertEqual(payload["metaPhoto"]["date_precision"], "YEAR")
        self.assertEqual(payload["metaPhoto"]["date_start_year"], "1952")
        self.assertNotIn("date_start_year", payload["bare"])
        self.assertNotEqual(
            payload["metaItem"]["date_start_year"],
            payload["metaPhoto"]["date_start_year"],
        )

    def test_collect_meta_without_root_still_works_for_single_unprefixed_widget(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available for JavaScript execution checks")

        html = (
            '<form id="upload-form">'
            + render_archive_date_widget(
                prefix="",
                form_data=archive_date_form_data(
                    date_start=date(1948, 1, 1),
                    date_end=date(1948, 12, 31),
                    date_precision=ArchiveItem.DatePrecision.YEAR,
                ),
            )
            + "</form>"
        )
        runner = (
            _MINIDOM_JS
            + r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const widgetJs = fs.readFileSync(process.argv[3], "utf8");
const document = createDocument(html);
const window = {
  matchMedia() {
    return { matches: false, addEventListener() {}, addListener() {} };
  },
  vsArchiveDateEntry: null,
};
global.document = document;
global.window = window;
eval(widgetJs);
document.dispatchEvent("DOMContentLoaded");
const meta = {};
window.vsArchiveDateEntry.collectMeta(meta);
fs.writeFileSync(process.argv[4], JSON.stringify(meta));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            html_path = tmp_path / "page.html"
            runner_path = tmp_path / "runner.js"
            out_path = tmp_path / "out.json"
            html_path.write_text(html, encoding="utf-8")
            runner_path.write_text(runner, encoding="utf-8")
            completed = subprocess.run(
                [
                    node,
                    str(runner_path),
                    str(html_path),
                    str(_archive_date_js_path()),
                    str(out_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.fail(
                    "node single-widget collectMeta runner failed:\n"
                    f"{completed.stderr}\n{completed.stdout}"
                )
            meta = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("date_precision"), "YEAR")
        self.assertEqual(meta.get("date_start_year"), "1948")
