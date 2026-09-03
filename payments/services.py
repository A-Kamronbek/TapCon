"""Payment business logic.

Everything that touches provider credentials, builds per-seller tolov
gateways or applies webhook results lives here, not in views, so the planned
v2 DRF API can reuse it.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.module_loading import import_string
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from .models import (
    InvalidTransition,
    ProviderIntegration,
    Transaction,
    TransactionStatus,
)
from .providers import PROVIDERS

logger = logging.getLogger("payments")


class IntegrationError(Exception):
    """The seller asked for something the provider setup cannot do."""


def integrations_for(seller) -> dict:
    """Every provider in the registry, with the seller's row if one exists.

    Returns provider key -> ProviderIntegration (unsaved when never touched),
    so the page can render all six rows whether or not they are configured.
    """
    existing = {i.provider: i for i in seller.integrations.all()}
    result = {}
    for key in PROVIDERS:
        result[key] = existing.get(key) or ProviderIntegration(
            seller=seller, provider=key
        )
    return result


def connected_count(seller) -> tuple:
    """(live providers, total providers) for the summary card."""
    live = sum(1 for i in seller.integrations.all() if i.is_live)
    return live, len(PROVIDERS)


@transaction.atomic
def save_credentials(seller, provider: str, credentials: dict) -> ProviderIntegration:
    if provider not in PROVIDERS:
        raise IntegrationError(_("Unknown payment provider."))

    integration, _created = ProviderIntegration.objects.get_or_create(
        seller=seller, provider=provider
    )
    integration.credentials = credentials

    # A provider that lost a required field must not stay switched on.
    if integration.is_enabled and not integration.is_complete:
        integration.is_enabled = False

    integration.save()
    # Never log the values themselves, only that something changed.
    logger.info("Seller %s updated %s credentials", seller.uid, provider)
    return integration


@transaction.atomic
def set_enabled(seller, provider: str, enabled: bool) -> ProviderIntegration:
    if provider not in PROVIDERS:
        raise IntegrationError(_("Unknown payment provider."))

    integration, _created = ProviderIntegration.objects.get_or_create(
        seller=seller, provider=provider
    )

    if enabled and not integration.is_complete:
        raise IntegrationError(
            _("Fill in every field for this provider before switching it on.")
        )

    integration.is_enabled = enabled
    integration.save(update_fields=["is_enabled", "updated_at"])
    return integration


@transaction.atomic
def set_test_mode(seller, provider: str, test_mode: bool) -> ProviderIntegration:
    if provider not in PROVIDERS:
        raise IntegrationError(_("Unknown payment provider."))

    integration, _created = ProviderIntegration.objects.get_or_create(
        seller=seller, provider=provider
    )
    integration.is_test_mode = test_mode
    integration.save(update_fields=["is_test_mode", "updated_at"])
    return integration


def enabled_providers(seller) -> list:
    """What the pay page offers this customer, in registry order.

    Registry order rather than whatever order the seller happened to connect
    them in: the buttons a customer taps at a counter should sit in the same
    place for every business.

    At most one card route is offered. Octo and Multicard are two names for
    the same thing to a customer — both say "Bank card" — and two buttons that
    say the same thing is a choice nobody can make. Whichever comes first in
    the registry wins, which is Octo.
    """
    if not seller.is_live:
        return []

    live = {i.provider: i for i in seller.integrations.all() if i.is_live}

    offered = []
    card_taken = False
    for key, config in PROVIDERS.items():
        integration = live.get(key)
        if integration is None:
            continue
        if config.get("card_route"):
            if card_taken:
                continue
            card_taken = True
        offered.append(integration)
    return offered


# --- taking a payment ----------------------------------------------------


class PaymentError(Exception):
    """The payment could not be started. Safe to show to a customer."""


def webhook_url(seller, provider: str) -> str:
    """Where this seller's callbacks for this provider arrive.

    Built from SITE_URL rather than the incoming request: the address a
    provider is told to call has to be the stable public one, not whichever
    host a customer happened to reach us on.
    """
    path = reverse("webhooks:provider", args=[provider, seller.uid])
    return f"{settings.SITE_URL}{path}"


def build_gateway(integration):
    """A tolov client carrying this seller's credentials and nothing else.

    Constructed per request. There is deliberately no cache: a seller can
    change their keys or flip sandbox mode between two payments, and a cached
    client would keep charging through the old ones.
    """
    gateway_class = import_string(integration.config["gateway"])
    kwargs = integration.gateway_kwargs()

    # Octo is told at construction time where to send its callback, and that
    # differs per seller. The others carry the seller in the URL registered
    # in their cabinet instead.
    #
    # Declared in the registry rather than sniffed from the constructor
    # signature: several tolov gateways take **kwargs, so introspection would
    # quietly decide they do not want a notify_url and Octo's callbacks would
    # go nowhere.
    if integration.config.get("needs_notify_url"):
        kwargs["notify_url"] = webhook_url(integration.seller, integration.provider)

    return gateway_class(**kwargs)


# Payme is told which key of its `account` object carries our id. It must
# match settings.TOLOV["PAYME"]["ACCOUNT_FIELD"], or its webhook will look up
# the wrong thing and every payment will fail at CheckPerformTransaction.
_CHECKOUT_EXTRA = {
    "payme": {"account_field_name": settings.TOLOV["PAYME"]["ACCOUNT_FIELD"]},
}

# Octo renders its card form in one of three languages, and defaults to Uzbek.
# A customer reading the pay page in Russian must not be handed an Uzbek form
# to type their card number into.
_OCTO_LANGUAGES = {"uz", "ru", "en"}


def checkout_amount(provider: str, amount: Decimal):
    """The amount in the unit this provider's checkout call expects.

    Almost every tolov gateway takes soum and converts internally. Paynet does
    not: it builds `app.paynet.uz/?a=<amount>` and that parameter is **tiyin**,
    so passing soum straight through would ask the customer for one hundredth
    of what the seller charged. The unit is declared in the registry rather
    than remembered here.
    """
    if PROVIDERS[provider].get("amount_unit") == "tiyin":
        return int(amount * 100)
    return amount


def checkout_extra(integration, *, language: str = "") -> dict:
    """Per-provider extras for `create_payment`, beyond id/amount/return_url.

    Registry-driven for the same reason `needs_notify_url` is: sniffing a
    gateway's signature does not work when it takes **kwargs, and a kwarg that
    silently goes nowhere is only discovered by a customer.
    """
    provider = integration.provider
    config = PROVIDERS[provider]
    extra = dict(_CHECKOUT_EXTRA.get(provider, {}))

    # Multicard is told per invoice where to call back — there is no place to
    # register it once. Without this its callback goes nowhere and every
    # payment it takes stays `pending` for ever.
    if config.get("needs_callback_url"):
        extra["callback_url"] = webhook_url(integration.seller, provider)

    if config.get("sends_language"):
        code = (language or "").split("-")[0].lower()
        extra["language"] = code if code in _OCTO_LANGUAGES else "uz"

    if config.get("sends_description"):
        # Shown on the provider's own card form and on the customer's
        # statement. A blank description there reads as a payment to nobody.
        extra["description"] = str(integration.seller.business_name)[:255]

    return extra


def start_payment(seller, provider: str, amount, *, return_url=""):
    """Create our Transaction, then ask the provider for a checkout URL.

    Returns (transaction, checkout_url). The row is written before the
    customer leaves, so an abandoned payment is still visible to the seller
    rather than vanishing.

    `return_url` may be a string, or a callable taking the transaction — the
    page the customer comes back to is addressed by the transaction's own
    reference, which does not exist until the row does.

    Deliberately NOT wrapped in a single atomic block. Two reasons, and the
    first one bit: rolling back on a provider error would take the `failed`
    row down with it, so the outage would leave the gap this is meant to
    prevent. The second is that an atomic block held open across an HTTP call
    to Payme pins a database connection for as long as they take to answer.
    """
    if provider not in PROVIDERS:
        raise PaymentError(_("Unknown payment provider."))

    if not seller.is_live:
        raise PaymentError(_("This business is not accepting payments yet."))

    integration = seller.integrations.filter(provider=provider).first()
    if integration is None or not integration.is_live:
        raise PaymentError(_("That payment method is not available right now."))

    try:
        amount = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentError(_("Enter a valid amount.")) from exc

    # Whole soum only, checked here as well as in the form: this is the
    # boundary every caller crosses, including the future API. A fractional
    # amount reaches the provider as a fractional tiyin, and what they do with
    # it — reject, floor, round — is not something to leave to chance when it
    # decides what a customer is charged.
    if amount != amount.to_integral_value():
        raise PaymentError(_("Enter a whole number of soum."))

    # A zero or negative payment is never a real one, whatever a seller has
    # set as their minimum.
    if amount <= 0:
        raise PaymentError(_("Enter a valid amount."))

    if amount < seller.min_amount or amount > seller.max_amount:
        raise PaymentError(_("That amount is outside this business's limits."))

    txn = Transaction.objects.create(
        seller=seller,
        provider=provider,
        amount=amount,
        is_test_mode=integration.is_test_mode,
    )

    try:
        gateway = build_gateway(integration)
        checkout_url = gateway.create_payment(
            id=txn.pk,
            amount=checkout_amount(provider, amount),
            return_url=return_url(txn) if callable(return_url) else return_url,
            **checkout_extra(integration, language=get_language() or ""),
        )
    except Exception as exc:
        # The provider refused or was unreachable. Record it against the row
        # so the seller sees a failed attempt rather than a gap, and never let
        # the provider's own message reach the customer — it may name keys.
        #
        # Only the exception's *class* is stored. `str(exc)` on a gateway or
        # httpx error is an unaudited third-party string going into a plain
        # JSONField that staff can read in the admin, and the full text is in
        # the log where it belongs.
        logger.exception("Seller %s: %s checkout failed", seller.uid, provider)
        _settle_quietly(txn, TransactionStatus.FAILED,
                        {"error": type(exc).__name__})
        raise PaymentError(_("We could not reach that payment system. Try another.")) from exc

    if not checkout_url:
        _settle_quietly(txn, TransactionStatus.FAILED,
                        {"error": "empty checkout url"})
        raise PaymentError(_("We could not reach that payment system. Try another."))

    _settle_quietly(txn, TransactionStatus.PENDING)
    return txn, checkout_url


def _settle_quietly(txn, status, payload=None):
    """Move the row on, but never turn a status clash into a 500.

    A provider's callback can land between creating this row and marking it
    pending — the gateway call is a network round trip, and some providers
    call back before their own response has finished arriving. The row may
    already be `paid`, and `paid -> pending` is refused by the transition
    table, correctly.

    Left unhandled that surfaced as a 500 on the pay page *after* the
    provider already held a live invoice: the customer sees an error, tries
    again, and pays twice. The callback's status is the authoritative one, so
    losing this write is exactly the right outcome — it just must not be
    loud.
    """
    try:
        txn.move_to(status, payload=payload or {})
    except InvalidTransition:
        logger.info(
            "Transaction %s was already %s when checkout tried to set %s — "
            "the provider's callback got there first",
            txn.uid, txn.status, status,
        )
