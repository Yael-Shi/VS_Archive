from django.contrib import admin
from .models import (
    Document,
    CorrectionRequest,
    DocumentTextResult,
    Tag,
    DocumentMetadata,
    TranskribusRun,
)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created_at", "updated_at")
    search_fields = ("name",)
    ordering = ("name",)


class DocumentMetadataInline(admin.StackedInline):
    model = DocumentMetadata
    extra = 0
    can_delete = False
    fields = (
        "notes",
        "donor",
        "collection",
        "original_location",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")


class TranskribusRunInline(admin.TabularInline):
    model = TranskribusRun
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "id",
        "status",
        "mode",
        "remote_doc_id",
        "model_id",
        "recognition_job_id",
        "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    inlines = (DocumentMetadataInline, TranskribusRunInline)

    list_display = (
        "id",
        "title",
        "doc_type",
        "text_input_type",
        "metadata_status",
        "upload_status",
        "processing_state_user",
        "visibility",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "doc_type",
        "text_input_type",
        "metadata_status",
        "upload_status",
        "processing_state_user",
        "visibility",
        "tags_m2m",
    )
    search_fields = ("title", "category_event", "language", "text_input_type", "tags_m2m__name")
    ordering = ("-created_at",)

    filter_horizontal = ("tags_m2m",)

    fieldsets = (
        ("Core", {"fields": ("title", "doc_type", "text_input_type")}),
        (
            "Status",
            {
                "fields": (
                    "metadata_status",
                    "upload_status",
                    "processing_state_user",
                    "visibility",
                )
            },
        ),
        (
            "Optional metadata",
            {
                "fields": (
                    "date_start",
                    "date_end",
                    "language",
                    "category_event",
                    "tags_m2m",
                )
            },
        ),
        (
            "File (S3)",
            {
                "fields": (
                    "file_s3_key",
                    "file_original_name",
                    "mime_type",
                    "size_bytes",
                    "upload_error",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(CorrectionRequest)
class CorrectionRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "status", "scope", "created_at")
    list_filter = ("status", "scope")
    search_fields = ("document__title", "message")
    ordering = ("-created_at",)


@admin.register(DocumentTextResult)
class DocumentTextResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "document",
        "result_type",
        "engine",
        "status",
        "verification_status",
        "created_at",
        "updated_at",
    )
    list_filter = ("result_type", "engine", "status", "verification_status")
    search_fields = ("document__id", "document__title")
    ordering = ("-created_at",)

    # Read-only right now (v2). In the future will add an edit option.
    readonly_fields = (
        "document",
        "result_type",
        "engine",
        "status",
        "text",
        "error_code",
        "error_details",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Identity", {"fields": ("document", "result_type", "engine")}),
        ("Processing", {"fields": ("status", "error_code", "error_details")}),
        ("Text (read-only)", {"fields": ("text",)}),
        ("Verification", {"fields": ("verification_status",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(TranskribusRun)
class TranskribusRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "document",
        "status",
        "mode",
        "remote_doc_id",
        "collection_id",
        "model_id",
        "created_at",
    )
    list_filter = ("status", "mode")
    search_fields = (
        "document__id",
        "document__title",
        "remote_doc_id",
        "ingest_job_id",
        "recognition_job_id",
    )
    ordering = ("-created_at",)

    readonly_fields = (
        "document",
        "status",
        "mode",
        "collection_id",
        "model_id",
        "remote_doc_id",
        "pages_query",
        "page_index_to_page_nr",
        "upload_id",
        "ingest_job_id",
        "recognition_job_id",
        "engine_runtime",
        "error_code",
        "error_details",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Identity", {"fields": ("document", "status", "mode")}),
        (
            "Trp remote context",
            {
                "fields": (
                    "collection_id",
                    "model_id",
                    "remote_doc_id",
                    "pages_query",
                    "page_index_to_page_nr",
                )
            },
        ),
        (
            "Job ids",
            {"fields": ("upload_id", "ingest_job_id", "recognition_job_id")},
        ),
        (
            "Outcome",
            {"fields": ("engine_runtime", "error_code", "error_details")},
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # View/detail only — discourage manual edits to remote ids and status.
        return request.method in ("GET", "HEAD")
