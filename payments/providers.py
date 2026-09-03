"""Provider registry.

Adding a provider should be config, not code: this dict drives the
integrations form, the pay-page buttons and the per-seller gateway
construction. Field names match the tolov gateway constructor kwargs
exactly, so a seller's stored credentials can be splatted straight in:

    gateway = PaymeGateway(**integration.credentials, is_test_mode=...)

Verified against tolov 2.2.0 — every gateway accepts credentials in its
constructor, so nothing needs to live in settings.TOLOV.
"""
from django.utils.translation import gettext_lazy as _

# secret=True fields are masked in the UI and never rendered back to HTML.
PROVIDERS = {
    "payme": {
        "logo": "payme.png",
        "label": "Payme",
        "gateway": "tolov.PaymeGateway",
        "fields": [
            {"name": "payme_id", "label": _("Merchant ID"), "secret": False},
            {"name": "payme_key", "label": _("Key"), "secret": True},
        ],
    },
    "click": {
        "logo": "click.png",
        "label": "Click",
        "gateway": "tolov.ClickGateway",
        "fields": [
            {"name": "service_id", "label": _("Service ID"), "secret": False},
            {"name": "merchant_id", "label": _("Merchant ID"), "secret": False},
            {"name": "merchant_user_id", "label": _("Merchant user ID"), "secret": False},
            {"name": "secret_key", "label": _("Secret key"), "secret": True},
        ],
    },
    # Uzum and Paynet authenticate their *webhooks* with a username and
    # password, which their gateway constructors do not take. Both are
    # collected here and handed to the webhook handler; `gateway_fields`
    # names the subset that goes to the constructor.
    "uzum": {
        "logo": "uzum.png",
        "label": "Uzum",
        "gateway": "tolov.UzumGateway",
        "gateway_fields": ["service_id"],
        "fields": [
            {"name": "service_id", "label": _("Service ID"), "secret": False},
            {"name": "username", "label": _("Webhook username"), "secret": False},
            {"name": "password", "label": _("Webhook password"), "secret": True},
        ],
    },
    "paynet": {
        "logo": "paynet.png",
        "label": "Paynet",
        "gateway": "tolov.gateways.paynet.client.PaynetGateway",
        "gateway_fields": ["merchant_id"],
        # Paynet is the one gateway that does not convert for us: it builds
        # `app.paynet.uz/?a=<amount>` and that parameter is tiyin. Passing
        # soum would ask the customer for a hundredth of the bill.
        "amount_unit": "tiyin",
        "fields": [
            {"name": "merchant_id", "label": _("Merchant ID"), "secret": False},
            {"name": "service_id", "label": _("Service ID"), "secret": False},
            {"name": "username", "label": _("Webhook username"), "secret": False},
            {"name": "password", "label": _("Webhook password"), "secret": True},
        ],
    },
    # Octo is the card-payment route (Phase 5). A seller knows it as "Octo",
    # the name on their contract — but a customer paying at a counter has
    # never heard of it and is looking for the button that takes a bank card.
    # So it carries a second, customer-facing name and mark.
    "octo": {
        "logo": "",
        "label": "Octo",
        "customer_label": _("Bank card"),
        "customer_logo": "card.png",
        # Both aggregators are "pay with a card" to a customer, and offering
        # two buttons that say the same thing is a choice nobody can make.
        # Registry order decides which one is shown: Octo is first, so a
        # seller who has both is offered through Octo.
        "card_route": True,
        "gateway": "tolov.OctoGateway",
        # Octo is told where to send its callback at construction time, and
        # that URL carries the seller — so build_gateway() supplies it.
        "needs_notify_url": True,
        # Octo's form is rendered in whichever of uz/ru/en we ask for, and it
        # is where the customer types their card number — it has to match the
        # language they were reading a moment ago. It also prints the
        # description on the form and on the statement.
        "sends_language": True,
        "sends_description": True,
        "gateway_fields": ["octo_shop_id", "octo_secret"],
        "fields": [
            # Octo's API types this as an integer and compares it as one.
            {"name": "octo_shop_id", "label": _("Shop ID"), "secret": False,
             "numeric": True},
            {"name": "octo_secret", "label": _("Secret"), "secret": True},
            # Octo signs its callbacks sha1(unique_key + uuid + status). It is
            # a different key from the secret, it comes from the Octo team,
            # and without it a live callback cannot be verified at all.
            {"name": "octo_unique_key", "label": _("Callback key"), "secret": True},
        ],
    },
    # The standby card route. A seller knows the name from their contract; a
    # customer has never heard it and is looking for the button that takes a
    # bank card — the same button Octo provides, which is why only one of the
    # two is ever offered.
    "multicard": {
        "logo": "",
        "label": "Multicard",
        "customer_label": _("Bank card"),
        "customer_logo": "card.png",
        "card_route": True,
        "gateway": "tolov.MulticardGateway",
        # Multicard is told where to call back per invoice — unlike Payme or
        # Click there is no cabinet field to register it in once. Without this
        # its callback goes nowhere and every payment it takes sits `pending`
        # for ever while the customer's money has moved.
        "needs_callback_url": True,
        "fields": [
            {"name": "application_id", "label": _("Application ID"), "secret": False},
            {"name": "secret", "label": _("Secret"), "secret": True},
            {"name": "store_id", "label": _("Store ID"), "secret": False},
        ],
    },
}

PROVIDER_CHOICES = [(key, cfg["label"]) for key, cfg in PROVIDERS.items()]
