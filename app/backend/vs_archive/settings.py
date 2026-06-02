from pathlib import Path
import os
import tempfile

BASE_DIR = Path(__file__).resolve().parent.parent

google_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if google_json:
    tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tfile.write(google_json.encode("utf-8"))
    tfile.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tfile.name
# --------------------------------------------------------

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-!5*72sfq!20ubj(3&k48o_##@+_+%5t%ph@*66@&1x)yxzydz&"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

UPLOADS_BUCKET_NAME = os.getenv("UPLOADS_BUCKET_NAME") or os.getenv("S3_BUCKET") or ""
AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")

ALLOWED_HOSTS = ["*"]  # dev only
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", SECRET_KEY)
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "documents",
    "public.apps.PublicConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

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


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

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


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"

# Auth redirects
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/api/ui/documents/"
LOGOUT_REDIRECT_URL = "/"

CSRF_TRUSTED_ORIGINS = [
    "https://*.ngrok-free.dev",
    "https://vs-archive.com",
    "http://vs-arc-vsarc-arz8x1qh0dhg-1038935491.eu-central-1.elb.amazonaws.com",
]

CSRF_TRUSTED_ORIGINS = os.getenv(
    "CSRF_TRUSTED_ORIGINS",
    " ".join(CSRF_TRUSTED_ORIGINS)
).split()


CSRF_TRUSTED_ORIGINS = [s.strip() for s in CSRF_TRUSTED_ORIGINS if s.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# build v2.2
