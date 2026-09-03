"""Per-seller webhook endpoints.

tolov's webhook handlers were written for a single-tenant shop: their
``__init__`` reads ``settings.TOLOV[...]`` once and stores the credentials on
the instance. TapCon has a different merchant account per seller, so a global
key would be wrong for everyone but the first.

The fix is small, and it is the whole reason the Phase 4 spike mattered:
everything after ``__init__`` reads ``self.payme_key`` (and friends) rather
than the settings dict, and Django builds a fresh view instance per request
and calls ``setup()`` before ``dispatch()``. So we resolve the seller from the
URL in ``setup()`` and overwrite those attributes with theirs.

    /webhooks/payme/<seller_uid>/

Nothing here is CSRF-protected — the provider is not a browser and has no
token. Authentication is the provider's own scheme, checked by tolov against
the credentials we set: Basic auth for Payme, a signature for Click.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction
from django.http import Http404, JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from tolov.core.exceptions import AccountNotFound
from tolov.integrations.django import webhooks as tolov_webhooks

from core.limits import webhook_per_seller
from merchants.models import SellerProfile

from .models import InvalidTransition, Transaction, TransactionStatus
from .services import build_gateway, webhook_url

logger = logging.getLogger("payments")


class SellerWebhookMixin:
    """Resolve the seller from the URL and wear their credentials.

    ``credential_map`` maps the attribute tolov reads to the name of the
    credential in our registry. Only names present in the seller's stored
    credentials are copied, so a provider we have not finished wiring up
    fails authentication rather than silently authenticating as nobody.
    """

    provider = ""
    credential_map: dict = {}

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)

        self.seller = (
            SellerProfile.objects.filter(uid=kwargs.get("uid")).first()
        )
        if self.seller is None:
            # Deliberately indistinguishable from a wrong URL: a webhook
            # endpoint should not confirm which seller ids exist.
            raise Http404("Unknown seller")

        integration = self.seller.integrations.filter(provider=self.provider).first()
        # Deliberately `is_complete`, not `is_live`. `is_enabled` decides
        # whether the pay page *offers* a provider; it must not decide whether
        # we accept confirmations for payments that already started. A seller
        # switching Payme off one minute after a customer paid through it
        # would otherwise have that callback rejected, and the payment would
        # sit as `pending` for ever while the money had actually moved.
        if integration is None or not integration.is_complete:
            raise Http404("Provider not configured for this seller")

        self.integration = integration
        credentials = integration.webhook_credentials()
        for attribute, credential_name in self.credential_map.items():
            if credentials.get(credential_name):
                setattr(self, attribute, credentials[credential_name])

    # --- scoping tolov's lookups to this seller -------------------------

    def _find_account(self, lookup):
        """Refuse a transaction that belongs to a different seller.

        tolov looks our Transaction up by primary key alone, and that key
        travels to the provider and back. Without this check, seller A —
        holding valid credentials of their own — could drive their webhook at
        seller B's transaction id and have it marked paid. Scoping the lookup
        means the provider is told the account does not exist, before tolov
        writes anything down.

        `lookup` is deliberately not named `params`: the handlers disagree
        about what they pass. Payme, Click, Uzum and Paynet pass the request
        params dict; **Octo passes the bare account id**. This override only
        forwards it and inspects the result, so it works for both — but do not
        reach into it here without checking which shape you have.
        """
        account = super()._find_account(lookup)
        # Not every handler raises for a miss: Multicard's returns None and
        # lets its caller carry on. Normalise that into the same refusal, and
        # never reach into it before knowing there is something there.
        if account is None:
            raise AccountNotFound("Account not found")
        if getattr(account, "seller_id", None) != self.seller.id:
            logger.error(
                "%s webhook for seller %s reached for transaction %s, which is "
                "not theirs",
                self.provider,
                self.seller.uid,
                account.pk,
            )
            raise AccountNotFound("Account not found")

        # Take the row, and hold it for the rest of the callback.
        #
        # Everything below — and tolov's own "this account already has a
        # pending transaction" check, which runs straight after we return —
        # is a read followed by a write. Two callbacks in two Gunicorn
        # workers both read, both find nothing in their way, and both open a
        # charge. Measured, not theorised: eight simultaneous
        # CreateTransaction calls opened **two** invoices against one
        # payment, so the customer is asked for the amount twice and only one
        # of the two can ever be recorded. It is reachable with two tabs.
        #
        # `webhook_dispatch` wraps the whole view in a transaction, so this
        # lock is held until the callback has finished and committed. The
        # second worker then wakes up, re-reads, and finds the pending
        # transaction the first one wrote.
        account = (
            Transaction.objects.select_for_update().filter(pk=account.pk).first()
        )
        if account is None:  # deleted between the two reads; not reachable today
            raise AccountNotFound("Account not found")

        # A payment that is over must never be started again.
        #
        # tolov only refuses a second attempt while a *non-final* transaction
        # exists for the account — a `SUCCESSFULLY` or `CANCELLED` one is
        # explicitly excluded, so it happily opens a new one. That is a real
        # sequence, not a hypothetical: the checkout URL carries our
        # transaction id and stays in the customer's history, so cancelling
        # and then going Back and paying is two taps.
        #
        # Without this the customer is charged, the provider is answered
        # "performed", and our row is still `cancelled` — the transition table
        # refuses `cancelled → paid`, `_advance` logs it and returns, and the
        # seller is never credited for money that has genuinely left the
        # customer's account. On an already-`paid` row it is worse: the second
        # charge is absorbed as a no-op and the ledger shows one payment.
        #
        # `_find_account` is the right place because both the check and the
        # create path go through it, so the refusal lands *before* the
        # customer is asked for money rather than after. Octo's refund
        # callbacks do not reach here — its handler only consults
        # `_find_account` when no transaction row exists yet — so recording a
        # refund against a paid payment still works.
        if account.is_settled:
            # ...unless this is the provider resending the payment we have
            # already recorded. Most handlers only reach `_find_account` when
            # they are opening a *new* provider transaction, so a settled row
            # there can only mean a second payment. **Multicard is different**:
            # it calls `_find_account` on every callback, before its own
            # deduplication, so refusing here answered a perfectly ordinary
            # retry with 404 — which a provider can read as "unknown invoice,
            # stop retrying". Same provider id means same payment.
            replayed = self._provider_txn_id_from_request()
            if replayed and replayed == account.provider_txn_id:
                return account

            logger.warning(
                "%s webhook for seller %s tried to start a new payment on "
                "transaction %s, which is already %s — refusing before any "
                "money moves",
                self.provider,
                self.seller.uid,
                account.uid,
                account.status,
            )
            raise AccountNotFound("Account not found")
        return account

    # --- what tolov calls back into ------------------------------------

    def _our_transaction(self, tolov_transaction):
        """tolov's row points at ours by id, and ours must be this seller's.

        The seller check is not paranoia: the account id travels through the
        provider, so a seller who knew another seller's transaction id could
        otherwise have their own webhook mark it paid.
        """
        try:
            return Transaction.objects.get(
                pk=tolov_transaction.account_id, seller=self.seller
            )
        except (Transaction.DoesNotExist, ValueError, TypeError):
            logger.error(
                "%s webhook for seller %s named unknown transaction %r",
                self.provider,
                self.seller.uid,
                getattr(tolov_transaction, "account_id", None),
            )
            return None

    @staticmethod
    def _jsonable(value):
        """Make a provider's payload safe to store in a JSONField.

        Octo parses `total_sum` into a Decimal before handing the payload to
        us, and Django's JSON encoder refuses Decimals — so storing it raised
        TypeError, the webhook answered 500, and Octo would have retried a
        payment it had already taken, for ever. Decimals become strings, which
        keeps the exact digits rather than rounding through a float.
        """
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: SellerWebhookMixin._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [SellerWebhookMixin._jsonable(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _amount_agrees(self, tolov_transaction, txn) -> bool:
        """Belt and braces on the one number that is money.

        Five of the six tolov handlers check the amount against our row before
        they ever call us, and reject the callback outright if it disagrees.
        Multicard's does not — it records whatever it is sent. So the check
        lives here as well, where it covers every provider including any added
        later, and where a provider-side bug cannot skip it.

        tolov stores this side of the conversation in soum for all six (Payme,
        Click, Uzum, Paynet and Multicard divide their tiyin down; Octo works
        in soum already), so both sides are directly comparable.

        A missing amount is not a mismatch: Paynet's Perform callback does not
        repeat it.
        """
        reported = getattr(tolov_transaction, "amount", None)
        if reported in (None, ""):
            return True
        try:
            reported = Decimal(str(reported))
        except (InvalidOperation, TypeError, ValueError):
            return False
        # Tolerance, not equality: these arrive as floats through JSON.
        return abs(reported - txn.amount) <= Decimal("0.01")

    def _provider_txn_id_from_request(self) -> str:
        """The provider's own id for the callback being handled, if we can see it.

        Only needed by handlers that call `_find_account` on every callback
        rather than only when opening a new provider transaction — it is what
        tells a replay apart from a second payment. Empty by default, because
        for every other provider reaching `_find_account` already means a new
        transaction.
        """
        return ""

    def _save_payload(self, txn, params):
        """Write what the provider said onto our row, without moving it.

        Used when something happened that we must be able to reconstruct
        afterwards but that must not change the status — a refused
        transition, a second charge, a partial refund.
        """
        txn.raw_payload = self._jsonable(params)
        txn.save(update_fields=["raw_payload", "updated_at"])

    def _advance(self, tolov_transaction, status, params):
        txn = self._our_transaction(tolov_transaction)
        if txn is None:
            return

        if status == TransactionStatus.PAID and not self._amount_agrees(
            tolov_transaction, txn
        ):
            # Never mark a payment paid for an amount we did not ask for. The
            # seller would hand over goods against it. Left `pending` and
            # shouted about instead, because this can only mean our record and
            # the provider's disagree about money.
            logger.critical(
                "%s webhook for seller %s: transaction %s is %s but the "
                "provider reported %s — refusing to mark it paid",
                self.provider,
                self.seller.uid,
                txn.uid,
                txn.amount,
                getattr(tolov_transaction, "amount", None),
            )
            return

        params = self._jsonable(params) if isinstance(params, dict) else {}
        provider_txn_id = str(tolov_transaction.transaction_id or "")

        # The last line of defence, and the one that must never be quiet.
        #
        # `move_to` treats arriving at the status you already hold as a no-op,
        # which is exactly right for a provider retrying a callback it already
        # sent — same payment, same provider id. It is exactly wrong for a
        # *second* payment: a different provider id on an already-paid row
        # means the customer has been charged twice and the ledger is about to
        # show one payment.
        #
        # `_find_account` should have refused this before any money moved. If
        # it did not, the money is already gone and the only thing left worth
        # doing is making it impossible to miss.
        if (
            status == TransactionStatus.PAID
            and txn.status == TransactionStatus.PAID
            and provider_txn_id
            and txn.provider_txn_id
            and provider_txn_id != txn.provider_txn_id
        ):
            logger.critical(
                "%s webhook for seller %s: transaction %s was already paid as "
                "%s and has now been paid again as %s. The customer has been "
                "charged twice for %s and only one payment is recorded — this "
                "needs a refund from the provider's cabinet.",
                self.provider,
                self.seller.uid,
                txn.uid,
                txn.provider_txn_id,
                provider_txn_id,
                txn.amount,
            )
            self._save_payload(txn, {
                "double_charge": {
                    "first": txn.provider_txn_id,
                    "second": provider_txn_id,
                    "payload": params,
                }
            })
            return

        try:
            # move_to() is a no-op when the status is already there, which is
            # what makes a retried webhook harmless.
            txn.move_to(
                status,
                payload=params,
                provider_txn_id=provider_txn_id,
            )
        except InvalidTransition:
            # A callback that contradicts a settled payment — a Perform
            # arriving after a Cancel, say. Never let it out as a 500: the
            # provider would simply retry the same impossible thing for hours.
            # Answer normally and leave a loud line to investigate, because
            # this means our record and theirs disagree about real money.
            #
            # CRITICAL rather than ERROR when a payment was refused: a
            # provider saying "performed" against a cancelled row means money
            # has moved that the seller will never be credited for, and that
            # is not something to find in a log review next week.
            shout = (logger.critical if status == TransactionStatus.PAID
                     else logger.error)
            shout(
                "%s webhook for seller %s: refused %s -> %s on transaction %s"
                "%s",
                self.provider,
                self.seller.uid,
                txn.status,
                status,
                txn.uid,
                (" — the customer may have been charged for a payment that "
                 "cannot be recorded" if status == TransactionStatus.PAID else ""),
            )
            self._save_payload(txn, {
                "refused_transition": {
                    "from": txn.status,
                    "to": status,
                    "provider_txn_id": provider_txn_id,
                    "payload": params,
                }
            })

    def successfully_payment(self, params, transaction):
        self._advance(transaction, TransactionStatus.PAID, params)

    def cancelled_payment(self, params, transaction):
        self._advance(transaction, TransactionStatus.CANCELLED, params)


class RefundAwareMixin:
    """For providers whose cancel hook covers more than one outcome.

    Octo funnels `canceled`, `failed` **and `refunded`** into the single
    `cancelled_payment` hook. Taking that at face value gets two things wrong:

    * a refund recorded as a cancellation is refused outright — a paid payment
      cannot be cancelled — so the row would stay `paid` and refunded money
      would keep showing as money received;
    * a card declined by the bank would be filed as "the customer changed
      their mind", which is a different thing to a seller reading their day.

    So the outcome is read off the callback itself rather than inferred from
    which hook fired.
    """

    # Whatever the provider calls each outcome in the payload.
    refund_statuses = {"refunded", "refund"}
    failure_statuses = {"failed", "failure", "error"}

    def cancelled_payment(self, params, transaction):
        params = params if isinstance(params, dict) else {}
        reported = str(params.get("status") or "").strip().lower()

        if reported in self.refund_statuses:
            if not self._is_full_refund(params, transaction):
                # A partial refund is a real event, but it is not this row
                # becoming `refunded` — the payment still stands for the rest.
                # We have nowhere to put "half of it came back", so the honest
                # thing is to leave the status alone and keep the payload,
                # rather than write down a reversal that did not happen.
                logger.warning(
                    "%s webhook for seller %s: partial refund on transaction "
                    "%s (refunded %s of %s) — status left as paid",
                    self.provider,
                    self.seller.uid,
                    transaction.transaction_id,
                    params.get("refunded_sum"),
                    params.get("total_sum"),
                )
                self._record_payload(transaction, params)
                return
            status = TransactionStatus.REFUNDED
        elif reported in self.failure_statuses:
            status = TransactionStatus.FAILED
        else:
            status = TransactionStatus.CANCELLED

        self._advance(transaction, status, params)

    def _is_full_refund(self, params, transaction) -> bool:
        """Did the whole payment come back?

        Octo reports `refunded_sum` alongside the original `total_sum`. When
        it says nothing, assume full: a refund callback with no figure is
        Octo's way of saying the payment was reversed.
        """
        refunded = params.get("refunded_sum")
        if refunded in (None, ""):
            return True
        try:
            refunded = Decimal(str(refunded))
        except (InvalidOperation, TypeError, ValueError):
            return True

        txn = self._our_transaction(transaction)
        expected = txn.amount if txn is not None else None
        if expected is None:
            return True
        return refunded >= expected - Decimal("0.01")

    def _record_payload(self, transaction, params):
        """Keep what the provider said without moving the status."""
        txn = self._our_transaction(transaction)
        if txn is None:
            return
        self._save_payload(txn, params)


@method_decorator(csrf_exempt, name="dispatch")
class PaymeWebhook(SellerWebhookMixin, tolov_webhooks.PaymeWebhook):
    provider = "payme"
    credential_map = {"payme_id": "payme_id", "payme_key": "payme_key"}


@method_decorator(csrf_exempt, name="dispatch")
class ClickWebhook(SellerWebhookMixin, tolov_webhooks.ClickWebhook):
    provider = "click"
    credential_map = {"service_id": "service_id", "secret_key": "secret_key"}


@method_decorator(csrf_exempt, name="dispatch")
class UzumWebhook(SellerWebhookMixin, tolov_webhooks.UzumWebhook):
    """Uzum's Biller API names the operation in the path, not the body.

    tolov's handler is written for that — ``post(self, request, action, **_)``
    — so the URL has to supply it. Ours did not, and every Uzum callback
    raised TypeError and answered 500 before touching a line of our code. A
    provider reads 500 as "retry later", so a payment that had genuinely been
    taken would have sat `pending` for ever while the customer's money moved.
    Found by driving one real callback at a running server; no unit test saw
    it, because they all use Payme's shape.
    """

    provider = "uzum"
    # Tells webhook_dispatch to expect the trailing /check/, /confirm/ etc.
    action_in_url = True
    credential_map = {
        "service_id": "service_id",
        "username": "username",
        "password": "password",
    }


@method_decorator(csrf_exempt, name="dispatch")
class PaynetWebhook(SellerWebhookMixin, tolov_webhooks.PaynetWebhook):
    provider = "paynet"
    credential_map = {
        "paynet_service_id": "service_id",
        "paynet_username": "username",
        "paynet_password": "password",
    }


@method_decorator(csrf_exempt, name="dispatch")
class OctoWebhook(RefundAwareMixin, SellerWebhookMixin, tolov_webhooks.OctoWebhook):
    """Octo needs more care than the other five.

    Its ``__init__`` *raises* unless a shop id, secret and callback key are
    already in settings, so it cannot be constructed at all before the seller
    is known. `settings.TOLOV["OCTO_BANK"]` therefore carries deliberately
    unusable placeholders to get past that check, and everything real is
    injected here.

    Two things beyond the usual credentials:

    * ``unique_key`` — Octo signs callbacks ``sha1(unique_key + uuid +
      status)``. It is not the secret, and it is per seller.
    * ``is_test_mode`` — tolov **skips signature verification entirely** in
      test mode, so this must follow the seller's own setting rather than a
      global one. A live seller left on a global test flag would accept
      unsigned callbacks.
    """

    provider = "octo"
    credential_map = {
        "octo_shop_id": "octo_shop_id",
        "octo_secret": "octo_secret",
        "unique_key": "octo_unique_key",
    }

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)

        # Signature verification is skipped in test mode, so this flag decides
        # whether callbacks are checked at all. It follows the integration.
        self.is_test_mode = self.integration.is_test_mode
        self.notify_url = webhook_url(self.seller, self.provider)

    # `gateway` is a property rather than an attribute, and that is not
    # decoration. tolov's __init__ builds one from the settings placeholders
    # before setup() has resolved the seller, and we then replaced it with the
    # seller's — two OctoGateway objects per callback, each carrying an
    # httpx.Client that loads its own CA bundle, on an endpoint a provider
    # retries. Nothing on the webhook path calls the gateway at all, so now
    # the placeholder is closed on arrival and the real one is built only if
    # something ever asks for it.
    _gateway = None

    @property
    def gateway(self):
        if self._gateway is None:
            self._gateway = build_gateway(self.integration)
        return self._gateway

    @gateway.setter
    def gateway(self, value):
        client = getattr(value, "http_client", None)
        if client is not None:
            client.close()


@method_decorator(csrf_exempt, name="dispatch")
class MulticardWebhook(SellerWebhookMixin, tolov_webhooks.MulticardWebhook):
    """Multicard signs md5(store_id + invoice_id + amount + secret).

    `store_id` is in the map for two reasons: it is part of the signature, and
    tolov also compares it against the callback and rejects another store's.
    Left out, that comparison would be made against the settings placeholder
    and every real callback would be turned away.
    """

    provider = "multicard"
    credential_map = {
        "application_id": "application_id",
        "secret": "secret",
        "store_id": "store_id",
    }

    def _provider_txn_id_from_request(self) -> str:
        """Multicard's `uuid`, read straight off the request body.

        Its handler calls `_find_account` on *every* callback, before its own
        deduplication — unlike the other five, which only reach it when
        opening a new provider transaction. So the settled-transaction guard
        needs this to tell "Multicard is retrying the payment we already
        recorded" from "a second payment against a finished one". Without it
        the guard answered an ordinary retry with 404.
        """
        try:
            return str(json.loads(self.request.body or b"{}").get("uuid") or "")
        except (ValueError, AttributeError):
            return ""

    def post(self, request, *args, **kwargs):
        """Answer an unknown invoice, rather than crashing on one.

        Octo's tolov handler wraps its own body in a try/except and turns an
        AccountNotFound into a 404. Multicard's does not — so a callback
        naming an invoice that is not this seller's (which is exactly what our
        `_find_account` raises for) came out of the view as an unhandled
        exception: a 500, and a provider that retries a 500 for hours.
        """
        try:
            return super().post(request, *args, **kwargs)
        except AccountNotFound:
            logger.warning(
                "multicard webhook for seller %s named an invoice that is not "
                "theirs",
                self.seller.uid,
            )
            return JsonResponse(
                {"success": False, "error": "unknown invoice"}, status=404
            )


WEBHOOKS = {
    "payme": PaymeWebhook,
    "click": ClickWebhook,
    "uzum": UzumWebhook,
    "paynet": PaynetWebhook,
    "octo": OctoWebhook,
    "multicard": MulticardWebhook,
}


# The CSRF middleware checks the callback the URLconf resolved, which is this
# function — so the exemption has to be here, not only on the classes.
#
# The rate limit sits outside every handler, so a flood is turned away before
# any of them touches the database. It is counted per seller and set far
# above what a provider could ever send, because refusing a genuine callback
# means a seller who was paid and never told — see core/limits.py.
@csrf_exempt
@webhook_per_seller
def webhook_dispatch(request, provider, uid, action=None):
    """One URL pattern, six handlers, chosen by name.

    `action` is the trailing segment Uzum needs and nobody else uses. It is
    passed on only to the handler that declares it, so an extra segment on
    someone else's URL is a 404 rather than a confusing TypeError.
    """
    view = WEBHOOKS.get(provider)
    if view is None:
        return JsonResponse({"error": "unknown provider"}, status=404)

    extra = {}
    if getattr(view, "action_in_url", False):
        if not action:
            # Answered rather than raised: this is a misconfigured callback
            # URL in the provider's cabinet, and the message is what tells
            # whoever set it up what is wrong.
            return JsonResponse(
                {
                    "status": "FAILED",
                    "errorCode": "10003",
                    "detail": (
                        "this provider's callback URL must end with the "
                        "operation, e.g. /check/ /create/ /confirm/ "
                        "/reverse/ /status/"
                    ),
                },
                status=400,
            )
        extra["action"] = action
    elif action:
        return JsonResponse({"error": "unknown endpoint"}, status=404)

    # One transaction around the whole callback, so the row lock taken in
    # `_find_account` is still held while tolov decides whether to open a
    # charge. Without it that lock would be released the moment the SELECT
    # returned and two workers could both open one — which they did, in
    # `payments/test_concurrency.py`, before this existed.
    #
    # The rate limit is deliberately outside it: its counter must survive a
    # callback that ends in a rollback.
    with db_transaction.atomic():
        return view.as_view()(request, provider=provider, uid=uid, **extra)
