#!/usr/bin/env bash
set -Eeuo pipefail

# VS-Archive production inventory report
#
# Runs from your local machine, but executes the actual counting code inside
# the currently running ECS web container. Therefore the data comes from the
# production Django database and production S3 configuration, not localhost.
#
# Required environment variables:
#   AWS_REGION
#   CLUSTER_NAME
#   WEB_SERVICE
#
# Optional:
#   AWS_PROFILE
#
# Usage:
#   bash scripts/prod_archive_inventory.sh
#
# Save JSON too:
#   OUTPUT_JSON=tmp/prod_archive_inventory.json \
#     bash scripts/prod_archive_inventory.sh
#
# Override container name only if automatic detection selects the wrong one:
#   WEB_CONTAINER_NAME=web bash scripts/prod_archive_inventory.sh

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: required environment variable '$name' is not set." >&2
    exit 1
  fi
}

require_env AWS_REGION
require_env CLUSTER_NAME
require_env WEB_SERVICE

AWS_ARGS=(--region "$AWS_REGION")
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

echo "Locating running production web task..."

WEB_TASK_ARN="$(
  aws ecs list-tasks \
    --cluster "$CLUSTER_NAME" \
    --service-name "$WEB_SERVICE" \
    --desired-status RUNNING \
    "${AWS_ARGS[@]}" \
    --query 'taskArns[0]' \
    --output text
)"

if [[ -z "$WEB_TASK_ARN" || "$WEB_TASK_ARN" == "None" ]]; then
  echo "ERROR: no RUNNING ECS task found for service '$WEB_SERVICE'." >&2
  exit 1
fi

if [[ -z "${WEB_CONTAINER_NAME:-}" ]]; then
  TASK_DEFINITION_ARN="${WEB_TD:-}"

  if [[ -z "$TASK_DEFINITION_ARN" ]]; then
    TASK_DEFINITION_ARN="$(
      aws ecs describe-services \
        --cluster "$CLUSTER_NAME" \
        --services "$WEB_SERVICE" \
        "${AWS_ARGS[@]}" \
        --query 'services[0].taskDefinition' \
        --output text
    )"
  fi

  if [[ -z "$TASK_DEFINITION_ARN" || "$TASK_DEFINITION_ARN" == "None" ]]; then
    echo "ERROR: could not determine the web task definition." >&2
    exit 1
  fi

  WEB_CONTAINER_NAME="$(
    aws ecs describe-task-definition \
      --task-definition "$TASK_DEFINITION_ARN" \
      "${AWS_ARGS[@]}" \
      --query 'taskDefinition.containerDefinitions[?essential==`true`][0].name' \
      --output text
  )"

  if [[ -z "$WEB_CONTAINER_NAME" || "$WEB_CONTAINER_NAME" == "None" ]]; then
    WEB_CONTAINER_NAME="$(
      aws ecs describe-task-definition \
        --task-definition "$TASK_DEFINITION_ARN" \
        "${AWS_ARGS[@]}" \
        --query 'taskDefinition.containerDefinitions[0].name' \
        --output text
    )"
  fi
fi

if [[ -z "$WEB_CONTAINER_NAME" || "$WEB_CONTAINER_NAME" == "None" ]]; then
  echo "ERROR: could not determine the web container name." >&2
  echo "Container definitions:" >&2
  aws ecs describe-task-definition \
    --task-definition "${TASK_DEFINITION_ARN:-${WEB_TD:-}}" \
    "${AWS_ARGS[@]}" \
    --query 'taskDefinition.containerDefinitions[].{name:name,essential:essential}' \
    --output table >&2 || true
  exit 1
fi

echo "Task:      $WEB_TASK_ARN"
echo "Container: $WEB_CONTAINER_NAME"
echo

PYTHON_CODE="$(cat <<'PY'
from __future__ import annotations

import io
import json
from collections import Counter, defaultdict

import boto3
from django.conf import settings

from documents.models import (
    ArchiveItem,
    Document,
    DocumentSourceFile,
    DocumentTextResult,
    PhotoContent,
)
from documents.services import text_presentation


def concrete_relation_name(model, related_model):
    matches = []
    for field in model._meta.get_fields():
        remote = getattr(field, "remote_field", None)
        if (
            remote is not None
            and remote.model is related_model
            and getattr(field, "concrete", False)
        ):
            matches.append(field.name)

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one concrete relation from {model.__name__} "
            f"to {related_model.__name__}; found {matches!r}"
        )
    return matches[0]


def get_bucket_name():
    import os

    setting_names = (
        "AWS_STORAGE_BUCKET_NAME",
        "AWS_S3_BUCKET_NAME",
        "AWS_S3_BUCKET",
        "S3_BUCKET_NAME",
        "S3_BUCKET",
        "DOCUMENTS_S3_BUCKET_NAME",
        "DOCUMENTS_S3_BUCKET",
        "DOCUMENTS_BUCKET_NAME",
        "DOCUMENTS_BUCKET",
        "UPLOAD_BUCKET_NAME",
        "UPLOAD_BUCKET",
    )

    for name in setting_names:
        value = getattr(settings, name, None) or os.environ.get(name)
        if value:
            return str(value).strip()

    storages = getattr(settings, "STORAGES", {}) or {}
    for _storage_name, storage_config in storages.items():
        options = (storage_config or {}).get("OPTIONS", {}) or {}
        for option_name in ("bucket_name", "bucket", "Bucket"):
            value = options.get(option_name)
            if value:
                return str(value).strip()

    for name in dir(settings):
        upper_name = name.upper()
        if "BUCKET" not in upper_name:
            continue
        value = getattr(settings, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def is_pdf(key):
    return key.lower().split("?", 1)[0].endswith(".pdf")


def count_pdf_pages_from_bytes(pdf_bytes):
    """
    Count pages without requiring an installed Python PDF package.

    Order:
    1. pypdf
    2. PyPDF2
    3. pdfinfo CLI, if available in the container
    4. conservative PDF object scan fallback
    """
    import os
    import re
    import shutil
    import subprocess
    import tempfile

    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(pdf_bytes)).pages), "pypdf"
    except ImportError:
        pass

    try:
        from PyPDF2 import PdfReader

        return len(PdfReader(io.BytesIO(pdf_bytes)).pages), "PyPDF2"
    except ImportError:
        pass

    if shutil.which("pdfinfo"):
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_name = tmp.name

            result = subprocess.run(
                ["pdfinfo", tmp_name],
                check=True,
                capture_output=True,
                text=True,
            )
            match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
            if not match:
                raise RuntimeError("pdfinfo output did not contain a Pages field")
            return int(match.group(1)), "pdfinfo"
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    page_objects = re.findall(rb"/Type\s*/Page(?!s)\b", pdf_bytes)
    if page_objects:
        return len(page_objects), "pdf-object-scan"

    raise RuntimeError(
        "Could not count PDF pages: no pypdf/PyPDF2, no pdfinfo, "
        "and the PDF object scan found no page objects."
    )


def raw_value(value, fallback="UNKNOWN"):
    value = str(value or "").strip()
    return value or fallback


def model_choice_label(obj, field_name):
    getter = getattr(obj, f"get_{field_name}_display", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            pass
    return raw_value(getattr(obj, field_name, None))


def sorted_counter(counter):
    return {str(k): int(v) for k, v in sorted(counter.items(), key=lambda kv: str(kv[0]))}


def nested_language_report(language_stats, language_labels):
    result = {}
    for language in sorted(language_stats):
        row = language_stats[language]
        result[language] = {
            "label": language_labels.get(language, language),
            "documents": row["documents"],
            "document_pages": row["document_pages"],
            "processing_states": sorted_counter(row["processing_states"]),
            "text_input_types": sorted_counter(row["text_input_types"]),
            "transcription_review": sorted_counter(row["transcription_review"]),
            "transcription_quality": sorted_counter(row["transcription_quality"]),
            "displayed_transcription_engines": sorted_counter(
                row["displayed_transcription_engines"]
            ),
            "translation_review": sorted_counter(row["translation_review"]),
        }
    return result


document_item_field = concrete_relation_name(Document, ArchiveItem)
source_document_field = concrete_relation_name(DocumentSourceFile, Document)

items = list(ArchiveItem.objects.all().order_by("pk"))
items_by_id = {item.pk: item for item in items}
documents = list(Document.objects.all().order_by("pk"))
source_files = list(
    DocumentSourceFile.objects.all().order_by(source_document_field, "order_index", "pk")
)
photo_contents = list(PhotoContent.objects.all().order_by("archive_item_id", "position", "pk"))
text_results_total = DocumentTextResult.objects.count()

documents_by_item_id = {}
for document in documents:
    archive_item = getattr(document, document_item_field)
    documents_by_item_id[archive_item.pk] = document

source_files_by_document_id = defaultdict(list)
for source_file in source_files:
    document = getattr(source_file, source_document_field)
    source_files_by_document_id[document.pk].append(source_file)

photo_contents_by_item_id = defaultdict(list)
for photo in photo_contents:
    photo_contents_by_item_id[photo.archive_item_id].append(photo)

type_counts = Counter()
visibility_counts = Counter()
matrix = defaultdict(Counter)

document_pages_total = 0
document_pages_by_visibility = Counter()
document_pages_by_language = Counter()
image_pages = 0
pdf_pages = 0
pdf_files = 0

ocr_language_counts = Counter()
ocr_text_input_type_counts = Counter()
ocr_processing_state_counts = Counter()
ocr_upload_status_counts = Counter()
ocr_transcription_review_counts = Counter()
ocr_transcription_quality_counts = Counter()
ocr_transcription_engine_counts = Counter()
ocr_translation_review_counts = Counter()
ocr_language_labels = {}
ocr_language_stats = defaultdict(
    lambda: {
        "documents": 0,
        "document_pages": 0,
        "processing_states": Counter(),
        "text_input_types": Counter(),
        "transcription_review": Counter(),
        "transcription_quality": Counter(),
        "displayed_transcription_engines": Counter(),
        "translation_review": Counter(),
    }
)

photo_item_ids = {
    item.pk for item in items if raw_value(item.item_type) == "PHOTO"
}
photo_item_count = len(photo_item_ids)
photo_images_total = len(photo_contents)
photo_images_by_visibility = Counter()
photo_upload_status_counts = Counter()
photo_cluster_sizes = Counter()
photo_items_without_images = []
photo_contents_on_non_photo_items = []

for item_id in photo_item_ids:
    count = len(photo_contents_by_item_id.get(item_id, []))
    photo_cluster_sizes[count] += 1
    if count == 0:
        photo_items_without_images.append(item_id)

for photo in photo_contents:
    item = items_by_id.get(photo.archive_item_id)
    if item is None or raw_value(item.item_type) != "PHOTO":
        photo_contents_on_non_photo_items.append(photo.pk)
        visibility = "UNKNOWN"
    else:
        visibility = raw_value(item.visibility)
    photo_images_by_visibility[visibility] += 1
    photo_upload_status_counts[raw_value(getattr(photo, "upload_status", None))] += 1

single_photo_items = sum(count for size, count in photo_cluster_sizes.items() if size == 1)
multi_photo_items = sum(count for size, count in photo_cluster_sizes.items() if size > 1)
max_photos_in_item = max(
    (len(photo_contents_by_item_id.get(item_id, [])) for item_id in photo_item_ids),
    default=0,
)

ocr_items_without_document = 0
documents_without_source = 0
pdf_files_not_counted = 0
errors = []

bucket_name = get_bucket_name()
s3 = None
pdf_reader_name = None
pdf_reader_methods = Counter()

resolve_displayed_transcription_result = getattr(
    text_presentation, "resolve_displayed_transcription_result", None
)
resolve_displayed_hebrew_translation_result = getattr(
    text_presentation, "resolve_displayed_hebrew_translation_result", None
)

for item in items:
    item_type = raw_value(item.item_type)
    visibility = raw_value(item.visibility)

    type_counts[item_type] += 1
    visibility_counts[visibility] += 1
    matrix[visibility][item_type] += 1

    if item_type != "OCR_DOCUMENT":
        continue

    document = documents_by_item_id.get(item.pk)
    if document is None:
        ocr_items_without_document += 1
        errors.append(
            {
                "archive_item_id": item.pk,
                "title": item.title,
                "error": "OCR_DOCUMENT has no related Document row",
            }
        )
        continue

    language = raw_value(getattr(document, "language", None))
    text_input_type = raw_value(getattr(document, "text_input_type", None))
    processing_state = raw_value(getattr(document, "processing_state_user", None))
    upload_status = raw_value(getattr(document, "upload_status", None))

    ocr_language_counts[language] += 1
    ocr_text_input_type_counts[text_input_type] += 1
    ocr_processing_state_counts[processing_state] += 1
    ocr_upload_status_counts[upload_status] += 1
    ocr_language_labels.setdefault(language, model_choice_label(document, "language"))

    lang_stats = ocr_language_stats[language]
    lang_stats["documents"] += 1
    lang_stats["processing_states"][processing_state] += 1
    lang_stats["text_input_types"][text_input_type] += 1

    displayed_transcription = None
    if callable(resolve_displayed_transcription_result):
        displayed_transcription = resolve_displayed_transcription_result(document)

    if displayed_transcription is None:
        transcription_review = "NO_DISPLAYED_TRANSCRIPTION"
        transcription_quality = "NO_DISPLAYED_TRANSCRIPTION"
        transcription_engine = "NO_DISPLAYED_TRANSCRIPTION"
    else:
        transcription_review = raw_value(
            getattr(displayed_transcription, "verification_status", None)
        )
        transcription_quality = raw_value(
            getattr(displayed_transcription, "quality", None)
        )
        transcription_engine = raw_value(
            getattr(displayed_transcription, "engine_key", None)
            or getattr(displayed_transcription, "engine", None)
        )

    ocr_transcription_review_counts[transcription_review] += 1
    ocr_transcription_quality_counts[transcription_quality] += 1
    ocr_transcription_engine_counts[transcription_engine] += 1
    lang_stats["transcription_review"][transcription_review] += 1
    lang_stats["transcription_quality"][transcription_quality] += 1
    lang_stats["displayed_transcription_engines"][transcription_engine] += 1

    translation_review = "NOT_APPLICABLE"
    if language != "he":
        if callable(resolve_displayed_hebrew_translation_result):
            translation = resolve_displayed_hebrew_translation_result(document)
            translation_review = (
                raw_value(getattr(translation, "verification_status", None))
                if translation is not None
                else "NO_DISPLAYED_TRANSLATION"
            )
        else:
            translation_review = "HELPER_UNAVAILABLE"
    ocr_translation_review_counts[translation_review] += 1
    lang_stats["translation_review"][translation_review] += 1

    keys = []
    for source_file in source_files_by_document_id.get(document.pk, []):
        key = str(getattr(source_file, "file_s3_key", "") or "").strip()
        if key:
            keys.append(key)

    legacy_key = str(getattr(document, "file_s3_key", "") or "").strip()
    if not keys and legacy_key:
        keys.append(legacy_key)

    if not keys:
        documents_without_source += 1
        errors.append(
            {
                "archive_item_id": item.pk,
                "document_id": document.pk,
                "title": item.title,
                "error": "Document has no source-file S3 key",
            }
        )
        continue

    item_pages = 0

    for key in keys:
        if not is_pdf(key):
            item_pages += 1
            image_pages += 1
            continue

        pdf_files += 1

        try:
            if not bucket_name:
                import os

                bucket_env_names = sorted(
                    name
                    for name in os.environ
                    if "BUCKET" in name.upper() or "S3" in name.upper()
                )
                bucket_setting_names = sorted(
                    name
                    for name in dir(settings)
                    if name.isupper() and ("BUCKET" in name or "S3" in name)
                )
                raise RuntimeError(
                    "Could not determine the production S3 bucket name. "
                    f"Candidate environment names: {bucket_env_names}; "
                    f"candidate Django setting names: {bucket_setting_names}"
                )

            if s3 is None:
                s3 = boto3.client("s3")

            response = s3.get_object(Bucket=bucket_name, Key=key)
            pdf_bytes = response["Body"].read()
            count, method = count_pdf_pages_from_bytes(pdf_bytes)
            pdf_reader_methods[method] += 1
            pdf_reader_name = ", ".join(
                f"{name}:{count}"
                for name, count in sorted(pdf_reader_methods.items())
            )

            item_pages += count
            pdf_pages += count
        except Exception as exc:
            pdf_files_not_counted += 1
            errors.append(
                {
                    "archive_item_id": item.pk,
                    "document_id": document.pk,
                    "title": item.title,
                    "s3_key": key,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    document_pages_total += item_pages
    document_pages_by_visibility[visibility] += item_pages
    document_pages_by_language[language] += item_pages
    lang_stats["document_pages"] += item_pages

known_types = ["OCR_DOCUMENT", "MANUAL_TEXT", "PHOTO", "VIDEO"]
all_types = sorted(set(type_counts) | set(known_types))
all_visibilities = sorted(visibility_counts)

page_total_complete = (
    ocr_items_without_document == 0
    and documents_without_source == 0
    and pdf_files_not_counted == 0
)
inventory_consistent = (
    page_total_complete
    and not photo_items_without_images
    and not photo_contents_on_non_photo_items
)

verified_transcriptions = ocr_transcription_review_counts["VERIFIED"]
unverified_transcriptions = ocr_transcription_review_counts["UNVERIFIED"]
rejected_transcriptions = ocr_transcription_review_counts["REJECTED"]
missing_transcriptions = ocr_transcription_review_counts["NO_DISPLAYED_TRANSCRIPTION"]

report = {
    "environment": {
        "database_engine": settings.DATABASES["default"]["ENGINE"],
        "database_host": settings.DATABASES["default"].get("HOST"),
        "s3_bucket": bucket_name,
        "pdf_reader": pdf_reader_name,
    },
    "summary": {
        "archive_items_total": len(items),
        "ocr_documents": type_counts["OCR_DOCUMENT"],
        "manual_texts": type_counts["MANUAL_TEXT"],
        "photo_items_clusters": photo_item_count,
        "photo_images": photo_images_total,
        "videos": type_counts["VIDEO"],
        "private_items": visibility_counts["private"],
        "document_pages_total": document_pages_total,
        "image_document_pages": image_pages,
        "pdf_document_pages": pdf_pages,
        "pdf_files": pdf_files,
        "physical_images_and_document_pages_total": (
            photo_images_total + document_pages_total
        ),
        "verified_displayed_transcriptions": verified_transcriptions,
        "unverified_displayed_transcriptions": unverified_transcriptions,
        "rejected_displayed_transcriptions": rejected_transcriptions,
        "ocr_documents_without_displayed_transcription": missing_transcriptions,
    },
    "photos": {
        "archive_item_clusters": photo_item_count,
        "individual_images": photo_images_total,
        "single_photo_clusters": single_photo_items,
        "multi_photo_clusters": multi_photo_items,
        "max_images_in_one_cluster": max_photos_in_item,
        "cluster_size_distribution": sorted_counter(photo_cluster_sizes),
        "upload_status": sorted_counter(photo_upload_status_counts),
        "images_by_visibility": sorted_counter(photo_images_by_visibility),
    },
    "ocr": {
        "documents_by_language": sorted_counter(ocr_language_counts),
        "document_pages_by_language": sorted_counter(document_pages_by_language),
        "documents_by_text_input_type": sorted_counter(ocr_text_input_type_counts),
        "processing_states": sorted_counter(ocr_processing_state_counts),
        "upload_statuses": sorted_counter(ocr_upload_status_counts),
        "displayed_transcription_review": sorted_counter(
            ocr_transcription_review_counts
        ),
        "displayed_transcription_quality": sorted_counter(
            ocr_transcription_quality_counts
        ),
        "displayed_transcription_engines": sorted_counter(
            ocr_transcription_engine_counts
        ),
        "displayed_hebrew_translation_review": sorted_counter(
            ocr_translation_review_counts
        ),
        "text_result_rows_total": text_results_total,
        "by_language": nested_language_report(
            ocr_language_stats,
            ocr_language_labels,
        ),
    },
    "by_visibility": {
        visibility: {
            "archive_items_total": visibility_counts[visibility],
            "ocr_documents": matrix[visibility]["OCR_DOCUMENT"],
            "manual_texts": matrix[visibility]["MANUAL_TEXT"],
            "photo_item_clusters": matrix[visibility]["PHOTO"],
            "photo_images": photo_images_by_visibility[visibility],
            "videos": matrix[visibility]["VIDEO"],
            "document_pages_total": document_pages_by_visibility[visibility],
            "physical_images_and_document_pages_total": (
                photo_images_by_visibility[visibility]
                + document_pages_by_visibility[visibility]
            ),
        }
        for visibility in all_visibilities
    },
    "visibility_by_type_matrix": {
        visibility: {
            item_type: matrix[visibility][item_type]
            for item_type in all_types
        }
        for visibility in all_visibilities
    },
    "data_quality": {
        "inventory_consistent": inventory_consistent,
        "page_total_is_complete": page_total_complete,
        "ocr_items_without_document": ocr_items_without_document,
        "documents_without_source": documents_without_source,
        "pdf_files_not_counted": pdf_files_not_counted,
        "photo_items_without_images": photo_items_without_images,
        "photo_contents_on_non_photo_items": photo_contents_on_non_photo_items,
        "errors": errors,
    },
}

print("===VS_ARCHIVE_REPORT_JSON_START===")
print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
print("===VS_ARCHIVE_REPORT_JSON_END===")

print()
print("VS-Archive production inventory")
print("=" * 78)
print(f"All archival items:       {report['summary']['archive_items_total']}")
print(f"OCR documents:            {report['summary']['ocr_documents']}")
print(f"Manual texts:             {report['summary']['manual_texts']}")
print(f"Photo items / clusters:   {report['summary']['photo_items_clusters']}")
print(f"Individual photo images:  {report['summary']['photo_images']}")
print(f"Videos:                   {report['summary']['videos']}")
print(f"Private items:            {report['summary']['private_items']}")

print()
print("Physical content")
print("-" * 78)
print(f"All document pages:       {report['summary']['document_pages_total']}")
print(f"  Image pages:            {report['summary']['image_document_pages']}")
print(f"  PDF pages:              {report['summary']['pdf_document_pages']}")
print(f"  PDF files:              {report['summary']['pdf_files']}")
print(
    "Photo images + doc pages: "
    f"{report['summary']['physical_images_and_document_pages_total']}"
)

print()
print("Photos")
print("-" * 78)
print(f"Photo clusters:           {photo_item_count}")
print(f"Individual images:        {photo_images_total}")
print(f"Single-photo clusters:    {single_photo_items}")
print(f"Multi-photo clusters:     {multi_photo_items}")
print(f"Max images in one cluster:{max_photos_in_item:>6}")
print(
    "Cluster sizes:           "
    + ", ".join(
        f"{size} image(s): {count}"
        for size, count in sorted(photo_cluster_sizes.items())
    )
)
print(
    "Photo upload status:     "
    + ", ".join(
        f"{status}: {count}"
        for status, count in sorted(photo_upload_status_counts.items())
    )
)

print()
print("OCR documents by language")
print("-" * 78)
for language, row in report["ocr"]["by_language"].items():
    print(
        f"{language} ({row['label']}): "
        f"{row['documents']} docs | "
        f"{row['document_pages']} pages | "
        f"verified transcription {row['transcription_review'].get('VERIFIED', 0)} | "
        f"unverified {row['transcription_review'].get('UNVERIFIED', 0)} | "
        f"rejected {row['transcription_review'].get('REJECTED', 0)} | "
        f"no displayed transcription "
        f"{row['transcription_review'].get('NO_DISPLAYED_TRANSCRIPTION', 0)}"
    )

print()
print("OCR processing / review")
print("-" * 78)
print(
    "Processing states:       "
    + ", ".join(
        f"{state}: {count}"
        for state, count in sorted(ocr_processing_state_counts.items())
    )
)
print(
    "Input types:             "
    + ", ".join(
        f"{input_type}: {count}"
        for input_type, count in sorted(ocr_text_input_type_counts.items())
    )
)
print(f"Verified transcription:   {verified_transcriptions}")
print(f"Unverified transcription: {unverified_transcriptions}")
print(f"Rejected transcription:   {rejected_transcriptions}")
print(f"No displayed transcription:{missing_transcriptions:>5}")
print(
    "Translation review:      "
    + ", ".join(
        f"{status}: {count}"
        for status, count in sorted(ocr_translation_review_counts.items())
    )
)

print()
print("By visibility")
print("-" * 78)
for visibility, row in report["by_visibility"].items():
    print(
        f"{visibility}: "
        f"{row['archive_items_total']} items | "
        f"{row['ocr_documents']} documents | "
        f"{row['manual_texts']} manual texts | "
        f"{row['photo_item_clusters']} photo clusters | "
        f"{row['photo_images']} photo images | "
        f"{row['videos']} videos | "
        f"{row['document_pages_total']} document pages | "
        f"{row['physical_images_and_document_pages_total']} images+pages"
    )

print()
print("Data quality")
print("-" * 78)
print(f"Inventory consistent:     {'YES' if inventory_consistent else 'NO'}")
print(f"Complete page total:      {'YES' if page_total_complete else 'NO'}")
print(f"OCR items without Document: {ocr_items_without_document}")
print(f"Documents without source:   {documents_without_source}")
print(f"PDF files not counted:      {pdf_files_not_counted}")
print(f"Photo items without images: {len(photo_items_without_images)}")
print(
    "Photo rows on non-PHOTO items: "
    f"{len(photo_contents_on_non_photo_items)}"
)

if errors:
    print()
    print("Unresolved records/errors")
    print("-" * 78)
    for error in errors:
        print(json.dumps(error, ensure_ascii=False, sort_keys=True))

PY
)"

PYTHON_B64="$(
  printf '%s' "$PYTHON_CODE" | base64 | tr -d '\n'
)"

REMOTE_COMMAND="python /app/manage.py shell -c \"import base64; exec(base64.b64decode('${PYTHON_B64}').decode('utf-8'))\""

TMP_OUTPUT="$(mktemp)"
trap 'rm -f "$TMP_OUTPUT"' EXIT

echo "Running report inside the production ECS web container..."
echo

set +e
aws ecs execute-command \
  --cluster "$CLUSTER_NAME" \
  --task "$WEB_TASK_ARN" \
  --container "$WEB_CONTAINER_NAME" \
  --interactive \
  "${AWS_ARGS[@]}" \
  --command "$REMOTE_COMMAND" | tee "$TMP_OUTPUT"
AWS_EXIT="${PIPESTATUS[0]}"
set -e

if [[ "$AWS_EXIT" -ne 0 ]]; then
  echo
  echo "ERROR: ECS execute-command failed with exit code $AWS_EXIT." >&2
  exit "$AWS_EXIT"
fi

if [[ -n "${OUTPUT_JSON:-}" ]]; then
  mkdir -p "$(dirname "$OUTPUT_JSON")"

  awk '
    /===VS_ARCHIVE_REPORT_JSON_START===/ {capture=1; next}
    /===VS_ARCHIVE_REPORT_JSON_END===/   {capture=0; exit}
    capture
  ' "$TMP_OUTPUT" > "$OUTPUT_JSON"

  if [[ ! -s "$OUTPUT_JSON" ]]; then
    echo
    echo "ERROR: report ran, but JSON extraction failed." >&2
    exit 1
  fi

  echo
  echo "JSON report saved to: $OUTPUT_JSON"
fi
