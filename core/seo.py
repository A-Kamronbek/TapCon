"""What a page tells a search engine and a link preview about itself.

Only the public marketing and legal pages opt in. Everything else — the pay
page, the merchant portal, anything behind a login — inherits `noindex` from
base.html and gets no canonical, which is the safer default when the thing
being indexed could be a seller's payment form.

One page in three languages is one page. hreflang is what tells a crawler
they are translations rather than three pages competing for the same words,
so a Russian speaker searching in Russian is offered the Russian one.

**Title and description live here, not in the templates.** They are used in
four places each — `<title>`, `<meta name=description>`, `og:title`,
`og:description` — and a Django block cannot be rendered twice, so keeping
them in the template guarantees either duplication or (as happened here)
three of the four falling back to a generic default. Nine of ten pages
shipped with the same 37-character description and every shared link said
"TapCon".
"""
from django.conf import settings
from django.urls import reverse
from django.utils import translation

# Uzbek is the primary language, so it is what x-default points at: a visitor
# whose language we have no page for gets the one the business speaks.
DEFAULT_LANGUAGE = "uz"

# Open Graph wants language_TERRITORY, not a bare language code. Facebook and
# Telegram ignore a malformed one, which costs the localised preview.
OG_LOCALES = {"uz": "uz_UZ", "ru": "ru_RU", "en": "en_US"}


def og_locale(code: str) -> str:
    return OG_LOCALES.get(code, "uz_UZ")


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


def page_meta(url_name, title, description, *, nav="", indexable=True) -> dict:
    """Everything the <head> needs for one public page.

    `title` is the whole thing, including the brand — a page is free to lead
    with its own words rather than always trailing "— TapCon", and the
    landing page does.
    """
    context = {
        "meta_title": title,
        "meta_description": description,
        "nav": nav,
    }
    if indexable and url_name:
        context["indexable"] = True
        context.update(alternates(url_name))
    return context
