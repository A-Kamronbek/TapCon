"""Walk the whole site over real HTTP, the way a browser would.

    python manage.py runserver 127.0.0.1:8009 --insecure --settings=config.settings.preview
    python tools/live_crawl.py

The test client is not a browser: it never fetches a stylesheet, never
follows an <img src>, and never sees the response DEBUG=False produces. This
does. For every page, in every language, signed in and signed out, it checks
the status, then fetches every asset the page references and checks those
too — a 404 on a stylesheet is invisible in a passing test suite and very
visible to a customer.

Untranslated text is deliberately NOT checked here. Uzbek is written in the
Latin alphabet, so "looks like English" cannot tell an Uzbek sentence from an
English one; tools/audit_pages.py does that job against the message
catalogue, which can.
"""
import os
import pathlib
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402
from django.urls.resolvers import LocalePrefixPattern  # noqa: E402

BASE = os.environ.get("CRAWL_BASE", "http://127.0.0.1:8009")
LANGUAGES = ("uz", "ru", "en")

# Called by a provider's server, not by a person.
SKIP = ("/webhooks/", "/admin/", "/static/", "/media/", "/i18n/")


class Assets(HTMLParser):
    """Every URL the browser will fetch to finish rendering this page."""

    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "stylesheet" in (attrs.get("rel") or ""):
            self.urls.append(attrs.get("href"))
        elif tag in ("img", "script") and attrs.get("src"):
            self.urls.append(attrs["src"])


def routes():
    """Concrete browser-facing paths, each tagged with whether it is localised.

    The pay page lives outside i18n_patterns on purpose — the URL is printed
    on a card, so it has no language prefix. Prefixing it here would report
    a 404 that is the correct answer.
    """
    from merchants.models import SellerProfile
    from payments.models import Transaction

    profile = SellerProfile.objects.filter(status="approved").first()
    txn = Transaction.objects.filter(seller=profile).first()

    def walk(resolver, prefix="", localised=False):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                # A LocalePrefixPattern stringifies to the *active* language
                # ("uz/"). Keeping it would bake one language into the route
                # and then prefix a second one on top: "/ru/uz/terms/".
                if isinstance(entry.pattern, LocalePrefixPattern):
                    yield from walk(entry, prefix, True)
                else:
                    yield from walk(entry, prefix + str(entry.pattern), localised)
            elif isinstance(entry, URLPattern):
                yield prefix + str(entry.pattern), localised

    out = set()
    for route, localised in walk(get_resolver()):
        if route.startswith("^") or "(?P<" in route:
            continue
        filled = route
        for placeholder, value in (
            ("<slug:uid>", profile.uid),
            ("<slug:ref>", txn.uid if txn else "x"),
            ("<slug:provider>", "payme"),
            ("<str:provider>", "payme"),
            ("<int:pk>", "1"),
        ):
            filled = filled.replace(placeholder, value)
        if "<" in filled:
            continue
        url = "/" + filled
        if not url.startswith(SKIP):
            out.add((url, localised))
    return sorted(out)


def sign_in(client):
    page = client.get("/uz/auth/login/")
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    response = client.post(
        "/uz/auth/login/",
        data={
            "csrfmiddlewaretoken": token.group(1),
            "phone": os.environ["CRAWL_PHONE"],
            "password": os.environ["CRAWL_PASSWORD"],
        },
        headers={"Referer": f"{BASE}/uz/auth/login/"},
    )
    return "/merchant/" in str(response.url)


def main():
    problems = []
    assets_seen = {}
    pages = 0

    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=30) as client:
        try:
            client.get("/uz/")
        except httpx.ConnectError:
            print(f"No server at {BASE} - start runserver first.")
            return 2

        entries = routes()
        localised = sum(1 for _, flag in entries if flag)
        print(f"{len(entries)} routes ({localised} localised, "
              f"{len(entries) - localised} not), signed out then signed in")

        for signed_in in (False, True):
            if signed_in and not sign_in(client):
                print("  could not sign in")
                return 2
            who = "in" if signed_in else "out"

            for path, is_localised in entries:
                langs = LANGUAGES if is_localised else ("-",)
                for lang in langs:
                    url = f"/{lang}{path}" if is_localised else path
                    for method in ("GET", "HEAD"):
                        response = client.request(method, url)
                        if response.status_code >= 400:
                            problems.append(
                                f"{method} {url} [{who}] -> {response.status_code}")
                    pages += 1

                    response = client.get(url)
                    if response.status_code >= 400:
                        continue
                    parser = Assets()
                    parser.feed(response.text)
                    for asset in parser.urls:
                        if not asset or asset.startswith(("data:", "http")):
                            continue
                        full = urljoin(str(response.url), asset)
                        key = urlparse(full).path
                        if key in assets_seen:
                            continue
                        code = client.get(full).status_code
                        assets_seen[key] = code
                        if code >= 400:
                            problems.append(f"asset {key} -> {code} (on {url})")

        # The pages a customer reaches by mistake.
        for url, expected in (
            ("/uz/no-such-page/", 404),
            ("/pay/zzzzzz/", 404),
            ("/robots.txt", 200),
            ("/sitemap.xml", 200),
        ):
            code = client.get(url).status_code
            if code != expected:
                problems.append(f"GET {url} -> {code}, expected {expected}")

    print(f"{pages} page requests, {len(assets_seen)} distinct assets fetched")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for line in problems:
            print(f"  {line}")
        return 1
    print("\nOK - every page and every asset answers, in every language")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
