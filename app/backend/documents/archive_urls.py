from django.urls import path

from . import views

urlpatterns = [
    path("", views.archive_list_page, name="archive-list"),
    path("manage/", views.archive_manage_list_page, name="archive-manage-list"),
    path(
        "manage/new/manual-text/",
        views.archive_manage_manual_text_create_page,
        name="archive-manage-manual-text-create",
    ),
    path(
        "manage/<int:item_id>/edit/",
        views.archive_manage_edit_page,
        name="archive-manage-edit",
    ),
    path("<int:item_id>/", views.archive_detail_page, name="archive-detail"),
]
