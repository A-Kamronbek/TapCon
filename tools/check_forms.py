"""Submit junk to every form and check the rejection is usable.

A form that 500s, silently accepts nothing, or answers in English is a defect.
This posts empty and invalid data to every form in the product, in every
language, and reports anything that does not come back as a translated,
visible error.

    python tools/check_forms.py
"""
import os
import pathlib
import re
import sys

import django

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.core.cache import cache  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

# This tool submits every form in the product several times over, which is by
# design more than a person would — and therefore more than the rate limits
# allow. Those limits have their own tests in core/test_throttle.py; being
# refused here would only hide the form errors this is actually checking.
cache.clear()

LATIN = re.compile(r"[A-Za-z]")
# Words that would only appear if a Django or form message escaped translation.
ENGLISH = [
    "This field is required", "Enter a valid", "Ensure this value",
    "required.", "Please correct the error", "Select a valid choice",
    "This password is too", "The two password fields",
]


def errors_in(html):
    """Both shapes an error can take: beside a field, or above the form."""
    return (
        re.findall(r'class="field-error"[^>]*>(.*?)<', html, re.S)
        + re.findall(r'class="toast toast-error"[^>]*>(.*?)</div>', html, re.S)
    )


def probe(client, path, data, lang, label, expect_errors=True, expected_status=200):
    problems = []
    # A browser always posts the CSRF token, so request.POST is never empty.
    # Send one, or this probe tests a situation that cannot happen.
    client.get(path.rsplit("go/", 1)[0], headers={"accept-language": lang})
    data = dict(data)
    data.setdefault("csrfmiddlewaretoken", client.cookies.get("csrftoken", ""))
    # Follow redirects: what matters is the page the person ends up looking
    # at, not the status of the POST. The pay form redirects on failure —
    # rendering the error at /pay/<uid>/go/<provider>/ left a POST-only URL in
    # the address bar, and changing language from there returned 405.
    response = client.post(
        path, data, headers={"accept-language": lang}, follow=True
    )

    if response.status_code >= 500:
        return [f"HTTP {response.status_code}"]

    if not expect_errors:
        return problems

    if response.status_code != expected_status:
        return [
            f"answered HTTP {response.status_code}, expected {expected_status}"
        ]

    body = response.content.decode("utf-8")
    found = errors_in(body)
    if not found:
        problems.append("rejected the input but showed no error text")

    if lang != "en":
        for marker in ENGLISH:
            if marker in body:
                problems.append(f"untranslated: {marker!r}")
    return problems


def main():
    from django.contrib.auth import get_user_model

    from accounts.services import register_seller
    from merchants.models import SellerProfile

    phone = "+998 90 555 11 00"
    User = get_user_model()
    User.objects.filter(phone=phone).delete()
    user = register_seller(
        phone=phone, full_name="Forms", business_name="Forms Cafe",
        password="F0rms-p@ssw0rd",
    )
    user.phone_verified = True
    user.save()
    profile = SellerProfile.objects.get(user=user)

    # A live seller, so the pay page shows a real amount form to reject.
    from payments.services import save_credentials, set_enabled

    profile.approve()
    save_credentials(profile, "payme", {"payme_id": "1", "payme_key": "2"})
    set_enabled(profile, "payme", True)

    anon_cases = [
        ("register", "/{lang}/auth/register/", {}),
        ("register bad phone", "/{lang}/auth/register/",
         {"full_name": "A", "business_name": "B", "phone": "123",
          "password": "Str0ng-pass!x", "password_confirm": "Str0ng-pass!x"}),
        ("register weak password", "/{lang}/auth/register/",
         {"full_name": "A", "business_name": "B", "phone": "+998 90 111 22 33",
          "password": "12345", "password_confirm": "12345"}),
        ("login", "/{lang}/auth/login/", {"phone": "+998 90 000 11 22",
                                          "password": "nope"}),
        ("password reset", "/{lang}/auth/password-reset/", {"phone": "123"}),
        ("order", "/{lang}/order/", {}),
        ("order bad quantity", "/{lang}/order/",
         {"full_name": "A", "phone": "+998 90 111 22 33", "address": "X",
          "quantity": "0", "comment": ""}),
        ("contact", "/{lang}/contact/", {}),
    ]
    # The pay page: not language-prefixed, and the amount is checked against
    # this seller's own limits.
    pay_cases = [
        ("pay: nothing typed", f"/pay/{profile.uid}/go/payme/", {}),
        ("pay: below the minimum", f"/pay/{profile.uid}/go/payme/", {"amount": "1"}),
        ("pay: above the maximum",
         f"/pay/{profile.uid}/go/payme/", {"amount": "99999999999"}),
        ("pay: not a number", f"/pay/{profile.uid}/go/payme/", {"amount": "abc"}),
    ]
    member_cases = [
        ("profile", "/{lang}/merchant/settings/",
         {"form": "profile", "full_name": "", "phone": ""}),
        ("business", "/{lang}/merchant/settings/",
         {"form": "business", "business_name": ""}),
        ("password change", "/{lang}/merchant/settings/",
         {"form": "password", "old_password": "wrong",
          "new_password1": "abc", "new_password2": "xyz"}),
        ("pay page", "/{lang}/merchant/payment-page/",
         {"business_name": "", "welcome_text": "", "thank_you_text": "",
          "min_amount": "-5", "max_amount": "1"}),
        ("integrations half filled", "/{lang}/merchant/integrations/",
         {"provider": "click", "action": "save", "service_id": "1",
          "merchant_id": "", "merchant_user_id": "", "secret_key": ""}),
    ]

    total = 0
    for lang in ("uz", "ru", "en"):
        guest, seller = Client(), Client()
        seller.force_login(user)

        for label, path, data in anon_cases:
            for problem in probe(guest, path.format(lang=lang), data, lang, label):
                print(f"[{lang}] {label} -> {problem}")
                total += 1

        for label, path, data in member_cases:
            for problem in probe(seller, path.format(lang=lang), data, lang, label):
                print(f"[{lang}] {label} -> {problem}")
                total += 1

        # A rejected payment redirects back to the pay page and shows the
        # error there, so the probe follows it like a browser would.
        for label, path, data in pay_cases:
            for problem in probe(guest, path, data, lang, label):
                print(f"[{lang}] {label} -> {problem}")
                total += 1

    from payments.models import Transaction

    Transaction.objects.filter(seller=profile).delete()
    User.objects.filter(phone=phone).delete()
    print("\nOK - every form rejects junk visibly and in the right language"
          if not total else f"\n{total} problem(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
