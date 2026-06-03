from django.contrib import admin

from public.models import PublicContentBlock


@admin.register(PublicContentBlock)
class PublicContentBlockAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "updated_at")
    search_fields = ("key", "title")
    readonly_fields = ("created_at", "updated_at")
