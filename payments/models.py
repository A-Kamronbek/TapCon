"""Per-seller provider credentials, and the payments made through them.

TapCon is multi-tenant: every seller has their own merchant account with each
provider, so credentials live here rather than in settings. The set of fields
per provider comes from `payments.providers`, which mirrors tolov's gateway
constructor kwargs — so `gateway_kwargs()` can be splatted straight in.

Two transaction records exist and they are not the same thing:

  * `Transaction` (here) is ours — what the customer is paying, for which
    seller. It is tolov's "account model".
  * `tolov.integrations.django.models.PaymentTransaction` is the provider's
    side — their transaction id, their state machine. tolov's webhook
    handlers own it; we never write it directly.
"""
import json
import secrets

from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from encrypted_model_fields.fields import EncryptedTextField

from .providers import PROVIDER_CHOICES, PROVIDERS


class ProviderIntegration(models.Model):
    """One seller's credentials for one provider.

    Credentials are a JSON blob inside a single encrypted column rather than a
    column per field: providers disagree on which fields they need, and the
    registry is the source of truth. We never query by a credential value, so
    there is nothing to gain from separate columns.
    """

    seller = models.ForeignKey(
        "merchants.SellerProfile",
        on_delete=models.CASCADE,
        related_name="integrations",
        verbose_name=_("seller"),
    )
    provider = models.CharField(_("provider"), max_length=32, choices=PROVIDER_CHOICES)

    # Encrypted at rest. Never rendered back into HTML — see masked_value().
    credentials_blob = EncryptedTextField(_("credentials"), blank=True, default="")

    is_enabled = models.BooleanField(_("enabled"), default=False)
    is_test_mode = models.BooleanField(
        _("test mode"),
        default=True,
        help_text=_("Use the provider's sandbox instead of real money."),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("provider integration")
        verbose_name_plural = _("provider integrations")
        constraints = [
            models.UniqueConstraint(
                fields=["seller", "provider"], name="unique_seller_provider"
            )
        ]
        ordering = ("provider",)

    def __str__(self):
        return f"{self.seller.business_name} — {self.provider}"

    # --- registry helpers ------------------------------------------------

    @property
    def config(self) -> dict:
        return PROVIDERS[self.provider]

    @property
    def label(self) -> str:
        return self.config["label"]

    @property
    def field_specs(self) -> list:
        return self.config["fields"]

    @property
    def logo(self) -> str:
        """Static filename, or "" when we have no artwork for this provider."""
        return self.config.get("logo", "")

    @property
    def logo_url(self) -> str:
        """Resolved static URL, or "" — templates fall back to the name."""
        return self._static_logo(self.logo)

    # --- what a customer sees ------------------------------------------
    #
    # A seller knows the provider by the name on their contract. A customer
    # at a counter has never heard of "Octo" and is looking for the button
    # that takes a bank card. Where the two differ, the registry says so.

    @property
    def customer_label(self) -> str:
        return self.config.get("customer_label") or self.label

    @property
    def customer_logo_url(self) -> str:
        return self._static_logo(self.config.get("customer_logo") or self.logo)

    @staticmethod
    def _static_logo(filename: str) -> str:
        if not filename:
            return ""
        from django.templatetags.static import static

        return static(f"img/providers/{filename}")

    # --- credentials -----------------------------------------------------

    @property
    def credentials(self) -> dict:
        if not self.credentials_blob:
            return {}
        try:
            data = json.loads(self.credentials_blob)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Filter on the way out too, so a row written before a provider's
        # field list changed cannot feed a stray kwarg to the gateway.
        known = {spec["name"] for spec in self.field_specs}
        return {k: v for k, v in data.items() if k in known}

    @credentials.setter
    def credentials(self, value: dict) -> None:
        """Store only fields the registry knows about.

        gateway_kwargs() splats this dict straight into the tolov gateway
        constructor, so a stray key would not fail here — it would fail at
        payment time, on a real customer.
        """
        known = {spec["name"] for spec in self.field_specs}
        cleaned = {
            k: v
            for k, v in (value or {}).items()
            if k in known and v not in (None, "")
        }
        self.credentials_blob = json.dumps(cleaned) if cleaned else ""

    def get(self, name: str) -> str:
        return self.credentials.get(name, "")

    def masked_value(self, name: str) -> str:
        """What the UI may show: enough to recognise, not enough to reuse."""
        value = self.get(name)
        if not value:
            return ""
        if len(value) <= 4:
            return "•" * len(value)
        return "•" * max(4, len(value) - 4) + value[-4:]

    @property
    def missing_fields(self) -> list:
        stored = self.credentials
        return [f["name"] for f in self.field_specs if not stored.get(f["name"])]

    @property
    def is_complete(self) -> bool:
        """Every field the provider needs has a value."""
        return not self.missing_fields

    @property
    def is_live(self) -> bool:
        """Ready to actually take a payment."""
        return self.is_enabled and self.is_complete

    def gateway_kwargs(self) -> dict:
        """Splat straight into the tolov gateway constructor.

        Some providers need credentials we collect but the constructor does
        not take — Uzum and Paynet authenticate their webhooks with a username
        and password. `gateway_fields` in the registry names the subset the
        constructor accepts; without it, everything goes.
        """
        allowed = self.config.get("gateway_fields")
        credentials = self.credentials
        if allowed is not None:
            credentials = {k: v for k, v in credentials.items() if k in allowed}

        # Credentials are stored as text, because that is what a seller types.
        # A field the provider's API types as a number goes out as a number:
        # Octo declares `octo_shop_id: int` and puts it straight into the JSON
        # body, and a shop id quoted as a string is a different value to a
        # strict API. The form has already refused anything non-numeric.
        numeric = {
            spec["name"] for spec in self.field_specs if spec.get("numeric")
        }
        credentials = {
            k: (int(v) if k in numeric and str(v).strip().lstrip("-").isdigit() else v)
            for k, v in credentials.items()
        }
        return {**credentials, "is_test_mode": self.is_test_mode}

    def webhook_credentials(self) -> dict:
        """Everything stored, for the webhook handler's own attributes."""
        return dict(self.credentials)


class TransactionStatus(models.TextChoices):
    CREATED = "created", _("Created")
    PENDING = "pending", _("Pending")
    PAID = "paid", _("Paid")
    CANCELLED = "cancelled", _("Cancelled")
    FAILED = "failed", _("Failed")
    # TapCon never starts a refund and offers a seller no way to. This status
    # exists only because Octo can report one that happened in *its* cabinet
    # or through a bank dispute, and a payment whose money has gone back must
    # stop counting as takings. The label says who returned it, so a seller
    # reading their ledger does not think TapCon did something.
    REFUNDED = "refunded", _("Returned to customer")


# A payment only ever moves forwards. Anything else is a bug or a replayed
# webhook, and both should be refused rather than allowed to corrupt a ledger
# a seller is paid against.
ALLOWED_TRANSITIONS = {
    TransactionStatus.CREATED: {
        TransactionStatus.PENDING,
        TransactionStatus.PAID,
        TransactionStatus.CANCELLED,
        TransactionStatus.FAILED,
    },
    TransactionStatus.PENDING: {
        TransactionStatus.PAID,
        TransactionStatus.CANCELLED,
        TransactionStatus.FAILED,
    },
    # A paid payment is never *cancelled* — but it can be refunded, and Octo
    # sends exactly that callback. Refusing it would leave a refunded payment
    # showing as money received, which is the one thing a ledger must not do.
    # It is a separate status rather than a reversal: the payment did happen,
    # and both facts matter.
    TransactionStatus.PAID: {TransactionStatus.REFUNDED},
    TransactionStatus.CANCELLED: set(),
    TransactionStatus.FAILED: set(),
    TransactionStatus.REFUNDED: set(),
}


class InvalidTransition(Exception):
    """Refused a status change that would move a payment backwards."""


# A customer's payment is over, whatever the outcome. `paid` belongs here even
# though a refund can still follow it.
_SETTLED = [
    TransactionStatus.PAID,
    TransactionStatus.CANCELLED,
    TransactionStatus.FAILED,
    TransactionStatus.REFUNDED,
]


class TransactionQuerySet(models.QuerySet):
    """Named filters, so nobody has to remember the two that matter.

    Every total a seller is ever shown must go through `real()`. A sandbox
    payment is a real row with a real amount, and the only thing separating
    it from money is this flag — an `aggregate(Sum("amount"))` that forgets it
    would quietly inflate a seller's takings.
    """

    def real(self):
        return self.filter(is_test_mode=False)

    def sandbox(self):
        return self.filter(is_test_mode=True)

    def paid(self):
        return self.filter(status=TransactionStatus.PAID)

    def settled(self):
        """Everything that will never change again.

        Note `paid` is settled but not final in the strict sense — a refund
        can still follow it. It is listed here because from the customer's
        side the payment is over.
        """
        return self.filter(status__in=_SETTLED)

    def in_flight(self):
        return self.exclude(status__in=_SETTLED)


class Transaction(models.Model):
    """One payment attempt, by one customer, at one seller.

    This is *our* record, and it is what `settings.TOLOV[...]["ACCOUNT_MODEL"]`
    points at: tolov's webhook handlers look this row up by id, check the
    amount against it, and keep their own `PaymentTransaction` row alongside
    for the provider's side of the conversation.

    It is created before the customer leaves for the provider, so an abandoned
    payment leaves a `created` row behind rather than no trace at all.
    """

    seller = models.ForeignKey(
        "merchants.SellerProfile",
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name=_("seller"),
    )

    # The customer's result page is addressed by this, not by the primary
    # key. The pk travels to the provider and back as tolov's account id, so
    # it is effectively public to them — but sequential ids in a URL a
    # customer holds would let anyone read the next customer's payment.
    uid = models.CharField(
        _("reference"), max_length=32, unique=True, db_index=True, blank=True
    )

    provider = models.CharField(_("provider"), max_length=32, choices=PROVIDER_CHOICES)

    # UZS, which has no subunit in practice. Decimal so the arithmetic stays
    # exact, and so tolov's `Decimal(amount) * 100` comparison lines up.
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)

    status = models.CharField(
        _("status"),
        max_length=16,
        choices=TransactionStatus.choices,
        default=TransactionStatus.CREATED,
        db_index=True,
    )

    # The provider's own id, copied across when their webhook first names it.
    provider_txn_id = models.CharField(
        _("provider transaction id"), max_length=255, blank=True, default=""
    )
    # Whatever the provider last sent, kept verbatim for support questions.
    raw_payload = models.JSONField(_("raw payload"), default=dict, blank=True)

    is_test_mode = models.BooleanField(_("test mode"), default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True, db_index=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        verbose_name = _("transaction")
        verbose_name_plural = _("transactions")
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["seller", "-created_at"])]

    def __str__(self):
        return f"{self.seller.business_name} — {self.amount} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.uid:
            # Not a card id a human types, so no need to avoid look-alike
            # characters — but it is unique, and a collision would fail a
            # payment, so retry rather than let one IntegrityError through.
            for _attempt in range(10):
                candidate = secrets.token_urlsafe(16)[:22]
                if not Transaction.objects.filter(uid=candidate).exists():
                    self.uid = candidate
                    break
            else:
                raise RuntimeError("Could not allocate a unique transaction uid")
        super().save(*args, **kwargs)

    @property
    def is_settled(self) -> bool:
        """The customer's payment is over, whatever the outcome.

        Not the same as "this row can never change again", and the difference
        is not academic: a paid payment can still be refunded, so it is not
        final — but it *is* settled, and the result page must stop polling.
        Conflating the two left every customer's phone polling for ever after
        a successful payment.
        """
        return TransactionStatus(self.status) in _SETTLED

    @property
    def is_final(self) -> bool:
        """No transition remains. Used for reasoning about the state machine,
        never for deciding whether to stop polling — see `is_settled`."""
        return not ALLOWED_TRANSITIONS[TransactionStatus(self.status)]

    @property
    def provider_label(self) -> str:
        return PROVIDERS.get(self.provider, {}).get("label", self.provider)

    def can_move_to(self, status) -> bool:
        return TransactionStatus(status) in ALLOWED_TRANSITIONS[
            TransactionStatus(self.status)
        ]

    def move_to(self, status, *, payload=None, provider_txn_id="") -> bool:
        """Advance the status, refusing anything that is not a step forward.

        The decision is made from the row **as the database has it right now**,
        under a write lock, not from whatever this instance was loaded with.
        That matters: providers retry, and a Perform and a Cancel callback can
        be in flight at the same moment. Deciding from a stale in-memory status
        would let a cancel land on top of a payment that was already taken.

        Arriving again at the status we already hold is a no-op rather than an
        error, because a retried webhook must not blow up. Returns whether
        anything actually changed.

        Note for anyone reading a green test run as proof: SQLite ignores
        SELECT ... FOR UPDATE and serialises writes at the database level
        instead, so the dev database cannot actually exhibit the race. The
        lock is what makes this correct on PostgreSQL, which is what
        production runs.
        """
        status = TransactionStatus(status)

        with db_transaction.atomic():
            current = (
                Transaction.objects.select_for_update().filter(pk=self.pk).first()
            )
            if current is None:
                raise InvalidTransition("Transaction no longer exists")

            if status == TransactionStatus(current.status):
                # Keep this instance honest about what the row really says.
                self.status = current.status
                self.paid_at = current.paid_at
                return False

            if not current.can_move_to(status):
                raise InvalidTransition(
                    f"{current.status} -> {status} is not a valid transition"
                )

            current.status = status
            if status == TransactionStatus.PAID:
                current.paid_at = timezone.now()
            if provider_txn_id:
                current.provider_txn_id = provider_txn_id
            if payload is not None:
                current.raw_payload = payload

            current.save(
                update_fields=[
                    "status",
                    "paid_at",
                    "provider_txn_id",
                    "raw_payload",
                    "updated_at",
                ]
            )

        # Mirror the committed values back onto the caller's instance.
        self.status = current.status
        self.paid_at = current.paid_at
        self.provider_txn_id = current.provider_txn_id
        self.raw_payload = current.raw_payload
        return True
