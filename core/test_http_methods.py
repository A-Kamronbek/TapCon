"""No URL in the project may answer a browser's GET with 405.

A 405 in an address bar is a dead end: there is no page, no message and
nothing the person can do except leave. It reached a real customer here —
a failed Octo payment rendered its error at `/pay/<uid>/go/octo/`, which
answers only POST, so the address bar kept that URL and the language switcher
posted `next=/pay/<uid>/go/octo/`. Changing language after a failed payment
returned 405.

Every POST-only endpoint is reachable by GET sooner or later — the Back
button, a reload, a link prefetcher, a bookmark — so every one of them must
answer with a redirect somewhere useful instead. This walks the whole URLconf
and holds that.
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from accounts.services import register_seller
from merchants.models import SellerProfile
from payments.models import Transaction
from payments.services import save_credentials, set_enabled

PASSWORD = "s3cretpw!x"

# Paths a browser never navigates to: they are called by a provider's server,
# not by a person, and a 405 there is the correct answer to a wrong method.
NOT_BROWSER_FACING = ("/webhooks/",)

# Admin has its own login and redirect behaviour, which is Django's, not ours.
SKIP_PREFIXES = ("/admin/", "/i18n/", "/static/", "/media/")


def _paths(resolver, prefix=""):
    """Every concrete path in the URLconf, with sample arguments filled in."""
    for entry in resolver.url_patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _paths(entry, route)
        elif isinstance(entry, URLPattern):
            yield route


class NoMethodNotAllowedTests(TestCase):
    """Every URL a browser can reach answers GET with a page or a redirect."""

    # Sample values for the capture groups in the URLconf.
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

        self.user = register_seller(
            phone="+998 90 123 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=self.user)
        self.profile.approve()
        save_credentials(
            self.profile, "payme", {"payme_id": "p1", "payme_key": "k1"}
        )
        set_enabled(self.profile, "payme", True)
        self.txn = Transaction.objects.create(
            seller=self.profile, provider="payme", amount=1000
        )

    def concrete(self, route):
        """Fill a route's placeholders with values that exist."""
        if route.startswith("^") or "(?P<" in route:
            return None  # a regex route (static/media), not a page
        if "<" not in route:
            return "/" + route

        filled = route
        for placeholder, value in (
            ("<slug:uid>", self.profile.uid),
            ("<slug:ref>", self.txn.uid),
            ("<slug:provider>", "payme"),
            ("<str:provider>", "payme"),
            ("<int:pk>", "1"),
        ):
            filled = filled.replace(placeholder, value)
        if "<" in filled:
            return None  # a shape this test does not know how to fill
        return "/" + filled

    def routes(self):
        """Concrete URLs, skipping the ones no browser navigates to.

        Filtered on the built URL rather than the route: routes come out of
        the resolver without a leading slash, so matching prefixes against
        them silently excludes nothing.
        """
        for route in _paths(get_resolver()):
            url = self.concrete(route)
            if not url:
                continue
            if url.startswith(SKIP_PREFIXES) or url.startswith(NOT_BROWSER_FACING):
                continue
            yield url

    def test_the_sweep_actually_found_urls(self):
        """A test that walks nothing passes for the wrong reason."""
        self.assertGreater(len(list(self.routes())), 20)

    def test_no_url_answers_get_with_405_anonymously(self):
        offenders = [
            url
            for url in self.routes()
            if self.client.get(url, follow=False).status_code == 405
        ]
        self.assertEqual(offenders, [])

    def test_no_url_answers_get_with_405_when_signed_in(self):
        self.client.login(phone="+998 90 123 45 67", password=PASSWORD)
        offenders = [
            url
            for url in self.routes()
            if self.client.get(url, follow=False).status_code == 405
        ]
        self.assertEqual(offenders, [])

    def test_no_url_answers_get_with_500(self):
        """A 500 is the other kind of dead end."""
        self.client.login(phone="+998 90 123 45 67", password=PASSWORD)
        offenders = [
            url
            for url in self.routes()
            if self.client.get(url, follow=False).status_code >= 500
        ]
        self.assertEqual(offenders, [])

    def test_no_url_answers_head_with_500_or_405(self):
        """Prefetchers and uptime monitors send HEAD, not GET."""
        self.client.login(phone="+998 90 123 45 67", password=PASSWORD)
        offenders = [
            (url, code)
            for url in self.routes()
            for code in [self.client.head(url, follow=False).status_code]
            if code == 405 or code >= 500
        ]
        self.assertEqual(offenders, [])

    def test_language_can_be_changed_from_every_page(self):
        """The switcher posts the current URL back as `next`.

        This is the general form of the bug a customer hit: the address bar
        held a POST-only URL, the switcher posted it as `next`, and Django
        redirected the browser there with GET. Any URL that cannot survive
        that round trip breaks the language switcher on whatever page left
        it in the address bar, so every one of them is checked, not just
        the one that was reported.
        """
        self.client.login(phone="+998 90 123 45 67", password=PASSWORD)
        offenders = []
        for url in self.routes():
            for lang in ("uz", "ru", "en"):
                response = self.client.post(
                    "/i18n/setlang/",
                    {"language": lang, "next": url},
                    follow=True,
                )
                if response.status_code != 200:
                    offenders.append((url, lang, response.status_code))
        self.assertEqual(offenders, [])
