"""Production settings: PostgreSQL, HTTPS-only cookies, strict headers."""
from .base import *  # noqa: F401,F403
from .base import FIELD_ENCRYPTION_KEY, MIDDLEWARE, SECRET_KEY, SITE_URL, env
from .checks import check_production_secrets, check_production_site_url

DEBUG = False

# Before anything else. A placeholder key here is not a misconfiguration that
# shows up later — it is a site that looks completely normal while every
# seller's provider credentials sit behind a key printed in this repository.
check_production_secrets(SECRET_KEY, FIELD_ENCRYPTION_KEY)
# And the address providers are told to call back on. A localhost SITE_URL in
# production charges customers and records nothing.
check_production_site_url(SITE_URL)

# Serve compressed, hashed static files straight from Gunicorn.
MIDDLEWARE = MIDDLEWARE.copy()
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = 60

# --- security ------------------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

CSRF_TRUSTED_ORIGINS = env(
    "CSRF_TRUSTED_ORIGINS", default=["https://tapcon.uz", "https://www.tapcon.uz"]
)

SMS_BACKEND = "eskiz"  # real SMS provider, configured in Phase 1

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        # Never log payment credentials or raw webhook secrets.
        "payments": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
