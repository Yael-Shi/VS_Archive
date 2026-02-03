from django.contrib import admin
from .models import Document, CorrectionRequest, DocumentTextResult, Tag, DocumentMetadata


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created_at", "updated_at")
    search_fields = ("name",)
    ordering = ("name",)


class DocumentMetadataInline(admin.StackedInline):
    model = DocumentMetadata
    extra = 0
    can_delete = False
    fields = ("notes", "donor", "collection", "original_location", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    inlines = (DocumentMetadataInline,)

    list_display = (
        "id",
        "title",
        "doc_type",
        "metadata_status",
        "upload_status",
        "processing_state_user",
        "visibility",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "doc_type",
        "metadata_status",
        "upload_status",
        "processing_state_user",
        "visibility",
        "tags_m2m",
    )
    search_fields = ("title", "category_event", "language", "tags_m2m__name")
    ordering = ("-created_at",)

    filter_horizontal = ("tags_m2m",)

    fieldsets = (
        ("Core", {"fields": ("title", "doc_type")}),
        (
            "Status",
            {"fields": ("metadata_status", "upload_status", "processing_state_user", "visibility")},
        ),
        (
            "Optional metadata",
            {
                "fields": ("date_start", "date_end", "language", "category_event", "tags_m2m")
            },
        ),
        (
            "File (S3)",
            {"fields": ("file_s3_key", "file_original_name", "mime_type", "size_bytes", "upload_error")},
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
