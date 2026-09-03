"""Which language a customer gets on the pay page, and why.

    python tools/check_pay_language.py

The pay page is the one page with no language in its URL — the address is
printed on a card, so it cannot carry /uz/. That means the language is chosen
from the phone, not the link, and a customer who taps a card must land in a
language they can read. This asks the running server what each kind of phone
would actually get.
"""
import os
import pathlib
import re
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from django.conf import settings  # noqa: E402

from merchants.models import SellerProfile  # noqa: E402

BASE = os.environ.get("CRAWL_BASE", "http://127.0.0.1:8009")

# What real phones in Uzbekistan send, plus one that speaks none of ours.
PHONES = [
    ("Uzbek phone", "uz-UZ,uz;q=0.9,ru;q=0.8", "uz"),
    ("Russian phone", "ru-RU,ru;q=0.9", "ru"),
    ("English phone", "en-US,en;q=0.9", "en"),
    ("Turkish phone", "tr-TR,tr;q=0.9", "uz"),
    ("no preference at all", None, "uz"),
]


def main():
    uid = SellerProfile.objects.filter(status="approved").first().uid
    print(f"LANGUAGE_CODE = {settings.LANGUAGE_CODE}")
    print(f"LANGUAGES = {[code for code, _ in settings.LANGUAGES]}\n")

    problems = []
    with httpx.Client(base_url=BASE, timeout=20) as client:
        for label, header, expected in PHONES:
            headers = {"Accept-Language": header} if header else {}
            response = client.get(f"/pay/{uid}/", headers=headers)
            got = (re.search(r"<html[^>]*lang=.([a-z-]+)", response.text)
                   or [None, "?"])[1]
            vary = response.headers.get("vary", "")
            mark = "ok  " if got == expected else "FAIL"
            if got != expected:
                problems.append(f"{label}: expected {expected}, got {got}")
            print(f"  {mark} {label:22} -> {got}   (Vary: {vary or 'none'})")

            # A cache in front of this must not serve one customer's language
            # to the next. Django adds Accept-Language and Cookie to Vary via
            # LocaleMiddleware; if that ever stops, every customer behind the
            # same proxy sees whichever language arrived first.
            if "accept-language" not in vary.lower():
                problems.append(f"{label}: response is not Vary: Accept-Language")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for line in problems:
            print(f"  {line}")
        return 1
    print("OK - every phone lands in a language it can read, and caches vary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
