"""Crawl every page in every language and report untranslated English leaking through.

    python tools/audit_pages.py

Uses Django's test client, so it needs no running server and no real SMS.
"""
import os
import pathlib
import re
import sys

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

# Words that should never appear on a page rendered in uz or ru.
ENGLISH_MARKERS = [
    "Sign in", "Sign out", "Dashboard", "Settings", "Password", "Continue",
    "Confirm", "Save", "Business", "Phone number", "Payment page",
    "Integrations", "Transactions", "Amount", "Design system", "Colours",
    "Buttons", "Inputs", "Active", "Pending", "Failed", "Your details",
    "Change password", "Order a card", "How it works", "For business",
    "Pricing", "Not accepting payments yet", "awaiting approval",
    "Create an account", "Reset password", "Repeat password", "New password",
    "Your name", "Business name", "Confirmation code", "Send code",
    "Order a card", "Contact us", "Send order", "Send message", "Pricing",
    "Notifications", "Mark all read", "Nothing new yet", "Menu",
    "Frequently asked questions", "Privacy policy", "Terms of service",
    "Delivery address", "How many cards", "Comment", "Message",
    "soum", "per card", "Order received", "Back to home", "About TapCon",
    "Bank card", "Tap your phone here", "Product", "Company", "Legal",
    # Phase 3 - integrations and payment page settings
    "Payment systems", "Switch on", "Switch off", "Not set up",
    # "Merchant ID" and "Service ID" are deliberately left in English in every
    # language: they are the exact labels the providers print in their own
    # cabinets, and a seller copying values across needs them to match.
    "Sandbox mode", "Live mode", "Secret key",
    "Leave blank to keep", "What customers see", "Preview",
    "Welcome text", "Thank-you text", "Smallest amount", "Largest amount",
    "Name shown to customers",
    # Phase 4 - the pay page, the result page
    "Choose how to pay", "Waiting for confirmation", "Payment cancelled",
    "Payment did not go through", "Nothing was charged", "Pay again",
    "Trouble paying", "Reference", "Refresh", "soum",
    "No payment method available", "Not accepting payments yet",
]

# A credential that is saved must never come back out in the HTML.
SECRET_PROBE = "sk-audit-9f3c-must-never-render"

TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def visible_text(html: str) -> str:
    html = TAG_RE.sub(" ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)


def _remove_audit_seller(User, phone):
    """Transaction.seller is PROTECT on purpose — a payment record must not
    disappear along with a seller — so clear those rows explicitly."""
    from payments.models import Transaction

    Transaction.objects.filter(seller__user__phone=phone).delete()
    User.objects.filter(phone=phone).delete()


def main():
    from accounts.services import register_seller
    from merchants.models import SellerProfile

    phone = "+998 90 555 44 33"
    from django.contrib.auth import get_user_model

    User = get_user_model()
    _remove_audit_seller(User, phone)
    user = register_seller(
        phone=phone, full_name="Audit", business_name="Audit Cafe", password="Aud1t-p@ssw0rd"
    )
    user.phone_verified = True
    user.save()
    profile = SellerProfile.objects.get(user=user)

    # Give the seller a real credential so the pages have something to leak.
    from payments.services import save_credentials

    save_credentials(profile, "payme", {"payme_id": "770011", "payme_key": SECRET_PROBE})

    # And a notification, so the bell panel renders rows rather than "nothing
    # new yet" — an empty panel would hide a broken row template.
    from core.services import notify

    notify(user, title="Audit", body="Audit notification body")

    # Approve and switch Payme on, so /pay/ renders the real amount form and
    # provider buttons rather than the "not accepting payments" card.
    from payments.services import set_enabled

    profile.approve()
    set_enabled(profile, "payme", True)

    # Octo too, so the pay page renders the card button and its own label.
    save_credentials(
        profile,
        "octo",
        {"octo_shop_id": "1", "octo_secret": "2", "octo_unique_key": SECRET_PROBE},
    )
    set_enabled(profile, "octo", True)

    from payments.models import Transaction, TransactionStatus

    waiting = Transaction.objects.create(
        seller=profile, provider="payme", amount=25000,
        status=TransactionStatus.PENDING,
    )
    settled = Transaction.objects.create(
        seller=profile, provider="payme", amount=25000,
        status=TransactionStatus.PAID,
    )

    anon_paths = [
        "/{lang}/",
        "/{lang}/how-it-works/",
        "/{lang}/for-business/",
        "/{lang}/pricing/",
        "/{lang}/about/",
        "/{lang}/faq/",
        "/{lang}/privacy/",
        "/{lang}/terms/",
        "/{lang}/order/",
        "/{lang}/order/done/",
        "/{lang}/contact/",
        "/{lang}/styleguide/",
        "/{lang}/auth/register/",
        "/{lang}/auth/login/",
        "/{lang}/auth/password-reset/",
    ]
    auth_paths = [
        "/{lang}/merchant/",
        "/{lang}/merchant/payment-page/",
        "/{lang}/merchant/integrations/",
        "/{lang}/merchant/transactions/",
        "/{lang}/merchant/settings/",
        "/{lang}/notifications/",
    ]
    unprefixed = [
        f"/pay/{profile.uid}/",
        f"/pay/{profile.uid}/result/{waiting.uid}/",
        f"/pay/{profile.uid}/result/{settled.uid}/",
    ]

    problems = 0
    for lang in ("uz", "ru", "en"):
        anon = Client()
        member = Client()
        member.force_login(user)

        checks = [(anon, p.format(lang=lang)) for p in anon_paths]
        checks += [(member, p.format(lang=lang)) for p in auth_paths]
        checks += [(anon, p, lang) for p in unprefixed]

        for check in checks:
            client, path = check[0], check[1]
            headers = {"accept-language": lang}
            response = client.get(path, headers=headers)
            status = response.status_code
            if status != 200:
                print(f"[{lang}] {path} -> HTTP {status}")
                problems += 1
                continue

            html = response.content.decode("utf-8")
            if SECRET_PROBE in html:
                print(f"[{lang}] {path} -> LEAKED a saved credential")
                problems += 1

            if lang == "en":
                continue

            text = visible_text(html)
            leaks = sorted({m for m in ENGLISH_MARKERS if m in text})
            if leaks:
                print(f"[{lang}] {path} -> untranslated: {', '.join(leaks)}")
                problems += 1

    _remove_audit_seller(User, phone)
    print("\nOK - every page renders, no English leaked, no credential leaked" if not problems
          else f"\n{problems} problem(s) found")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
