"""Error pages, the CSS bundle, self-hosted fonts, and what crawlers see.

The thread running through all of it: things that are only noticed when they
are already wrong. A stale bundle, a 500 page that cannot render itself, a
`noindex` missing from a seller's payment page — none of these break a test
run by themselves.
"""
import pathlib
import re
from unittest.mock import patch

from django.conf import settings
from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from accounts.services import register_seller
from core.management.commands.bundlecss import (
    BUNDLE,
    SOURCES,
    css_dir,
    sources_newer_than_bundle,
)
from merchants.models import SellerProfile

PASSWORD = "s3cretpw!x"


class ErrorPageTests(TestCase):
    """A dead end still has to tell someone where to go."""

    def test_a_missing_page_is_the_branded_404(self):
        response = self.client.get("/uz/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "error-code", status_code=404)

    def test_the_404_offers_a_way_onward(self):
        """Someone who mistyped a pay link needs a next step, not a full stop."""
        response = self.client.get("/uz/no-such-page/")
        self.assertContains(response, reverse("core:home"), status_code=404)
        self.assertContains(response, reverse("core:contact"), status_code=404)

    def test_error_pages_are_translated(self):
        for language, expected in (("uz", "topilmadi"), ("ru", "не найдена")):
            with self.subTest(language=language):
                response = self.client.get(
                    f"/{language}/no-such-page/",
                    headers={"accept-language": language},
                )
                self.assertContains(
                    response, expected, status_code=404,
                )

    def test_the_500_page_renders_with_no_context_at_all(self):
        """Django renders it with an empty context and no request.

        A 500 template that reaches for `support_phone`, `user` or a database
        row would fail inside the failure it is reporting. This renders it the
        way Django will — nothing passed in — and it has to survive that.
        """
        html = render_to_string("500.html")
        self.assertIn("500", html)
        self.assertIn("<html", html)

    def test_the_500_page_needs_no_static_files(self):
        """A broken collectstatic is exactly what also causes 500s.

        Its styles are inline for that reason. If this ever starts linking a
        stylesheet, the error page can stop rendering at the worst moment.
        """
        html = render_to_string("500.html")
        self.assertNotIn("/static/", html)
        self.assertIn("<style>", html)

    def test_the_500_page_reassures_someone_mid_payment(self):
        with translation.override("en"):
            html = render_to_string("500.html")
        self.assertIn("no money has been taken", html)

    def test_the_500_page_is_not_indexed(self):
        self.assertIn('name="robots"', render_to_string("500.html"))


class CssBundleTests(TestCase):
    """One request in production, eight while developing."""

    def test_the_bundle_is_not_stale(self):
        """The failure this exists for is silent.

        Editing a stylesheet and deploying without running `bundlecss` ships
        the previous design. Nothing errors; the site is just subtly wrong.
        """
        self.assertFalse(
            sources_newer_than_bundle(),
            "static/css/bundle.css is older than one of its sources — "
            "run `python manage.py bundlecss`",
        )

    def test_the_bundle_contains_every_source(self):
        bundle = (css_dir() / BUNDLE).read_text(encoding="utf-8")
        for name in SOURCES:
            with self.subTest(source=name):
                self.assertIn(f"===== {name} =====", bundle)

    def test_tokens_come_first(self):
        """Everything else reads the variables it defines."""
        bundle = (css_dir() / BUNDLE).read_text(encoding="utf-8")
        self.assertLess(
            bundle.index("===== tokens.css ====="),
            bundle.index("===== components.css ====="),
        )

    def test_production_links_the_bundle_and_development_links_the_sources(self):
        seller = _seller()
        with override_settings(DEBUG=False):
            production = Client().get(f"/pay/{seller.uid}/").content.decode()
        with override_settings(DEBUG=True):
            development = Client().get(f"/pay/{seller.uid}/").content.decode()

        self.assertIn("css/bundle.css", production)
        self.assertNotIn("css/tokens.css", production)
        self.assertIn("css/tokens.css", development)
        self.assertNotIn("css/bundle.css", development)


class SelfHostedFontTests(TestCase):
    def test_no_page_reaches_out_to_google_fonts(self):
        """It was a render-blocking third party on the page that must be fast."""
        seller = _seller()
        for path in ("/uz/", f"/pay/{seller.uid}/", "/uz/pricing/"):
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertNotIn("fonts.googleapis.com", body)
                self.assertNotIn("fonts.gstatic.com", body)

    def test_the_latin_subset_is_preloaded(self):
        body = self.client.get("/uz/").content.decode()
        self.assertIn("inter-latin.woff2", body)
        self.assertIn('rel="preload"', body)

    def test_every_font_the_css_names_is_actually_there(self):
        """A missing woff2 is a silent fallback to the system font."""
        css = (css_dir() / "fonts.css").read_text(encoding="utf-8")
        named = re.findall(r"url\('\.\./fonts/([^']+)'\)", css)
        self.assertTrue(named, "fonts.css names no font files")
        for name in named:
            with self.subTest(font=name):
                path = pathlib.Path(settings.BASE_DIR) / "static" / "fonts" / name
                self.assertTrue(path.exists(), f"{name} is missing")

    def test_cyrillic_is_a_separate_file_so_uzbek_never_fetches_it(self):
        css = (css_dir() / "fonts.css").read_text(encoding="utf-8")
        self.assertIn("inter-cyrillic.woff2", css)
        self.assertIn("unicode-range", css)


class CrawlerTests(TestCase):
    def test_robots_txt_is_served_without_a_language_prefix(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_robots_keeps_crawlers_out_of_the_pay_pages(self):
        """A seller's payment form is not a page to be found in search."""
        body = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /pay/", body)
        self.assertIn("Disallow: /webhooks/", body)
        self.assertIn("Disallow: /uz/merchant/", body)

    def test_robots_points_at_the_sitemap(self):
        body = self.client.get("/robots.txt").content.decode()
        self.assertIn("sitemap.xml", body)

    def test_the_sitemap_lists_the_marketing_pages_in_every_language(self):
        body = self.client.get("/sitemap.xml").content.decode()
        for path in ("/uz/pricing/", "/ru/pricing/", "/en/pricing/"):
            with self.subTest(path=path):
                self.assertIn(path, body)

    def test_the_sitemap_never_lists_a_pay_page_or_the_portal(self):
        seller = _seller()
        body = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn(f"/pay/{seller.uid}/", body)
        self.assertNotIn("/merchant/", body)
        self.assertNotIn("/styleguide/", body)

    def test_the_sitemap_ties_the_translations_together(self):
        body = self.client.get("/sitemap.xml").content.decode()
        self.assertIn('hreflang="ru"', body)


class IndexingTests(TestCase):
    """What may be indexed is opt-in, and the default is no."""

    def test_a_marketing_page_asks_to_be_indexed(self):
        body = self.client.get("/uz/pricing/").content.decode()
        self.assertIn("index, follow", body)

    def test_a_marketing_page_declares_its_canonical_and_translations(self):
        body = self.client.get("/uz/pricing/").content.decode()
        self.assertIn('rel="canonical"', body)
        self.assertIn('hreflang="ru"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_the_canonical_points_at_the_language_being_viewed(self):
        body = self.client.get("/ru/pricing/").content.decode()
        canonical = re.search(r'rel="canonical" href="([^"]+)"', body).group(1)
        self.assertTrue(canonical.endswith("/ru/pricing/"), canonical)

    def test_a_sellers_pay_page_is_never_indexed(self):
        """The single most important line in this file."""
        seller = _seller()
        body = self.client.get(f"/pay/{seller.uid}/").content.decode()
        self.assertIn("noindex", body)
        self.assertNotIn("index, follow", body)

    def test_the_merchant_portal_is_never_indexed(self):
        seller = _seller()
        self.client.force_login(seller.user)
        body = self.client.get(reverse("merchants:dashboard")).content.decode()
        self.assertIn("noindex", body)

    def test_the_styleguide_is_not_indexed(self):
        """It uses the marketing layout but is ours, not a customer's."""
        body = self.client.get("/uz/styleguide/").content.decode()
        self.assertIn("noindex", body)
        self.assertNotIn("index, follow", body)

    def test_a_link_preview_has_an_image_that_exists(self):
        body = self.client.get("/uz/").content.decode()
        self.assertIn('property="og:image"', body)
        image = pathlib.Path(settings.BASE_DIR) / "static" / "img" / "og-image.png"
        self.assertTrue(image.exists(), "og-image.png is missing")


class EskizTemplateTests(TestCase):
    """The SMS texts are submitted for moderation and must not drift.

    Eskiz approves one exact string and rejects anything that does not match
    it character for character. The documented text and the sent text are
    therefore the same text, and this is what says so.
    """

    def test_the_documented_texts_match_what_the_code_sends(self):
        from accounts.services import SMS_TEMPLATES

        doc = (
            pathlib.Path(settings.BASE_DIR) / "docs" / "eskiz-templates.md"
        ).read_text(encoding="utf-8")

        for purpose, template in SMS_TEMPLATES.items():
            with self.subTest(purpose=purpose):
                sample = template % {"code": "123456"}
                self.assertIn(
                    sample,
                    doc,
                    f"docs/eskiz-templates.md does not contain the {purpose} "
                    f"message the code actually sends",
                )

    def test_the_texts_use_a_plain_apostrophe(self):
        """A curly quote is a different character and would stop matching."""
        from accounts.services import SMS_TEMPLATES

        for purpose, template in SMS_TEMPLATES.items():
            with self.subTest(purpose=purpose):
                self.assertNotIn("’", template)
                self.assertNotIn("ʻ", template)


def _seller():
    sms = patch("accounts.services.send_sms")
    sms.start()
    try:
        user = register_seller(
            phone="+998 90 123 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
    finally:
        sms.stop()
    profile = SellerProfile.objects.get(user=user)
    profile.approve()
    return profile
