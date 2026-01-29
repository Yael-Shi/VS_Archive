from django.contrib import admin
from .models import Document, CorrectionRequest


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
