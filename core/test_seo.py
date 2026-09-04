"""What a public page tells a search engine about itself.

Two defects this exists to prevent, both of which shipped:

* **The sitemap and the page disagreed.** `/order/` and `/contact/` were
  listed in sitemap.xml and served `noindex, nofollow`, because they are
  function views that never received the SEO context the class-based pages
  get. Google reports that pair as "Submitted URL marked noindex", and the
  two pages a buyer most needs to find were the two asking not to be found.

* **Nine of ten pages shared one description.** Title and description were
  template blocks, and a Django block cannot be rendered twice — so
  `og:title` and `og:description` fell back to a generic default on every
  page, and only the landing page had a real description at all.
"""
import re

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

# Every page that asks to be indexed. Kept explicit rather than derived from
# the URLconf: a new public page should have to be added here deliberately,
# with someone thinking about what it says to a search engine.
INDEXABLE = [
    "core:home",
    "core:how_it_works",
    "core:for_business",
    "core:pricing",
    "core:order",
    "core:faq",
    "core:about",
    "core:contact",
    "core:privacy",
    "core:terms",
]

# Public, but deliberately not indexed.
NOT_INDEXABLE = ["core:order_done", "core:styleguide"]


def tag(html, pattern):
    m = re.search(pattern, html, re.S)
    return m.group(1).strip() if m else ""


def meta(html, name):
    return tag(html, rf'<meta name="{name}" content="(.*?)"')


def prop(html, name):
    return tag(html, rf'<meta property="{name}" content="(.*?)"')


class IndexablePageMetadataTests(TestCase):
    def pages(self, lang="uz"):
        with translation.override(lang):
            for name in INDEXABLE:
                yield name, self.client.get(reverse(name)).content.decode()

    def test_every_indexable_page_says_index(self):
        for name, html in self.pages():
            with self.subTest(page=name):
                self.assertEqual(
                    meta(html, "robots"), "index, follow",
                    f"{name} is in sitemap.xml but tells crawlers not to index it",
                )

    def test_pages_that_should_not_be_indexed_are_not(self):
        with translation.override("uz"):
            for name in NOT_INDEXABLE:
                html = self.client.get(reverse(name)).content.decode()
                with self.subTest(page=name):
                    self.assertEqual(meta(html, "robots"), "noindex, nofollow")

    def test_the_sitemap_and_the_pages_agree(self):
        """Whatever the sitemap advertises must actually want to be indexed."""
        sitemap = self.client.get("/sitemap.xml").content.decode()
        listed = set(re.findall(r"<loc>https?://[^/]+(/[^<]*)</loc>", sitemap))
        for path in listed:
            html = self.client.get(path).content.decode()
            with self.subTest(path=path):
                self.assertEqual(
                    meta(html, "robots"), "index, follow",
                    f"sitemap.xml advertises {path}, which is served noindex",
                )

    def test_titles_are_unique_and_meaningful(self):
        seen = {}
        for name, html in self.pages():
            title = tag(html, r"<title>(.*?)</title>")
            with self.subTest(page=name):
                self.assertNotEqual(title, "TapCon", f"{name} has the fallback title")
                self.assertGreaterEqual(len(title), 12, f"{name}: title too short")
                self.assertLessEqual(
                    len(title), 65,
                    f"{name}: {len(title)} chars — search results truncate past ~60",
                )
                self.assertNotIn(title, seen, f"{name} shares a title with {seen.get(title)}")
            seen[title] = name

    def test_descriptions_are_unique_and_the_right_length(self):
        seen = {}
        for name, html in self.pages():
            d = meta(html, "description")
            with self.subTest(page=name):
                self.assertNotEqual(
                    d, "Bir tegish bilan to&#x27;lov qabul qiling.",
                    f"{name} still has the generic fallback description",
                )
                self.assertGreaterEqual(
                    len(d), 70,
                    f"{name}: {len(d)} chars — too thin to be worth showing",
                )
                self.assertLessEqual(
                    len(d), 320,
                    f"{name}: {len(d)} chars — will be cut off",
                )
                self.assertNotIn(
                    d, seen,
                    f"{name} shares its description with {seen.get(d)} — duplicate "
                    "descriptions are the single most common on-page SEO fault",
                )
            seen[d] = name

    def test_link_previews_are_not_all_called_TapCon(self):
        for name, html in self.pages():
            with self.subTest(page=name):
                self.assertEqual(
                    prop(html, "og:title"), tag(html, r"<title>(.*?)</title>"),
                    f"{name}: og:title and <title> disagree",
                )
                self.assertEqual(
                    prop(html, "og:description"), meta(html, "description"),
                    f"{name}: og:description and the meta description disagree",
                )

    def test_open_graph_locale_is_a_real_locale(self):
        """Bare 'uz' is ignored by Telegram and Facebook; uz_UZ is not."""
        for lang, expected in (("uz", "uz_UZ"), ("ru", "ru_RU"), ("en", "en_US")):
            with translation.override(lang):
                html = self.client.get(reverse("core:home")).content.decode()
            with self.subTest(lang=lang):
                self.assertEqual(prop(html, "og:locale"), expected)
                self.assertEqual(len(re.findall(r'og:locale:alternate', html)), 2)

    def test_canonical_and_hreflang_are_absolute_https(self):
        for name, html in self.pages():
            canonical = tag(html, r'<link rel="canonical" href="(.*?)"')
            with self.subTest(page=name):
                self.assertTrue(
                    canonical.startswith("http"),
                    f"{name}: canonical is not an absolute URL",
                )

    def test_every_language_has_its_own_metadata(self):
        """A Russian page with an Uzbek description is worse than none."""
        titles = set()
        for lang in ("uz", "ru", "en"):
            with translation.override(lang):
                html = self.client.get(reverse("core:pricing")).content.decode()
            titles.add(tag(html, r"<title>(.*?)</title>"))
        self.assertEqual(len(titles), 3, f"pricing is not translated: {titles}")


class StructuredDataTests(TestCase):
    def test_indexable_pages_carry_json_ld(self):
        with translation.override("uz"):
            html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn("application/ld+json", html)
        self.assertIn('"@type": "Organization"', html)

    def test_json_ld_is_valid_json(self):
        """A trailing comma or an unescaped quote makes the whole block junk."""
        import json

        with translation.override("uz"):
            html = self.client.get(reverse("core:home")).content.decode()
        block = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(block, "no JSON-LD block found")
        data = json.loads(block.group(1))
        self.assertEqual(data["@context"], "https://schema.org")
        types = [node["@type"] for node in data["@graph"]]
        self.assertIn("Organization", types)
        self.assertIn("WebSite", types)
        self.assertIn("WebPage", types)

    def test_a_seller_page_describes_nothing_to_anyone(self):
        """The pay page and the portal must not emit structured data."""
        with translation.override("uz"):
            html = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn("application/ld+json", html)
