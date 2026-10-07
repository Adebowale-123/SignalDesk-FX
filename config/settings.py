"""SignalDesk FX settings. Secrets and environment-specific values come from environment variables (.env locally)."""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

try:  # use the operating system's certificate store for HTTPS (fixes corporate/local CA setups)
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass


def env(key, default=None):
    value = os.environ.get(key)
    return default if value in (None, "") else value


def env_bool(key, default=False):
    value = os.environ.get(key)
    return default if value in (None, "") else value.strip().lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    return [item.strip() for item in (env(key, default) or "").split(",") if item.strip()]


DEBUG = env_bool("DEBUG", True)
SECRET_KEY = env("SECRET_KEY", "dev-insecure-key-change-me")
if not DEBUG and SECRET_KEY.startswith("dev-insecure"):
    raise RuntimeError("SECRET_KEY must be set in production.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
RENDER_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME")
if RENDER_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_HOSTNAME}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.market",
    "apps.signals",
    "apps.alerts",
    "apps.news",
    "apps.macro",
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
    "apps.core.middleware.LoginRequiredMiddleware",
    "apps.core.middleware.EngineOnRequestMiddleware",
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
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"

# On a host the disk is wiped on every restart, so SQLite would silently lose all data there.
if RENDER_HOSTNAME and not env("DATABASE_URL"):
    raise RuntimeError("DATABASE_URL must be set on Render (e.g. a Neon PostgreSQL connection string).")
DATABASES = {"default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600, conn_health_checks=True)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "signals:board"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = env("TIME_ZONE", "Africa/Lagos")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "SignalDesk FX <alerts@signaldesk.local>")

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30

# Run the analysis on incoming requests (throttled) when there is no background worker (e.g. free hosting).
ENGINE_ON_REQUEST = env_bool("ENGINE_ON_REQUEST", False)
# Secret for /engine/tick/?key=... (an external scheduler calls it to run the analysis on a schedule).
ENGINE_TICK_KEY = env("ENGINE_TICK_KEY", "")

TESTING = len(os.sys.argv) > 1 and os.sys.argv[1] == "test"
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# Start self-tuning automatically after analysis cycles (off in tests).
AUTO_MAINTENANCE = not TESTING

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"apps": {"handlers": ["console"], "level": "WARNING" if TESTING else env("LOG_LEVEL", "INFO")}},
}
