from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps import views as sitemap_views
from django.urls import include, path
from django.views.generic import TemplateView

from core.sitemaps import SITEMAPS

# Language-independent URLs.
#
# /pay/<uid>/ lives here on purpose: that address is written once onto an NFC
# card and can never change, so it must not carry a language prefix. The
# customer's language comes from their cookie or Accept-Language header via
# LocaleMiddleware, and the switcher on the page sets the cookie.
#
# Webhooks likewise: providers call a fixed URL.
urlpatterns = [
    path("i18n/", include("django.conf.urls.i18n")),
    path("pay/", include("payments.urls")),
    path("webhooks/", include("payments.webhook_urls")),
    # Crawlers look for both of these at the root, without a language prefix.
    # The sitemap itself is i18n-aware and lists every language inside.
    path(
        "sitemap.xml",
        sitemap_views.sitemap,
        {"sitemaps": SITEMAPS},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path(
        "robots.txt",
        TemplateView.as_view(
            template_name="robots.txt", content_type="text/plain"
        ),
        name="robots",
    ),
]

# Everything else is language-prefixed: /uz/, /ru/, /en/
urlpatterns += i18n_patterns(
    path("admin/", admin.site.urls),
    path("auth/", include("accounts.urls")),
    path("merchant/", include("merchants.urls")),
    path("", include("core.urls")),
    prefix_default_language=True,
)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
