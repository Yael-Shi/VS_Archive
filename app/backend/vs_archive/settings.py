from pathlib import Path
import os
import re
import tempfile

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP  # pyright: ignore[reportMissingImports]

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

BASE_DIR = Path(__file__).resolve().parent.parent

google_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if google_json:
    tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tfile.write(google_json.encode("utf-8"))
    tfile.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tfile.name

_LOCAL_DEV_SECRET_KEY = "django-insecure-local-dev-only-change-me"
_LOCAL_DEV_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_LOCAL_DEBUG_CSRF_ORIGINS = ["https://*.ngrok-free.dev"]


def _split_env_list(value: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"[\s,]+", value.strip()):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def _log_level_from_env(name: str, *, default: str = "INFO") -> str:
    value = os.getenv(name, default).strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise ImproperlyConfigured(
            f"{name} must be one of {sorted(_VALID_LOG_LEVELS)}; got {value!r}."
        )
    return value


DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"

_django_secret_key = os.getenv("DJANGO_SECRET_KEY", "").strip()
if _django_secret_key:
    SECRET_KEY = _django_secret_key
elif DEBUG:
    SECRET_KEY = _LOCAL_DEV_SECRET_KEY
else:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is False."
    )

UPLOADS_BUCKET_NAME = os.getenv("UPLOADS_BUCKET_NAME") or os.getenv("S3_BUCKET") or ""
AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")

_allowed_hosts_raw = os.getenv("ALLOWED_HOSTS", "").strip()
if _allowed_hosts_raw:
    ALLOWED_HOSTS = _split_env_list(_allowed_hosts_raw)
elif DEBUG:
    ALLOWED_HOSTS = list(_LOCAL_DEV_ALLOWED_HOSTS)
else:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set when DJANGO_DEBUG is False.")

if not DEBUG and "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must not contain '*' when DJANGO_DEBUG is False."
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "documents",
    "public.apps.PublicConfig",
]

MIDDLEWARE = [
    "vs_archive.alb_health_check_middleware.AlbHealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vs_archive.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vs_archive.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "vs_archive"),
        "USER": os.getenv("DB_USER", "vs_archive_user"),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = DEBUG

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/api/ui/documents/"
LOGOUT_REDIRECT_URL = "/"

if DEBUG:
    _csrf_trusted_origins_default = " ".join(_LOCAL_DEBUG_CSRF_ORIGINS)
else:
    _csrf_trusted_origins_default = ""

_csrf_trusted_origins_raw = os.getenv(
    "CSRF_TRUSTED_ORIGINS", _csrf_trusted_origins_default
).strip()
CSRF_TRUSTED_ORIGINS = _split_env_list(_csrf_trusted_origins_raw)

if not DEBUG and not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured(
        "CSRF_TRUSTED_ORIGINS must be set when DJANGO_DEBUG is False."
    )

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True

# Smallest CSP change for public YouTube click-to-load: restrict outbound frames
# only. Other directives remain unset so existing scripts/styles/fonts keep working.
# Approved embed origin is youtube-nocookie.com only (no broad video wildcards).
SECURE_CSP = {
    "frame-src": [CSP.SELF, "https://www.youtube-nocookie.com"],
}

# Preserve Django's same-origin Referrer-Policy site-wide (does not send the
# archive-item path cross-origin). Activated YouTube click-to-load iframes set
# referrerPolicy="strict-origin-when-cross-origin" client-side so YouTube receives
# an origin-level Referer (avoids error 153) without weakening this site policy.
SECURE_REFERRER_POLICY = "same-origin"

LOG_LEVEL = _log_level_from_env("LOG_LEVEL")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(levelname)s %(asctime)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        # Keep common AWS/HTTP client libraries quiet when LOG_LEVEL=DEBUG.
        "botocore": {"level": "WARNING"},
        "boto3": {"level": "WARNING"},
        "urllib3": {"level": "WARNING"},
    },
}
