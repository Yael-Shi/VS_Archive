from django.contrib import admin
from django.db import connection
from django.urls import path, include
from django.http import HttpResponse


def health(request):
    return HttpResponse("ok")


def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return HttpResponse("unavailable", status=503)
    return HttpResponse("ok")


urlpatterns = [
    path("", include("public.urls")),
    path("admin/", admin.site.urls),
    path("health/", health),
    path("ready/", ready),
    path("accounts/", include("django.contrib.auth.urls")),
    path("archive/", include("documents.archive_urls")),
    path("api/", include("documents.urls")),
]
