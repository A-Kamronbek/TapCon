"""Refuse to start production with a secret that is not a secret.

These run at import time, before Gunicorn binds a port, because every failure
mode below is silent once the site is up. A placeholder SECRET_KEY signs
session cookies anybody can forge. A placeholder FIELD_ENCRYPTION_KEY
encrypts every seller's provider credentials with a key that is written in a
file in this repository — which is the same as not encrypting them.

Loud at deploy time, when someone is watching and nothing is at stake yet, is
much better than quiet for a year.
"""
from django.core.exceptions import ImproperlyConfigured

# Values that appear in .env.example, in this repository, or in a tutorial.
PLACEHOLDERS = {
    "", "change-me", "changeme", "secret", "your-secret-key",
    "django-insecure", "test", "dev", "placeholder", "xxx",
}

MIN_SECRET_KEY_LENGTH = 50
MIN_SECRET_KEY_UNIQUE_CHARS = 5


def _looks_like_a_placeholder(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if lowered in PLACEHOLDERS:
        return True
    # Django's own startproject key is prefixed this way, and it ends up
    # copied into .env more often than anyone admits.
    return lowered.startswith("django-insecure")


def check_production_site_url(site_url: str) -> None:
    """Refuse to start if providers would be told to call localhost.

    SITE_URL is the address Octo is handed as its `notify_url` at checkout,
    and it is built from this setting rather than the incoming request on
    purpose. Left at the development default in production, every Octo
    payment succeeds for the customer, charges their card, and reports back
    to a machine that does not exist — so the seller is never credited and
    the transaction sits `pending` for ever. Nothing else would look wrong.
    """
    url = (site_url or "").strip()
    if not url:
        raise ImproperlyConfigured("SITE_URL is empty.")
    if url.startswith(("http://127.0.0.1", "http://localhost", "http://0.0.0.0")):
        raise ImproperlyConfigured(
            f"SITE_URL is {url!r}, the development default. It is the address "
            "Octo is told to send its payment callbacks to — left like this, "
            "customers are charged and no payment is ever recorded. Set "
            "SITE_URL=https://tapcon.uz in .env."
        )
    if not url.startswith("https://"):
        raise ImproperlyConfigured(
            f"SITE_URL is {url!r}. Payment providers require HTTPS callbacks."
        )


def check_production_secrets(secret_key: str, encryption_key: str) -> None:
    """Raise unless both keys are real. Called from prod.py at import time."""
    if _looks_like_a_placeholder(secret_key):
        raise ImproperlyConfigured(
            "SECRET_KEY is a placeholder. Session cookies and password reset "
            "tokens are signed with it, so anyone holding this repository "
            "could forge them. Generate one with:\n"
            "  python -c \"from django.core.management.utils import "
            "get_random_secret_key; print(get_random_secret_key())\""
        )
    if len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise ImproperlyConfigured(
            f"SECRET_KEY is {len(secret_key)} characters; Django generates 50. "
            "A short key is a guessable key."
        )
    # Django's own security.W009 wants at least five distinct characters, and
    # it is right: a long key made of one repeated character has no more
    # entropy than that character. A warning is easy to scroll past on a
    # deploy, so it is an error here.
    if len(set(secret_key)) < MIN_SECRET_KEY_UNIQUE_CHARS:
        raise ImproperlyConfigured(
            f"SECRET_KEY uses only {len(set(secret_key))} distinct characters. "
            "Length is not randomness — generate a real one."
        )
    if _looks_like_a_placeholder(encryption_key):
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is a placeholder. Every seller's Payme, "
            "Click, Uzum, Paynet and Octo credentials are encrypted with it; "
            "a key from this repository means they are not encrypted at all. "
            "Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "Then back it up somewhere separate from the database: without "
            "it, every stored credential is unrecoverable and every seller "
            "has to re-enter theirs."
        )
