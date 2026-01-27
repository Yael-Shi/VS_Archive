from django.urls import path
from . import views

urlpatterns = [
    path("uploads/create/", views.create_upload, name="uploads-create"),
    path("uploads/<int:doc_id>/complete/", views.upload_complete, name="uploads-complete"),
    path("documents/", views.documents_list_api, name="documents-list-api"),
    path("ui/documents/", views.documents_list_page, name="documents-list-page"),
    path("ui/documents/<int:doc_id>/", views.document_detail_page, name="documents-detail-page"),
]
