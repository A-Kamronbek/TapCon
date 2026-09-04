"""Context every page needs: the notification bell and support contacts."""
from django.conf import settings
from django.utils import translation

from .seo import og_locale


def site(request):
    from .services import unread_count

    user = getattr(request, "user", None)
    active = translation.get_language() or "uz"
    return {
        # Open Graph wants uz_UZ, not uz. Every page carries these, not just
        # the indexable ones: a merchant sharing their own pay page link in a
        # Telegram chat should still get a sensible preview.
        "og_locale": og_locale(active),
        "og_locale_alternates": [
            og_locale(code) for code, _label in settings.LANGUAGES if code != active
        ],
        "unread_notifications": unread_count(user) if user else 0,
        "support_phone": settings.SUPPORT_PHONE,
        "support_telegram": settings.SUPPORT_TELEGRAM,
        "card_price": settings.CARD_PRICE_UZS,
        # Which stylesheets base.html links: the eight sources while
        # developing, the built bundle in production. Deliberately not
        # Django's own `{% if debug %}`, which is also false in development
        # unless the client's IP is in INTERNAL_IPS — a page served to
        # 127.0.0.1 by one developer and to a phone on the LAN by another
        # would then disagree about which CSS it has.
        "is_dev": settings.DEBUG,
        "site_url": settings.SITE_URL,
    }
