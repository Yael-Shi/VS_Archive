from django.urls import path
from . import views

urlpatterns = [
    path("uploads/create/", views.create_upload, name="uploads-create"),
    path(
        "uploads/<int:doc_id>/complete/", views.upload_complete, name="uploads-complete"
    ),
    path(
        "uploads/<int:doc_id>/parts/<int:order_index>/complete/",
        views.upload_part_complete,
        name="uploads-part-complete",
    ),
    path(
        "uploads/<int:doc_id>/finalize/",
        views.upload_finalize,
        name="uploads-finalize",
    ),
    path(
        "photo-uploads/create/",
        views.create_photo_upload,
        name="photo-uploads-create",
    ),
    path(
        "photo-uploads/<int:photo_content_id>/complete/",
        views.photo_upload_complete,
        name="photo-uploads-complete",
    ),
    path("documents/", views.documents_list_api, name="documents-list-api"),
    path("ui/documents/", views.documents_list_page, name="documents-list-page"),
    path(
        "ui/documents/<int:doc_id>/",
        views.document_detail_page,
        name="documents-detail-page",
    ),
    path(
        "ui/documents/<int:doc_id>/ocr-reprocess/",
        views.document_ocr_reprocess,
        name="documents-ocr-reprocess",
    ),
    path(
        "ui/documents/<int:doc_id>/hebrew-translation-retry/",
        views.document_hebrew_translation_retry,
        name="documents-hebrew-translation-retry",
    ),
    path(
        "ui/documents/<int:doc_id>/transcription-suggestions/new/",
        views.transcription_suggestion_form,
        name="transcription-suggestion-new",
    ),
    path(
        "ui/documents/<int:doc_id>/transcription-suggestions/thanks/",
        views.transcription_suggestion_thanks,
        name="transcription-suggestion-thanks",
    ),
    path("ui/upload/", views.upload_page, name="upload-page"),
    path("ui/admin/backlog/", views.admin_backlog_page, name="admin-backlog-page"),
    path("ui/admin/review/", views.review_backlog_page, name="review-backlog-page"),
    path(
        "ui/admin/review/<int:doc_id>/",
        views.review_detail_page,
        name="review-detail-page",
    ),
    path(
        "ui/admin/review/text-results/<int:result_id>/verify/",
        views.review_text_result_verify,
        name="review-text-result-verify",
    ),
    path(
        "ui/admin/review/text-results/<int:result_id>/reject/",
        views.review_text_result_reject,
        name="review-text-result-reject",
    ),
    path(
        "ui/admin/review/text-results/<int:result_id>/text/",
        views.review_text_result_update_text,
        name="review-text-result-update-text",
    ),
    path(
        "ui/admin/review/text-results/<int:result_id>/verified-edit/",
        views.review_text_result_verified_edit,
        name="review-text-result-verified-edit",
    ),
    path(
        "ui/admin/transcription-suggestions/",
        views.transcription_suggestion_backlog_page,
        name="transcription-suggestion-backlog",
    ),
    path(
        "ui/admin/transcription-suggestions/<int:suggestion_id>/",
        views.transcription_suggestion_detail_page,
        name="transcription-suggestion-detail",
    ),
    path(
        "ui/admin/transcription-suggestions/<int:suggestion_id>/approve/",
        views.transcription_suggestion_approve,
        name="transcription-suggestion-approve",
    ),
    path(
        "ui/admin/transcription-suggestions/<int:suggestion_id>/reject/",
        views.transcription_suggestion_reject,
        name="transcription-suggestion-reject",
    ),
    path(
        "ui/admin/archive-metadata-suggestions/",
        views.archive_metadata_suggestion_backlog_page,
        name="archive-metadata-suggestion-backlog",
    ),
]
