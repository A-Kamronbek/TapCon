"""Structural checks on every rendered page.

Both bugs Kamronbek reported were of one kind: markup that looks right but
cannot do what it appears to offer. So this crawls the real HTML and looks for
that class of problem rather than for wording.

    python tools/check_markup.py

Uses Django's test client, so it needs no running server.
"""
import os
import pathlib
import re
import sys
from collections import Counter
from html.parser import HTMLParser

import django

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# A <button> may only contain phrasing content. Browsers tolerate a <div>
# inside one, but it is invalid and layout behaves oddly.
NOT_PHRASING = {"div", "p", "ul", "ol", "li", "table", "section", "form", "h1",
                "h2", "h3", "h4", "h5", "h6"}


class PageCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.ids = Counter()
        self.stack = []
        self.labels_for = []
        self.form_depth = 0
        self.button_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)

        if a.get("id"):
            self.ids[a["id"]] += 1

        if tag == "form":
            if self.form_depth:
                self.problems.append("a <form> inside another <form>")
            self.form_depth += 1

        if tag == "button":
            self.button_depth += 1
            # A button with no type submits its form. Inside a form that is
            # rarely what a non-submit button meant to do.
            if self.form_depth and not a.get("type"):
                self.problems.append("<button> without type= inside a form")
        elif self.button_depth and tag in NOT_PHRASING:
            self.problems.append(f"<{tag}> inside a <button>")

        if tag == "label" and a.get("for"):
            self.labels_for.append(a["for"])

        if tag == "img" and "alt" not in a:
            self.problems.append(f"<img> without alt: {a.get('src', '?')}")

        if tag == "a":
            href = a.get("href", "")
            if not href or href == "#":
                self.problems.append("<a> with no destination")
            if a.get("target") == "_blank" and "noopener" not in a.get("rel", ""):
                self.problems.append(f"target=_blank without rel=noopener: {href}")

        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == "form":
            self.form_depth = max(0, self.form_depth - 1)
        if tag == "button":
            self.button_depth = max(0, self.button_depth - 1)
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def finish(self):
        for name, count in self.ids.items():
            if count > 1:
                self.problems.append(f"id={name!r} used {count} times")
        for target in self.labels_for:
            if target not in self.ids:
                self.problems.append(f"<label for={target!r}> points at nothing")
        if self.stack:
            self.problems.append(f"unclosed: {', '.join(self.stack[-5:])}")
        return self.problems


def check(html):
    parser = PageCheck()
    # Skip the parts of the page that are not markup.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    parser.feed(html)
    return parser.finish()


VAR_USE = re.compile(r"var\(\s*(--[\w-]+)\s*(,)?")
VAR_DEF = re.compile(r"^\s*(--[\w-]+)\s*:", re.M)


def check_css(base_dir):
    """Every var(--x) without a fallback must be defined somewhere.

    An undefined custom property does not error — the declaration is simply
    dropped, so the element silently keeps whatever it inherited. That is how
    a colour or a font size goes missing without anything looking broken,
    which makes it exactly the sort of thing worth checking mechanically.
    """
    css_dir = base_dir / "static" / "css"
    files = sorted(css_dir.glob("*.css"))

    defined = set()
    for path in files:
        defined.update(VAR_DEF.findall(path.read_text(encoding="utf-8")))

    problems = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name, has_fallback in VAR_USE.findall(text):
            if not has_fallback and name not in defined:
                problems.append(f"{path.name}: var({name}) is never defined")
    return sorted(set(problems))


def main():
    from django.contrib.auth import get_user_model

    from accounts.services import register_seller
    from core.services import notify
    from merchants.models import SellerProfile
    from payments.services import save_credentials

    phone = "+998 90 555 33 22"
    User = get_user_model()
    from payments.models import Transaction as _Txn

    _Txn.objects.filter(seller__user__phone=phone).delete()
    User.objects.filter(phone=phone).delete()
    user = register_seller(
        phone=phone, full_name="Markup", business_name="Markup Cafe",
        password="M@rkup-p@ss1",
    )
    user.phone_verified = True
    user.save()
    profile = SellerProfile.objects.get(user=user)
    save_credentials(profile, "payme", {"payme_id": "1", "payme_key": "2"})
    notify(user, title="Markup", body="Row must render")

    # A live seller, so the pay page renders its form and provider buttons.
    from payments.models import Transaction, TransactionStatus
    from payments.services import set_enabled

    profile.approve()
    set_enabled(profile, "payme", True)
    save_credentials(
        profile, "octo",
        {"octo_shop_id": "1", "octo_secret": "2", "octo_unique_key": "3"},
    )
    set_enabled(profile, "octo", True)
    waiting = Transaction.objects.create(
        seller=profile, provider="payme", amount=25000,
        status=TransactionStatus.PENDING,
    )
    settled = Transaction.objects.create(
        seller=profile, provider="payme", amount=25000,
        status=TransactionStatus.PAID,
    )

    anon = [
        "/{lang}/", "/{lang}/how-it-works/", "/{lang}/for-business/",
        "/{lang}/pricing/", "/{lang}/about/", "/{lang}/faq/", "/{lang}/privacy/",
        "/{lang}/terms/", "/{lang}/order/", "/{lang}/order/done/",
        "/{lang}/contact/", "/{lang}/styleguide/", "/{lang}/auth/register/",
        "/{lang}/auth/login/", "/{lang}/auth/password-reset/",
    ]
    member = [
        "/{lang}/merchant/", "/{lang}/merchant/payment-page/",
        "/{lang}/merchant/integrations/", "/{lang}/merchant/transactions/",
        "/{lang}/merchant/settings/", "/{lang}/notifications/",
    ]

    from django.conf import settings as django_settings

    total = 0
    for problem in check_css(django_settings.BASE_DIR):
        print(problem)
        total += 1

    for lang in ("uz", "ru", "en"):
        guest, seller = Client(), Client()
        seller.force_login(user)
        jobs = [(guest, p.format(lang=lang)) for p in anon]
        jobs += [(seller, p.format(lang=lang)) for p in member]
        jobs += [
            (guest, f"/pay/{profile.uid}/"),
            (guest, f"/pay/{profile.uid}/result/{waiting.uid}/"),
            (guest, f"/pay/{profile.uid}/result/{settled.uid}/"),
        ]

        for client, path in jobs:
            response = client.get(path, headers={"accept-language": lang})
            if response.status_code != 200:
                print(f"[{lang}] {path} -> HTTP {response.status_code}")
                total += 1
                continue
            for problem in check(response.content.decode("utf-8")):
                print(f"[{lang}] {path} -> {problem}")
                total += 1

    # Transaction.seller is PROTECT, so its rows go first.
    Transaction.objects.filter(seller=profile).delete()
    User.objects.filter(phone=phone).delete()
    print("\nOK - no structural problems" if not total else f"\n{total} problem(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
