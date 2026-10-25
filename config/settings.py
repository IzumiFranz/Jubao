"""
Django settings for config project.
Configured for both local development and PythonAnywhere free-plan deployment.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_file(dotenv_path: Path):
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_file(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DEVELOPMENT_FALLBACK_SECRET_KEY = (
    "django-insecure-0eq#g=p-akn$$s$2tbt1+0c_fdp1&kzzlmp7coy++r)@u9mx4t"
)


def resolve_secret_key(*, debug: bool, raw_secret_key: str | None) -> str:
    candidate = (raw_secret_key or "").strip()
    if debug:
        return candidate or DEVELOPMENT_FALLBACK_SECRET_KEY
    if (
        not candidate
        or candidate == DEVELOPMENT_FALLBACK_SECRET_KEY
        or candidate.startswith("django-insecure-")
    ):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a strong production secret when DJANGO_DEBUG=False. "
            "The development fallback key is not permitted when DEBUG is disabled."
        )
    return candidate


DEBUG = _env_bool("DJANGO_DEBUG", True)
SECRET_KEY = resolve_secret_key(
    debug=DEBUG,
    raw_secret_key=os.environ.get("DJANGO_SECRET_KEY"),
)

_raw_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
if _raw_hosts.strip():
    ALLOWED_HOSTS = [h.strip() for h in _raw_hosts.split(",") if h.strip()]
else:
    ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"] if DEBUG else []


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.budgets.apps.BudgetsConfig",
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

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.views.user_preferences_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
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

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "budgets:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Exchange-rate provider configuration (PythonAnywhere free-plan compatible)
EXCHANGE_RATE_PROVIDER = os.environ.get("EXCHANGE_RATE_PROVIDER", "frankfurter")
OPENEXCHANGERATES_APP_ID = os.environ.get("OPENEXCHANGERATES_APP_ID", "")

# HTTPS & Cookie security settings for production deployment
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", True)
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
    },
}
