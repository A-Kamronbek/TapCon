"""Local development settings. SQLite; switched to PostgreSQL in prod.py."""
from .base import *  # noqa: F401,F403
from .base import BASE_DIR

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "0.0.0.0", "testserver"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# OTP codes are printed to the console instead of sent by SMS in dev.
SMS_BACKEND = "console"

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Serve static without the manifest so a missing collectstatic doesn't break dev.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

INTERNAL_IPS = ["127.0.0.1"]
