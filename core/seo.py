"""Canonical and hreflang links for the pages that want to be found.

Only the public marketing and legal pages opt in. Everything else — the pay
page, the merchant portal, anything behind a login — inherits `noindex` from
base.html and gets no canonical, which is the safer default when the thing
being indexed could be a seller's payment form.

One page in three languages is one page. hreflang is what tells a crawler
they are translations rather than three pages competing for the same words,
so a Russian speaker searching in Russian is offered the Russian one.
"""
from django.conf import settings
from django.urls import reverse
from django.utils import translation

# Uzbek is the primary language, so it is what x-default points at: a visitor
# whose language we have no page for gets the one the business speaks.
DEFAULT_LANGUAGE = "uz"


def alternates(url_name: str, args=None) -> dict:
    """Canonical + one alternate per language, for a language-prefixed page.

    Reversing under each language is what produces the right prefix; building
    the paths by string substitution would break the moment a URL is
    translated or a language is added.
    """
    args = args or []
    paths = {}
    for code, _label in settings.LANGUAGES:
        with translation.override(code):
            paths[code] = reverse(url_name, args=args)

    return {
        "canonical_path": paths[translation.get_language() or DEFAULT_LANGUAGE],
        "language_alternates": sorted(paths.items()),
        "default_alternate": paths[DEFAULT_LANGUAGE],
    }
