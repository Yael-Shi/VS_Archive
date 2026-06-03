import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from public.services.public_content import (
    EDITABLE_PUBLIC_BLOCKS,
    get_all_public_content,
    save_public_content_from_post,
)

logger = logging.getLogger(__name__)


def _is_staff_user(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def home(request):
    return render(request, "public/home.html")


def about(request):
    return render(
        request,
        "public/about.html",
        {"content": get_all_public_content()},
    )


@login_required
def edit_public_content(request):
    if not _is_staff_user(request.user):
        return render(request, "public/forbidden.html", status=403)

    saved = False
    error_message = ""

    if request.method == "POST":
        try:
            save_public_content_from_post(request.POST)
            saved = True
        except Exception:
            logger.exception("Failed to save public content from edit form")
            error_message = "שמירת התוכן נכשלה. נסו שוב."

    content = get_all_public_content()
    blocks = [
        {
            "def": block_def,
            "content": content[block_def["key"]],
        }
        for block_def in EDITABLE_PUBLIC_BLOCKS
    ]

    return render(
        request,
        "public/edit_public_content.html",
        {
            "blocks": blocks,
            "saved": saved,
            "error_message": error_message,
        },
    )
