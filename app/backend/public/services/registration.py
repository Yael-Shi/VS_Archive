"""Public self-registration with basic application-level abuse protection.

This module provides focused registration abuse protection (CSRF via Django,
honeypot field, cache-based throttling by IP and normalized email). It is not
DDoS or infrastructure-level protection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME

User = get_user_model()

# Honeypot field — hidden in the form; bots that fill it are rejected silently.
HONEYPOT_FIELD_NAME = "company_name"

DUPLICATE_EMAIL_ERROR = "כתובת דוא״ל זו כבר רשומה במערכת."
WEAK_PASSWORD_ERROR = "הסיסמה חלשה מדי או אינה עומדת בדרישות האבטחה."
THROTTLE_ERROR = "בוצעו יותר מדי ניסיונות הרשמה. נסו שוב מאוחר יותר."

# Basic registration abuse protection limits (not DDoS protection).
IP_ATTEMPT_LIMIT = 5
EMAIL_ATTEMPT_LIMIT = 3
THROTTLE_WINDOW_SECONDS = 3600

_THROTTLE_IP_PREFIX = "registration_throttle:ip:"
_THROTTLE_EMAIL_PREFIX = "registration_throttle:email:"


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _throttle_count(key: str) -> int:
    value = cache.get(key)
    if value is None:
        return 0
    return int(value)


def is_registration_throttled(*, client_ip: str, normalized_email: str) -> bool:
    """True when prior attempts already reached the limit (next attempt is blocked)."""
    ip_key = f"{_THROTTLE_IP_PREFIX}{client_ip}"
    email_key = f"{_THROTTLE_EMAIL_PREFIX}{normalized_email}"
    if _throttle_count(ip_key) >= IP_ATTEMPT_LIMIT:
        return True
    if normalized_email and _throttle_count(email_key) >= EMAIL_ATTEMPT_LIMIT:
        return True
    return False


def record_registration_attempt(*, client_ip: str, normalized_email: str) -> None:
    """Record a registration POST attempt for basic abuse throttling."""
    for prefix, value in (
        (_THROTTLE_IP_PREFIX, client_ip),
        (_THROTTLE_EMAIL_PREFIX, normalized_email),
    ):
        if not value:
            continue
        key = f"{prefix}{value}"
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, THROTTLE_WINDOW_SECONDS)


@dataclass(frozen=True)
class RegistrationFieldValues:
    first_name: str
    last_name: str
    email: str

    def as_dict(self) -> dict[str, str]:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
        }


@dataclass
class RegistrationResult:
    user: AbstractBaseUser | None = None
    errors: list[str] | None = None
    field_values: RegistrationFieldValues | None = None
    honeypot_triggered: bool = False


def _parse_field_values(post_data: Any) -> RegistrationFieldValues:
    return RegistrationFieldValues(
        first_name=(post_data.get("first_name") or "").strip(),
        last_name=(post_data.get("last_name") or "").strip(),
        email=(post_data.get("email") or "").strip(),
    )


def _email_already_registered(normalized_email: str) -> bool:
    return (
        User.objects.filter(email__iexact=normalized_email).exists()
        or User.objects.filter(username__iexact=normalized_email).exists()
    )


def process_registration(*, request, post_data: Any) -> RegistrationResult:
    field_values = _parse_field_values(post_data)
    normalized_email = normalize_email(field_values.email)
    client_ip = get_client_ip(request)

    if (post_data.get(HONEYPOT_FIELD_NAME) or "").strip():
        return RegistrationResult(
            errors=[],
            field_values=field_values,
            honeypot_triggered=True,
        )

    if is_registration_throttled(
        client_ip=client_ip,
        normalized_email=normalized_email,
    ):
        return RegistrationResult(
            errors=[THROTTLE_ERROR],
            field_values=field_values,
        )

    record_registration_attempt(
        client_ip=client_ip,
        normalized_email=normalized_email,
    )

    errors: list[str] = []
    password1 = post_data.get("password1") or ""
    password2 = post_data.get("password2") or ""

    if not field_values.first_name:
        errors.append("יש למלא שם פרטי.")
    if not field_values.last_name:
        errors.append("יש למלא שם משפחה.")
    if not normalized_email:
        errors.append("יש למלא כתובת דוא״ל.")
    if password1 != password2:
        errors.append("הסיסמאות אינן תואמות.")
    if not password1:
        errors.append("יש למלא סיסמה.")

    if normalized_email and _email_already_registered(normalized_email):
        errors.append(DUPLICATE_EMAIL_ERROR)

    if errors:
        return RegistrationResult(errors=errors, field_values=field_values)

    user = User(
        username=normalized_email,
        email=normalized_email,
        first_name=field_values.first_name,
        last_name=field_values.last_name,
        is_staff=False,
        is_superuser=False,
    )

    try:
        validate_password(password1, user=user)
    except ValidationError:
        return RegistrationResult(
            errors=[WEAK_PASSWORD_ERROR],
            field_values=field_values,
        )

    try:
        with transaction.atomic():
            user.set_password(password1)
            user.save()
    except IntegrityError:
        return RegistrationResult(
            errors=[DUPLICATE_EMAIL_ERROR],
            field_values=field_values,
        )

    return RegistrationResult(user=user, field_values=field_values)


def user_has_family_access(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    return user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
