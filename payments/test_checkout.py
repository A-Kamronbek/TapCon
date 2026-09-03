"""What we actually send a provider, and what we do with what comes back.

Every defect held in place here was invisible from the outside: the pay page
rendered, the button worked, the customer reached a checkout. The damage was
in the numbers and the units — the parts nobody sees until money moves.
"""
import hashlib
import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from accounts.services import register_seller
from merchants.models import SellerProfile, SellerStatus

from .models import Transaction, TransactionStatus
from .providers import PROVIDERS
from .services import (
    checkout_amount,
    checkout_extra,
    save_credentials,
    set_enabled,
    set_test_mode,
    start_payment,
    webhook_url,
)
from .webhooks import MulticardWebhook

PASSWORD = "s3cretpw!x"

CREDENTIALS = {
    "payme": {"payme_id": "pm-1", "payme_key": "pm-key"},
    "click": {
        "service_id": "111",
        "merchant_id": "222",
        "merchant_user_id": "333",
        "secret_key": "click-secret",
    },
    "uzum": {"service_id": "444", "username": "u", "password": "p"},
    "paynet": {
        "merchant_id": "555",
        "service_id": "666",
        "username": "u",
        "password": "p",
    },
    "octo": {
        "octo_shop_id": "77001",
        "octo_secret": "octo-secret",
        "octo_unique_key": "octo-unique-key",
    },
    "multicard": {
        "application_id": "app-1",
        "secret": "mc-secret",
        "store_id": "888",
    },
}


class SellerTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

        self.user = register_seller(
            phone="+998 90 321 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=self.user)
        self.profile.approve()

    def connect(self, provider, *, test_mode=False):
        save_credentials(self.profile, provider, CREDENTIALS[provider])
        set_enabled(self.profile, provider, True)
        if test_mode:
            set_test_mode(self.profile, provider, True)
        return self.profile.integrations.get(provider=provider)


class AmountUnitTests(SellerTestCase):
    """The unit each gateway expects, which is not the same for all six."""

    def test_paynet_is_asked_for_tiyin(self):
        """Paynet builds `?a=<amount>` itself and that parameter is tiyin.

        Passing soum would have put `a=50000` in the URL for a 50 000 soum
        bill — asking the customer for 500 soum, and leaving the seller a
        payment that could never match.
        """
        self.assertEqual(checkout_amount("paynet", Decimal("50000")), 5000000)

    def test_everyone_else_is_asked_for_soum(self):
        """Payme, Click, Uzum, Octo and Multicard convert internally."""
        for provider in ("payme", "click", "uzum", "octo", "multicard"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    checkout_amount(provider, Decimal("50000")), Decimal("50000")
                )

    def test_the_unit_is_declared_in_the_registry(self):
        """So adding a provider is reading their docs, not reading this file."""
        self.assertEqual(PROVIDERS["paynet"].get("amount_unit"), "tiyin")

    def test_the_converted_amount_is_a_whole_number(self):
        """A float would reach the URL as `a=5000000.0`."""
        value = checkout_amount("paynet", Decimal("1234"))
        self.assertIsInstance(value, int)
        self.assertEqual(value, 123400)


class CallbackUrlTests(SellerTestCase):
    def test_multicard_is_told_where_to_call_back(self):
        """There is no cabinet field for it — it is per invoice or nowhere.

        Without this a Multicard payment is taken and never confirmed: the
        customer is charged and the row sits `pending` for ever.
        """
        integration = self.connect("multicard")
        extra = checkout_extra(integration)
        self.assertEqual(
            extra["callback_url"], webhook_url(self.profile, "multicard")
        )

    def test_the_callback_url_names_this_seller(self):
        integration = self.connect("multicard")
        self.assertIn(
            f"/webhooks/multicard/{self.profile.uid}/",
            checkout_extra(integration)["callback_url"],
        )

    def test_nobody_else_is_given_a_callback_url(self):
        """Their constructors and checkout calls do not take one."""
        for provider in ("payme", "click", "uzum", "paynet", "octo"):
            with self.subTest(provider=provider):
                integration = self.connect(provider)
                self.assertNotIn("callback_url", checkout_extra(integration))


class OfferedProviderTests(SellerTestCase):
    """What the customer is actually shown, and in what order."""

    def offered(self):
        from .services import enabled_providers

        return [i.provider for i in enabled_providers(self.profile)]

    def test_only_one_card_button_is_offered(self):
        """Octo and Multicard both say "Bank card" to a customer.

        Two buttons with the same words on them is a choice nobody can make,
        and the second one is a brand a customer has never heard of.
        """
        self.connect("octo")
        self.connect("multicard")
        offered = self.offered()
        self.assertIn("octo", offered)
        self.assertNotIn("multicard", offered)

    def test_a_seller_with_only_multicard_still_gets_a_card_button(self):
        self.connect("multicard")
        self.assertEqual(self.offered(), ["multicard"])

    def test_multicard_is_shown_to_a_customer_as_a_bank_card(self):
        integration = self.connect("multicard")
        with translation.override("en"):
            self.assertEqual(str(integration.customer_label), "Bank card")
        self.assertTrue(integration.customer_logo_url.endswith("card.png"))

    def test_the_buttons_are_in_registry_order_not_connection_order(self):
        """The same method sits in the same place at every business."""
        for provider in ("octo", "click", "payme", "uzum"):
            self.connect(provider)
        self.assertEqual(self.offered(), ["payme", "click", "uzum", "octo"])

    def test_a_seller_who_is_not_live_is_offered_nothing(self):
        self.connect("payme")
        self.profile.status = SellerStatus.SUSPENDED
        self.profile.save(update_fields=["status"])
        self.assertEqual(self.offered(), [])


class OctoCheckoutTests(SellerTestCase):
    def test_the_card_form_is_in_the_customers_language(self):
        """Octo defaults to Uzbek, and it is where the card number is typed.

        A customer who has been reading the pay page in Russian must not be
        handed an Uzbek form at the one moment they are being careful.
        """
        integration = self.connect("octo")
        for code in ("uz", "ru", "en"):
            with self.subTest(code=code):
                self.assertEqual(
                    checkout_extra(integration, language=code)["language"], code
                )

    def test_a_language_octo_does_not_have_falls_back_to_uzbek(self):
        integration = self.connect("octo")
        self.assertEqual(
            checkout_extra(integration, language="kk")["language"], "uz"
        )

    def test_a_regional_code_is_reduced_to_its_language(self):
        integration = self.connect("octo")
        self.assertEqual(
            checkout_extra(integration, language="ru-RU")["language"], "ru"
        )

    def test_octo_is_told_who_is_being_paid(self):
        """It prints the description on the form and on the statement."""
        integration = self.connect("octo")
        self.assertEqual(checkout_extra(integration)["description"], "Anor Cafe")

    def test_the_language_follows_the_page_the_customer_was_reading(self):
        """End to end: the active language reaches the gateway call."""
        integration = self.connect("octo")
        captured = {}

        class FakeOcto:
            def __init__(self, **kwargs):
                pass

            def create_payment(self, **kwargs):
                captured.update(kwargs)
                return "https://secure.octo.uz/pay/abc"

        with patch("payments.services.import_string", return_value=FakeOcto):
            with translation.override("ru"):
                start_payment(self.profile, "octo", Decimal("50000"))

        self.assertEqual(captured["language"], "ru")
        self.assertEqual(captured["description"], "Anor Cafe")


# --- what comes back -----------------------------------------------------


def octo_signature(unique_key, uuid, status):
    return hashlib.sha1(f"{unique_key}{uuid}{status}".encode()).hexdigest().upper()


class OctoCallbackTests(SellerTestCase):
    """Driven through the real URL, so the whole stack is under test."""

    def setUp(self):
        super().setUp()
        self.integration = self.connect("octo")
        self.url = reverse("webhooks:provider", args=["octo", self.profile.uid])

    def paid_transaction(self, amount="50000", uuid="octo-uuid-1"):
        txn = Transaction.objects.create(
            seller=self.profile, provider="octo", amount=Decimal(amount)
        )
        txn.move_to(TransactionStatus.PENDING)
        self.callback(txn, "succeeded", amount=amount, uuid=uuid)
        txn.refresh_from_db()
        return txn

    def callback(self, txn, status, *, amount=None, uuid="octo-uuid-1", **extra):
        body = {
            "octo_payment_UUID": uuid,
            "shop_transaction_id": str(txn.pk),
            "status": status,
            "total_sum": float(amount if amount is not None else txn.amount),
            "octo_shop_id": CREDENTIALS["octo"]["octo_shop_id"],
            "signature": octo_signature(
                CREDENTIALS["octo"]["octo_unique_key"], uuid, status
            ),
        }
        body.update(extra)
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def test_a_successful_callback_marks_it_paid(self):
        txn = self.paid_transaction()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_declined_card_is_recorded_as_failed_not_cancelled(self):
        """Octo funnels canceled, failed and refunded into one hook.

        Taking that hook at face value filed a bank decline as "the customer
        changed their mind" — a different thing to a seller reading their day,
        and the row a support question starts from.
        """
        txn = Transaction.objects.create(
            seller=self.profile, provider="octo", amount=Decimal("50000")
        )
        txn.move_to(TransactionStatus.PENDING)
        response = self.callback(txn, "failed", uuid="octo-uuid-fail")
        self.assertEqual(response.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.FAILED)

    def test_a_customer_who_backed_out_is_recorded_as_cancelled(self):
        txn = Transaction.objects.create(
            seller=self.profile, provider="octo", amount=Decimal("50000")
        )
        txn.move_to(TransactionStatus.PENDING)
        self.callback(txn, "canceled", uuid="octo-uuid-cancel")
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.CANCELLED)

    def test_a_full_refund_marks_it_refunded(self):
        txn = self.paid_transaction()
        self.callback(txn, "refunded", refunded_sum=50000)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.REFUNDED)

    def test_a_refund_with_no_figure_is_taken_as_the_whole_payment(self):
        txn = self.paid_transaction(uuid="octo-uuid-2")
        self.callback(txn, "refunded", uuid="octo-uuid-2")
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.REFUNDED)

    def test_a_partial_refund_does_not_reverse_the_whole_payment(self):
        """The payment still stands for the rest of it.

        We have nowhere to record "half of it came back", and writing down a
        reversal that did not happen is worse than recording nothing: the
        seller would read 50 000 soum as returned when 10 000 was.
        """
        txn = self.paid_transaction(uuid="octo-uuid-3")
        response = self.callback(
            txn, "refunded", uuid="octo-uuid-3", refunded_sum=10000
        )
        self.assertEqual(response.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_partial_refund_is_still_written_down(self):
        txn = self.paid_transaction(uuid="octo-uuid-4")
        self.callback(txn, "refunded", uuid="octo-uuid-4", refunded_sum=10000)
        txn.refresh_from_db()
        self.assertEqual(txn.raw_payload.get("refunded_sum"), 10000)


class AmountGuardTests(SellerTestCase):
    """Our own last check on the one number that is money."""

    def test_a_transaction_is_not_marked_paid_for_a_different_amount(self):
        """Multicard's tolov handler never compares the amount at all.

        Five of the six do, and reject the callback before we see it. This is
        the sixth — and the guard for any provider added later, or any handler
        whose check changes underneath us.
        """
        integration = self.connect("multicard")
        txn = Transaction.objects.create(
            seller=self.profile, provider="multicard", amount=Decimal("50000")
        )
        txn.move_to(TransactionStatus.PENDING)

        view = MulticardWebhook()
        view.seller = self.profile
        view.integration = integration

        tolov_row = Mock(account_id=str(txn.pk), transaction_id="mc-1",
                         amount=Decimal("500"))
        view.successfully_payment({}, tolov_row)

        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PENDING)

    def test_the_matching_amount_goes_through(self):
        integration = self.connect("multicard")
        txn = Transaction.objects.create(
            seller=self.profile, provider="multicard", amount=Decimal("50000")
        )
        txn.move_to(TransactionStatus.PENDING)

        view = MulticardWebhook()
        view.seller = self.profile
        view.integration = integration

        tolov_row = Mock(account_id=str(txn.pk), transaction_id="mc-2",
                         amount=Decimal("50000"))
        view.successfully_payment({}, tolov_row)

        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_provider_that_repeats_no_amount_is_not_punished_for_it(self):
        """Paynet's Perform callback does not carry one."""
        integration = self.connect("multicard")
        txn = Transaction.objects.create(
            seller=self.profile, provider="multicard", amount=Decimal("50000")
        )
        txn.move_to(TransactionStatus.PENDING)

        view = MulticardWebhook()
        view.seller = self.profile
        view.integration = integration

        tolov_row = Mock(account_id=str(txn.pk), transaction_id="mc-3", amount=None)
        view.successfully_payment({}, tolov_row)

        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)


class MulticardWebhookTests(SellerTestCase):
    def test_the_handler_can_be_constructed_at_all(self):
        """It raised ImproperlyConfigured on every request until now.

        tolov's Multicard handler reads its callback secret in __init__ and
        refuses to exist without one. Ours are per seller, so settings carried
        none — and every Multicard callback answered 500 while the customer's
        money had already moved. A placeholder in settings gets past the
        check; setup() replaces it with the seller's.
        """
        MulticardWebhook()

    def test_a_callback_for_an_unknown_seller_is_a_404(self):
        response = self.client.post(
            reverse("webhooks:provider", args=["multicard", "nosuchseller"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_the_store_id_is_injected(self):
        """It is part of the signature and tolov compares it to the callback.

        Left at the settings placeholder, every real callback would be turned
        away as another store's.
        """
        integration = self.connect("multicard")
        view = MulticardWebhook()
        view.seller = self.profile
        view.integration = integration
        credentials = integration.webhook_credentials()
        for attribute, name in view.credential_map.items():
            if credentials.get(name):
                setattr(view, attribute, credentials[name])
        self.assertEqual(view.store_id, "888")
        self.assertEqual(view.secret, "mc-secret")

    # --- a real, signed callback through the real URL -------------------

    def signed_callback(self, txn, *, uuid="mc-uuid-1", tiyin=None, store_id=None):
        """Multicard signs md5(store_id + invoice_id + amount + secret).

        `amount` is tiyin on the wire — tolov divides it back down — so this
        also pins the unit our side of the conversation expects.
        """
        store_id = store_id or CREDENTIALS["multicard"]["store_id"]
        amount = tiyin if tiyin is not None else int(txn.amount * 100)
        raw = f"{store_id}{txn.pk}{amount}{CREDENTIALS['multicard']['secret']}"
        body = {
            "store_id": store_id,
            "invoice_id": txn.pk,
            "amount": amount,
            "uuid": uuid,
            "sign": hashlib.md5(raw.encode()).hexdigest(),
        }
        return self.client.post(
            reverse("webhooks:provider", args=["multicard", self.profile.uid]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def pending(self, amount="50000"):
        txn = Transaction.objects.create(
            seller=self.profile, provider="multicard", amount=Decimal(amount)
        )
        txn.move_to(TransactionStatus.PENDING)
        return txn

    def test_a_signed_callback_marks_the_payment_paid(self):
        self.connect("multicard")
        txn = self.pending()
        response = self.signed_callback(txn)
        self.assertEqual(response.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_an_unsigned_callback_is_refused(self):
        self.connect("multicard")
        txn = self.pending()
        response = self.client.post(
            reverse("webhooks:provider", args=["multicard", self.profile.uid]),
            data=json.dumps(
                {"store_id": "888", "invoice_id": txn.pk, "amount": 5000000,
                 "uuid": "mc-x", "sign": "0" * 32}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PENDING)

    def test_a_callback_for_the_wrong_amount_does_not_mark_it_paid(self):
        """Correctly signed, and still wrong. tolov's handler would take it."""
        self.connect("multicard")
        txn = self.pending()
        response = self.signed_callback(txn, tiyin=100)
        self.assertEqual(response.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PENDING)

    def test_the_same_callback_twice_is_harmless(self):
        self.connect("multicard")
        txn = self.pending()
        self.signed_callback(txn)
        self.signed_callback(txn)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_another_sellers_transaction_cannot_be_claimed(self):
        """The invoice id travels through the provider and back."""
        self.connect("multicard")
        other = register_seller(
            phone="+998 90 111 22 33",
            full_name="Bek",
            business_name="Boshqa",
            password=PASSWORD,
        )
        other_profile = SellerProfile.objects.get(user=other)
        other_profile.approve()
        theirs = Transaction.objects.create(
            seller=other_profile, provider="multicard", amount=Decimal("50000")
        )
        theirs.move_to(TransactionStatus.PENDING)

        response = self.signed_callback(theirs)
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, TransactionStatus.PENDING)

    def test_an_invoice_that_does_not_exist_is_answered_not_crashed(self):
        """A 500 is a provider retrying the same impossible thing for hours."""
        self.connect("multicard")
        raw = f"888999999500000{CREDENTIALS['multicard']['secret']}"
        response = self.client.post(
            reverse("webhooks:provider", args=["multicard", self.profile.uid]),
            data=json.dumps(
                {"store_id": "888", "invoice_id": 999999, "amount": 500000,
                 "uuid": "mc-ghost",
                 "sign": hashlib.md5(raw.encode()).hexdigest()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
