"""Shared settings for TapCon. Environment-specific bits live in dev.py / prod.py."""
from pathlib import Path

import environ
from django.utils.translation import gettext_lazy as _

# BASE_DIR = repo root (contains manage.py)
BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Key for django-encrypted-model-fields (seller provider credentials).
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")

# Login is by phone number + OTP, not email. See accounts app.
AUTH_USER_MODEL = "accounts.User"

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # For the sitemap.xml template only. `django.contrib.sites` is
    # deliberately not installed with it: without it the sitemap builds its
    # domain from the request's own host, which is what we want — one
    # deployment, one domain, and no Site row to keep in step with reality.
    "django.contrib.sitemaps",
]

THIRD_PARTY_APPS = [
    "encrypted_model_fields",
    # Ships the PaymentTransaction model its webhook handlers read and write.
    "tolov.integrations.django",
]

LOCAL_APPS = [
    "core",
    "accounts",
    "merchants",
    "payments",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise is added in prod.py only — runserver serves static in dev.
    "django.contrib.sessions.middleware.SessionMiddleware",
    # LocaleMiddleware must sit after Session and before Common.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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
                "django.template.context_processors.i18n",
                "core.context_processors.site",
            ],
        },
    },
]

# Our own subclasses: Django's uz catalogue has no translation for any of
# these messages, so the stock validators would show English to Uzbek users.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "accounts.validators.UserAttributeSimilarityValidator"},
    {"NAME": "accounts.validators.MinimumLengthValidator"},
    {"NAME": "accounts.validators.CommonPasswordValidator"},
    {"NAME": "accounts.validators.NumericPasswordValidator"},
]

# --- i18n / l10n ---------------------------------------------------------
LANGUAGE_CODE = "uz"
LANGUAGES = [
    ("uz", _("O'zbekcha")),
    ("ru", _("Русский")),
    ("en", _("English")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True
# Money is formatted explicitly (thousand separators, UZS), not by locale.
USE_THOUSAND_SEPARATOR = False

# --- static / media ------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- cache ---------------------------------------------------------------
# A database table, not local memory. The cache holds the rate-limit counters,
# and Gunicorn runs several worker processes: with the default LocMemCache
# each worker would keep its own count, so "5 codes per hour" would really be
# five per worker, and an attacker would only have to be handed a different
# worker on the next request. A table is shared by all of them.
#
# Redis would be faster and buys nothing here — these are a handful of small
# reads per request, on one VPS, and it would be another service to install,
# secure and keep running for a payments product to depend on.
#
# `manage.py createcachetable` creates it; the deploy script runs that.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "tapcon_cache",
        "TIMEOUT": 300,
        "OPTIONS": {"MAX_ENTRIES": 10_000, "CULL_FREQUENCY": 3},
    }
}

# --- TapCon domain settings ---------------------------------------------
# Currency is fixed for v1.
CURRENCY_CODE = "UZS"

# Telegram notifications for card orders / contact messages (Phase 2).
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", default="")

# URL names, not paths: every user-facing URL carries a language prefix.
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "merchants:dashboard"
LOGOUT_REDIRECT_URL = "core:home"

# --- phone OTP -----------------------------------------------------------
OTP_LENGTH = 6
OTP_TTL_SECONDS = 300           # a code is valid for 5 minutes
OTP_MAX_ATTEMPTS = 5            # wrong guesses before the code dies
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_PER_HOUR = 5            # codes sent to one number per hour

# Which SMS backend accounts.sms uses. Overridden per environment.
SMS_BACKEND = "console"

# --- Eskiz SMS (production) ---------------------------------------------
ESKIZ_BASE_URL = env("ESKIZ_BASE_URL", default="https://notify.eskiz.uz/api")
ESKIZ_EMAIL = env("ESKIZ_EMAIL", default="")
ESKIZ_PASSWORD = env("ESKIZ_PASSWORD", default="")
ESKIZ_SENDER = env("ESKIZ_SENDER", default="4546")

# Seller profile defaults (UZS)
SELLER_DEFAULT_MIN_AMOUNT = 1000
SELLER_DEFAULT_MAX_AMOUNT = 10_000_000

# --- commercial ----------------------------------------------------------
# One-time price per NFC card. Connecting a seller's provider merchant
# accounts is a separate service, quoted per business.
CARD_PRICE_UZS = env.int("CARD_PRICE_UZS", default=100_000)

# --- support contacts (shown on the pay page and marketing site) ---------
SUPPORT_PHONE = env("SUPPORT_PHONE", default="+998 90 000 00 00")
SUPPORT_TELEGRAM = env("SUPPORT_TELEGRAM", default="https://t.me/tapcon_uz")

# --- tolov ---------------------------------------------------------------
# Deliberately contains NO credentials. TapCon is multi-tenant: every seller
# has their own Payme/Click/Uzum keys, loaded from their ProviderIntegration
# row at request time by payments/webhooks.py.
#
# What lives here is the part that IS the same for every seller: which model
# a payment is *for*, and which of its fields hold the id and the amount.
# tolov's webhook handlers read these in __init__ and then only ever use the
# instance attributes, which is exactly why the per-seller override works.
_TOLOV_ACCOUNT = {
    "ACCOUNT_MODEL": "payments.models.Transaction",
    "ACCOUNT_FIELD": "id",
    "AMOUNT_FIELD": "amount",
    # One transaction row per payment attempt, so the amount must match
    # exactly and a second pending transaction on the same row is refused.
    "ONE_TIME_PAYMENT": True,
}
TOLOV = {
    "PAYME": dict(_TOLOV_ACCOUNT),
    "CLICK": dict(_TOLOV_ACCOUNT),
    "UZUM": dict(_TOLOV_ACCOUNT),
    "PAYNET": dict(_TOLOV_ACCOUNT),
    # Octo is the exception, and not because we changed our minds about
    # credentials in settings. Its webhook *raises* in __init__ unless a shop
    # id, secret and callback key are already present — so unlike the other
    # five it cannot even be constructed before we get a chance to inject the
    # seller's own. These placeholders exist only to get past that check, and
    # payments/webhooks.py replaces all three per request.
    #
    # They are deliberately unusable rather than blank, and TEST_MODE stays
    # False: if the injection ever failed to happen, a callback would be
    # checked against a placeholder key and rejected. Wrong, but closed.
    "OCTO_BANK": {
        **_TOLOV_ACCOUNT,
        "OCTO_SHOP_ID": "per-seller-see-payments-webhooks",
        "OCTO_SECRET": "per-seller-see-payments-webhooks",
        "OCTO_UNIQUE_KEY": "per-seller-see-payments-webhooks",
        "TEST_MODE": False,
    },
    # Multicard is the second handler that refuses to be constructed without
    # credentials: its __init__ raises ImproperlyConfigured unless a callback
    # secret is already in settings, so without this placeholder every
    # Multicard callback answered 500 and the payment behind it stayed
    # `pending` for ever. Same treatment as Octo — unusable on purpose, so a
    # failed injection fails closed, and replaced per request in
    # payments/webhooks.py.
    "MULTICARD": {
        **_TOLOV_ACCOUNT,
        "SECRET": "per-seller-see-payments-webhooks",
        "STORE_ID": None,
    },
}

# Absolute base for URLs we hand to a provider. Octo is told where to send
# its callback at checkout time, and that address has to be the stable public
# one — not whichever host the customer happened to reach us on.
SITE_URL = env("SITE_URL", default="http://127.0.0.1:8000").rstrip("/")
