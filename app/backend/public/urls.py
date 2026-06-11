from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="public-home"),
    path("about/", views.about, name="public-about"),
    path("about/edit/", views.edit_public_content, name="public-content-edit"),
    path("accounts/register/", views.register, name="public-register"),
    path("accounts/pending/", views.pending_approval, name="pending-approval"),
]
