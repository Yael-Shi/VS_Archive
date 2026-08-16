import logging

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse

from public.services.homepage_archive import homepage_archive_cards
from public.services.public_content import (
    EDITABLE_PUBLIC_BLOCKS,
    get_all_public_content,
    save_public_content_from_post,
)
from public.services.registration import (
    RegistrationFieldValues,
    process_registration,
    user_has_family_access,
)

logger = logging.getLogger(__name__)


def _is_staff_user(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _empty_registration_field_values() -> dict[str, str]:
    return RegistrationFieldValues(
        first_name="",
        last_name="",
        email="",
    ).as_dict()


def home(request):
    return render(
        request,
        "public/home.html",
        {
            "homepage_archive_cards": homepage_archive_cards(),
        },
    )


def register(request):
    if request.user.is_authenticated:
        if user_has_family_access(request.user):
            return redirect(reverse("archive-list"))
        return redirect(reverse("pending-approval"))

    field_values = _empty_registration_field_values()
    form_errors: list[str] = []

    if request.method == "POST":
        result = process_registration(request=request, post_data=request.POST)
        field_values = (
            result.field_values.as_dict()
            if result.field_values is not None
            else field_values
        )

        if result.honeypot_triggered:
            return render(
                request,
                "registration/register.html",
                {
                    "form_errors": [],
                    "field_values": _empty_registration_field_values(),
                },
            )

        if result.user is not None:
            login(request, result.user)
            return redirect(reverse("pending-approval"))

        if result.errors:
            form_errors = result.errors

    return render(
        request,
        "registration/register.html",
        {
            "form_errors": form_errors,
            "field_values": field_values,
        },
    )


@login_required
def pending_approval(request):
    if user_has_family_access(request.user):
        return redirect(reverse("archive-list"))

    return render(request, "registration/pending_approval.html")


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
