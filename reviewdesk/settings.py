import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if "test" in __import__("sys").argv:
        SECRET_KEY = "test-only-not-for-deployment"
    else:
        raise RuntimeError("Set DJANGO_SECRET_KEY before starting Review Desk")
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "reviewdesk.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "core.workflow_views.owner_context",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "reviewdesk.wsgi.application"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "reviewdesk.sqlite3",
        "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
    }
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Jakarta"
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if x]
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# Enable only behind a trusted, loopback-bound reverse proxy that overwrites this header.
if os.environ.get("TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8040").rstrip("/")
CREDENTIAL_KEY = os.environ.get("CREDENTIAL_KEY", "")
LIVE_WRITES = os.environ.get("LIVE_WRITES", "0") == "1"

SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]

# Explicit account binding; superuser status alone does not grant dashboard retry.
DASHBOARD_RETRY_USER_ID = int(os.environ.get("DASHBOARD_RETRY_USER_ID", "0"))

# Fingerprinted URLs prevent old dashboard assets surviving a deployment in browser/CDN caches.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or "test" in __import__("sys").argv
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

CLI_RUNNER_IMAGE = os.environ.get("CLI_RUNNER_IMAGE", "")
CLI_CODEX_AUTH_VOLUME = os.environ.get("CLI_CODEX_AUTH_VOLUME", "auto-reviewer_codex-auth")
CLI_CLAUDE_AUTH_VOLUME = os.environ.get("CLI_CLAUDE_AUTH_VOLUME", "auto-reviewer_claude-auth")

# Opt-in final-summary journal; service credentials never enter CLI containers.
JOURNAL_ENABLED = os.environ.get("JOURNAL_ENABLED", "0") == "1"
JOURNAL_ENDPOINT = os.environ.get("JOURNAL_ENDPOINT", "")
JOURNAL_CREDENTIAL_ID = int(os.environ.get("JOURNAL_CREDENTIAL_ID", "0"))
JOURNAL_REPOSITORIES = [
    x.strip() for x in os.environ.get("JOURNAL_REPOSITORIES", "").split(",") if x.strip()
]
