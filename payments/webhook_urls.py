"""Provider webhook endpoints.

Deliberately outside i18n_patterns: providers call a fixed URL with no
language prefix. The shape is /webhooks/<provider>/<seller_uid>/ so that each
request resolves that seller's own credentials — see webhooks.py.

Uzum is the exception and needs the extra segment. Its Biller API does not put
the operation in the body; it calls a different path for each one
(.../check, .../create, .../confirm, .../reverse, .../status), and tolov's
handler reads that as a view argument:

    def post(self, request, action, **_):

Without a route that supplies it, every Uzum callback raised TypeError before
reaching any of our code and answered 500 — which a provider reads as "retry",
so a payment that had actually been taken would never have been recorded. The
second pattern is what carries it.
"""
from django.urls import path

from . import webhooks

app_name = "webhooks"

urlpatterns = [
    path("<slug:provider>/<slug:uid>/", webhooks.webhook_dispatch, name="provider"),
    path(
        "<slug:provider>/<slug:uid>/<slug:action>/",
        webhooks.webhook_dispatch,
        name="provider_action",
    ),
]
