"""What search engines are told exists.

Only the public marketing and legal pages. Deliberately absent:

* **`/pay/<uid>/`** — a seller's pay page is not a public web page, it is the
  address on their card. Indexing it would put every seller's payment form in
  search results, and the uid is meant to be unguessable rather than
  advertised.
* **the merchant portal** — behind a login, and nothing there is public.
* **`/styleguide/`** — ours, not a customer's.
* **order/done** — reachable only after ordering.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class MarketingSitemap(Sitemap):
    """The public pages, in each language.

    `i18n = True` makes Django emit one URL per language with the right
    prefix, plus `alternate` links tying them together, so a Russian speaker
    searching in Russian is offered the Russian page rather than the Uzbek one.
    """

    protocol = "https"
    i18n = True
    alternates = True

    # Changed by hand, rarely. `changefreq` and `priority` are hints a crawler
    # is free to ignore; the honest values are low and monthly.
    changefreq = "monthly"

    PRIORITIES = {
        "core:home": 1.0,
        "core:how_it_works": 0.8,
        "core:for_business": 0.8,
        "core:pricing": 0.8,
        "core:order": 0.7,
        "core:faq": 0.6,
        "core:about": 0.5,
        "core:contact": 0.5,
        "core:privacy": 0.3,
        "core:terms": 0.3,
    }

    def items(self):
        return list(self.PRIORITIES)

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.PRIORITIES[item]


SITEMAPS = {"marketing": MarketingSitemap}
