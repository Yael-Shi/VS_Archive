from django.urls import path

from . import views

urlpatterns = [
    path("", views.archive_list_page, name="archive-list"),
    path(
        "categories/<int:category_id>/",
        views.archive_category_browse_page,
        name="archive-category-browse",
    ),
    path(
        "events/<int:event_id>/",
        views.archive_event_browse_page,
        name="archive-event-browse",
    ),
    path(
        "tags/<int:tag_id>/",
        views.archive_tag_browse_page,
        name="archive-tag-browse",
    ),
    path("manage/", views.archive_manage_list_page, name="archive-manage-list"),
    path(
        "manage/new/",
        views.archive_manage_new_page,
        name="archive-manage-new",
    ),
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
    path(
        "manage/<int:item_id>/delete/",
        views.archive_manage_delete_page,
        name="archive-manage-delete",
    ),
    path(
        "manage/family-access/",
        views.archive_manage_family_access_page,
        name="archive-manage-family-access",
    ),
    path("<int:item_id>/", views.archive_detail_page, name="archive-detail"),
]
