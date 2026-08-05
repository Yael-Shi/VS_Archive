from django.contrib import admin

from documents.services.archive_item_access import (
    archive_item_queryset_for_user,
    filter_archive_items_for_user,
)
from documents.services.document_access import (
    document_queryset_for_user,
    filter_documents_for_user,
)

from .models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    Document,
    CorrectionRequest,
    DocumentTextResult,
    ManualTextContent,
    PhotoContent,
    Tag,
    DocumentMetadata,
    TranskribusRun,
)


class _DiscoveryTaxonomyNameSyncAdmin(admin.ModelAdmin):
    """Fan-out ArchiveItemSearchIndex sync when a discovery taxonomy name changes."""

    def save_model(self, request, obj, form, change):
        previous_name = None
        if change and obj.pk is not None:
            previous_name = (
                type(obj)
                .objects.filter(pk=obj.pk)
                .values_list("name", flat=True)
                .first()
            )
        super().save_model(request, obj, form, change)
        if change and previous_name is not None and previous_name != obj.name:
            from documents.services.archive_search_index import (
                sync_archive_item_search_indexes,
            )

            archive_item_ids = obj.archive_items.values_list("pk", flat=True)
            sync_archive_item_search_indexes(archive_item_ids)


class _VisibilityScopedAdminMixin:
    """Scope Django Admin rows and FK choices via centralized visibility policy."""

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.related_model is ArchiveItem:
            kwargs["queryset"] = archive_item_queryset_for_user(request.user)
        elif db_field.related_model is Document:
            kwargs["queryset"] = document_queryset_for_user(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Tag)
class TagAdmin(_DiscoveryTaxonomyNameSyncAdmin):
    list_display = ("id", "name", "created_at", "updated_at")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(ArchiveCategory)
class ArchiveCategoryAdmin(_DiscoveryTaxonomyNameSyncAdmin):
    list_display = ("id", "name", "slug", "created_at", "updated_at")
    search_fields = ("name", "slug")
    ordering = ("name",)


@admin.register(ArchiveEvent)
class ArchiveEventAdmin(_DiscoveryTaxonomyNameSyncAdmin):
    list_display = ("id", "name", "slug", "date_precision", "created_at", "updated_at")
    search_fields = ("name", "slug")
    list_filter = ("date_precision",)
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


class TranskribusRunInline(_VisibilityScopedAdminMixin, admin.TabularInline):
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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document__in=document_queryset_for_user(request.user))


@admin.register(ArchiveItem)
class ArchiveItemAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "item_type",
        "visibility",
        "metadata_status",
        "date_precision",
        "created_at",
    )
    list_filter = ("item_type", "visibility", "metadata_status", "date_precision")
    search_fields = ("title",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return filter_archive_items_for_user(
            request.user, super().get_queryset(request)
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ManualTextContent)
class ManualTextContentAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "archive_item", "created_at", "updated_at")
    search_fields = ("archive_item__title", "body")
    ordering = ("-created_at",)
    readonly_fields = ("archive_item", "body", "created_at", "updated_at")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(archive_item__in=archive_item_queryset_for_user(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PhotoContent)
class PhotoContentAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "archive_item",
        "original_filename",
        "original_mime_type",
        "created_at",
        "updated_at",
    )
    search_fields = ("archive_item__title", "original_filename", "original_file_key")
    ordering = ("-created_at",)
    readonly_fields = (
        "archive_item",
        "original_file_key",
        "original_filename",
        "original_mime_type",
        "original_size_bytes",
        "width",
        "height",
        "thumbnail_file_key",
        "thumbnail_mime_type",
        "thumbnail_size_bytes",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(archive_item__in=archive_item_queryset_for_user(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
    inlines = (DocumentMetadataInline, TranskribusRunInline)

    list_display = (
        "id",
        "canonical_title",
        "archive_item",
        "doc_type",
        "text_input_type",
        "canonical_metadata_status",
        "upload_status",
        "processing_state_user",
        "canonical_visibility",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "doc_type",
        "text_input_type",
        "archive_item__metadata_status",
        "upload_status",
        "processing_state_user",
        "archive_item__visibility",
        "tags_m2m",
    )
    search_fields = (
        "archive_item__title",
        "category_event",
        "language",
        "text_input_type",
        "tags_m2m__name",
    )
    ordering = ("-created_at",)

    filter_horizontal = ("tags_m2m",)

    fieldsets = (
        ("Core", {"fields": ("archive_item", "doc_type", "text_input_type")}),
        (
            "Processing status",
            {
                "fields": (
                    "upload_status",
                    "processing_state_user",
                )
            },
        ),
        (
            "Optional metadata",
            {
                "fields": (
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
    readonly_fields = (
        "archive_item",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("archive_item")
        return filter_documents_for_user(request.user, qs)

    @admin.display(description="Title", ordering="archive_item__title")
    def canonical_title(self, obj: Document) -> str:
        return obj.archive_item.title

    @admin.display(
        description="Metadata status",
        ordering="archive_item__metadata_status",
    )
    def canonical_metadata_status(self, obj: Document) -> str:
        return obj.archive_item.metadata_status

    @admin.display(description="Visibility", ordering="archive_item__visibility")
    def canonical_visibility(self, obj: Document) -> str:
        return obj.archive_item.visibility

    def has_add_permission(self, request):
        return False


@admin.register(CorrectionRequest)
class CorrectionRequestAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "document", "status", "scope", "created_at")
    list_filter = ("status", "scope")
    search_fields = ("document__archive_item__title", "message")
    ordering = ("-created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document__in=document_queryset_for_user(request.user))


@admin.register(DocumentTextResult)
class DocumentTextResultAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
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
    search_fields = ("document__id", "document__archive_item__title")
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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document__in=document_queryset_for_user(request.user))


@admin.register(TranskribusRun)
class TranskribusRunAdmin(_VisibilityScopedAdminMixin, admin.ModelAdmin):
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
        "document__archive_item__title",
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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document__in=document_queryset_for_user(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # View/detail only — discourage manual edits to remote ids and status.
        return request.method in ("GET", "HEAD")
