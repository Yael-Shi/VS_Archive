from django.contrib import admin
from .models import Document, CorrectionRequest, DocumentTextResult


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "doc_type",
        "metadata_status",
        "upload_status",
        "visibility",
        "created_at",
    )
    list_filter = ("doc_type", "metadata_status", "upload_status", "visibility")
    search_fields = ("title",)
    ordering = ("-created_at",)


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
    )
    list_filter = ("result_type", "engine", "status", "verification_status")
    search_fields = ("document__id", "document__title")
