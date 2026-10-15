"""
Django settings for SFL ERP System — wholesale sports card & collectibles distribution.

Environment-driven configuration. See .env.example for all variables.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key-change-me-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
# Railway injects the public hostname; trust it without extra configuration.
if env("RAILWAY_PUBLIC_DOMAIN"):
    _rd = env("RAILWAY_PUBLIC_DOMAIN")
    if _rd not in ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_rd)
    if f"https://{_rd}" not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(f"https://{_rd}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third party
    "rest_framework",
    "django_q",
    # SFL ERP System apps
    "core",
    "inventory",
    "customers",
    "orders",
    "invoicing",
    "payments",
    "expenses",
    "shipping",
    "reports",
    "accounting",
    "banking",
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
    "core.middleware.RoleRedirectMiddleware",
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
                "core.context_processors.company",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database -----------------------------------------------------------
# Postgres in docker / production; SQLite fallback for bare-metal dev.
if env("DATABASE_URL"):
    # Railway / Heroku-style single URL: postgres://user:pass@host:port/name
    from urllib.parse import urlparse
    _db = urlparse(env("DATABASE_URL"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db.path.lstrip("/"),
            "USER": _db.username or "",
            "PASSWORD": _db.password or "",
            "HOST": _db.hostname or "",
            "PORT": str(_db.port or 5432),
            "CONN_MAX_AGE": 60,
        }
    }
elif env("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "sfl_erp"),
            "USER": env("POSTGRES_USER", "sfl_erp"),
            "PASSWORD": env("POSTGRES_PASSWORD", "sfl_erp"),
            "HOST": env("POSTGRES_HOST", "db"),
            "PORT": env("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Localization: all business logic in Eastern time ---------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# --- DRF (picking API) ----------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# --- Background tasks: Django-Q2 with Redis broker ------------------------
Q_CLUSTER = {
    "name": "sfl_erp",
    "workers": int(env("Q_WORKERS", "2")),
    "timeout": 300,
    "retry": 360,
    "queue_limit": 50,
    "bulk": 10,
    "orm": "default",  # ORM broker fallback when Redis is not configured
}
if env("REDIS_URL"):
    # Railway-style single URL: redis://default:pass@host:port
    from urllib.parse import urlparse
    _r = urlparse(env("REDIS_URL"))
    Q_CLUSTER.pop("orm", None)
    Q_CLUSTER["redis"] = {
        "host": _r.hostname or "redis", "port": int(_r.port or 6379),
        "db": int((_r.path or "/0").lstrip("/") or 0),
        "password": _r.password or None,
        "ssl": _r.scheme == "rediss",
    }
elif env("REDIS_HOST"):
    Q_CLUSTER.pop("orm", None)
    Q_CLUSTER["redis"] = {
        "host": env("REDIS_HOST", "redis"),
        "port": int(env("REDIS_PORT", "6379")),
        "db": 0,
    }

# --- Email ----------------------------------------------------------------
if env("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@example.com")

# --- Shipping provider credentials ----------------------------------------
SHIPSTATION_API_KEY = env("SHIPSTATION_API_KEY", "")
SHIPSTATION_API_SECRET = env("SHIPSTATION_API_SECRET", "")
SHIPSTATION_BASE_URL = env("SHIPSTATION_BASE_URL", "https://ssapi.shipstation.com")
SHIPPO_API_TOKEN = env("SHIPPO_API_TOKEN", "")
SHIPPO_BASE_URL = env("SHIPPO_BASE_URL", "https://api.goshippo.com")
USHIP_API_KEY = env("USHIP_API_KEY", "")
USHIP_BASE_URL = env("USHIP_BASE_URL", "https://api.uship.com")

# --- Bank feed (Plaid) — optional; CSV import works without it ------------
PLAID_CLIENT_ID = env("PLAID_CLIENT_ID", "")
PLAID_SECRET = env("PLAID_SECRET", "")
PLAID_ENV = env("PLAID_ENV", "sandbox")          # sandbox | production
PLAID_PRODUCTS = ["transactions"]
PLAID_COUNTRY_CODES = ["US"]
# Public URL of this app, used for the Plaid Link redirect/webhook (optional)
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "")

# Security hardening applied automatically when DEBUG off
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
