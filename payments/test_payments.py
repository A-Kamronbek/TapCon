"""Taking a payment, end to end, with no provider on the other end.

There are no sandbox credentials yet, so every provider call is faked: the
gateway is a stub returning a checkout URL, and the webhooks are driven with
hand-built Payme JSON-RPC payloads. That is enough to test everything that is
ours — the state machine, the per-seller credential resolution, idempotency,
and the seller isolation — and it is the part most likely to be wrong.

What it cannot test is whether Payme accepts our checkout link. That needs
credentials and is the first thing to do when they arrive.
"""
import base64
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import InvalidTransition, Transaction, TransactionStatus
from .services import (
    PaymentError,
    save_credentials,
    set_enabled,
    set_test_mode,
    start_payment,
)

PASSWORD = "s3cretpw!x"
PAYME_ID = "62f1a5c0d8e9b2"
PAYME_KEY = "test-payme-key-0099"


class FakeGateway:
    """Stands in for tolov's client. Records what it was built with."""

    built_with = {}
    called_with = {}
    checkout_url = "https://checkout.paycom.uz/base64blob"
    raises = None

    def __init__(self, **kwargs):
        FakeGateway.built_with = kwargs

    def create_payment(self, **kwargs):
        FakeGateway.called_with = kwargs
        if FakeGateway.raises:
            raise FakeGateway.raises
        return FakeGateway.checkout_url


class PaymentTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

        FakeGateway.raises = None
        FakeGateway.checkout_url = "https://checkout.paycom.uz/base64blob"

        self.user = register_seller(
            phone="+998 90 123 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=self.user)
        self.profile.approve()

        save_credentials(
            self.profile, "payme", {"payme_id": PAYME_ID, "payme_key": PAYME_KEY}
        )
        set_enabled(self.profile, "payme", True)

    def gateway(self):
        return patch("payments.services.import_string", return_value=FakeGateway)


class StateMachineTests(PaymentTestCase):
    def make(self, status=TransactionStatus.CREATED):
        return Transaction.objects.create(
            seller=self.profile, provider="payme", amount=Decimal("5000"), status=status
        )

    def test_a_payment_moves_forward(self):
        txn = self.make()
        self.assertTrue(txn.move_to(TransactionStatus.PENDING))
        self.assertTrue(txn.move_to(TransactionStatus.PAID))
        self.assertIsNotNone(txn.paid_at)

    def test_a_payment_never_moves_backwards(self):
        txn = self.make(TransactionStatus.PAID)
        with self.assertRaises(InvalidTransition):
            txn.move_to(TransactionStatus.PENDING)

    def test_a_paid_payment_cannot_be_cancelled_by_a_late_webhook(self):
        """Reversing a paid payment is a refund, not an edit to this row."""
        txn = self.make(TransactionStatus.PAID)
        with self.assertRaises(InvalidTransition):
            txn.move_to(TransactionStatus.CANCELLED)

    def test_arriving_at_the_same_status_twice_is_a_no_op(self):
        """Providers retry webhooks. A retry must not raise."""
        txn = self.make(TransactionStatus.PAID)
        self.assertFalse(txn.move_to(TransactionStatus.PAID))

    def test_every_transaction_gets_an_unguessable_reference(self):
        one, two = self.make(), self.make()
        self.assertNotEqual(one.uid, two.uid)
        self.assertGreaterEqual(len(one.uid), 20)

    def test_a_cancel_cannot_land_on_top_of_a_payment(self):
        """The webhook race that would cost a seller money.

        A Perform and a Cancel callback can be in flight at once. Deciding
        from a stale in-memory status would write `cancelled` over a payment
        that was already taken — so the decision is made from the row as the
        database has it, under a lock.
        """
        txn = self.make(TransactionStatus.PENDING)
        stale = Transaction.objects.get(pk=txn.pk)   # another worker's copy

        txn.move_to(TransactionStatus.PAID)          # the Perform lands first

        with self.assertRaises(InvalidTransition):
            stale.move_to(TransactionStatus.CANCELLED)

        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_stale_duplicate_of_a_paid_row_is_a_no_op_not_an_error(self):
        """Two Perform callbacks racing is the ordinary case, not a fault."""
        txn = self.make(TransactionStatus.PENDING)
        stale = Transaction.objects.get(pk=txn.pk)

        txn.move_to(TransactionStatus.PAID)
        self.assertFalse(stale.move_to(TransactionStatus.PAID))
        self.assertEqual(stale.status, TransactionStatus.PAID)

    def test_sandbox_money_is_never_counted_as_real(self):
        from django.db.models import Sum

        Transaction.objects.create(
            seller=self.profile, provider="payme", amount=Decimal("1000"),
            status=TransactionStatus.PAID, is_test_mode=True,
        )
        Transaction.objects.create(
            seller=self.profile, provider="payme", amount=Decimal("7000"),
            status=TransactionStatus.PAID, is_test_mode=False,
        )
        total = Transaction.objects.real().paid().aggregate(t=Sum("amount"))["t"]
        self.assertEqual(total, Decimal("7000"))


class StartPaymentTests(PaymentTestCase):
    def test_the_gateway_is_built_with_this_sellers_credentials(self):
        with self.gateway():
            txn, url = start_payment(self.profile, "payme", Decimal("25000"))

        self.assertEqual(FakeGateway.built_with["payme_id"], PAYME_ID)
        self.assertEqual(FakeGateway.built_with["payme_key"], PAYME_KEY)
        self.assertTrue(FakeGateway.built_with["is_test_mode"])
        self.assertEqual(url, FakeGateway.checkout_url)
        self.assertEqual(txn.status, TransactionStatus.PENDING)

    def test_the_account_field_matches_the_webhook_setting(self):
        """If these two disagree, every real payment fails at the first call."""
        from django.conf import settings

        with self.gateway():
            start_payment(self.profile, "payme", Decimal("25000"))

        self.assertEqual(
            FakeGateway.called_with["account_field_name"],
            settings.TOLOV["PAYME"]["ACCOUNT_FIELD"],
        )

    def test_the_provider_is_given_our_primary_key(self):
        with self.gateway():
            txn, _url = start_payment(self.profile, "payme", Decimal("25000"))
        self.assertEqual(FakeGateway.called_with["id"], txn.pk)

    def test_an_unapproved_seller_cannot_take_money(self):
        self.profile.suspend()
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("25000"))

    def test_a_provider_that_is_off_cannot_take_money(self):
        set_enabled(self.profile, "payme", False)
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("25000"))

    def test_an_amount_below_the_sellers_minimum_is_refused(self):
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("1"))

    def test_an_amount_above_the_sellers_maximum_is_refused(self):
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("999999999"))

    def test_a_fractional_amount_is_refused(self):
        """It would reach the provider as a fractional tiyin, and what they do
        with it decides what the customer is actually charged."""
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("25000.50"))
        self.assertFalse(Transaction.objects.exists())

    def test_a_zero_amount_is_refused_even_if_the_seller_allows_it(self):
        self.profile.min_amount = Decimal("0")
        self.profile.save(update_fields=["min_amount"])
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("0"))

    def test_a_negative_amount_is_refused(self):
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("-5000"))

    def test_the_amount_reaching_the_provider_is_the_amount_we_stored(self):
        """The one number that must never drift between our row and theirs."""
        with self.gateway():
            txn, _url = start_payment(self.profile, "payme", Decimal("25000"))
        self.assertEqual(FakeGateway.called_with["amount"], txn.amount)
        self.assertEqual(txn.amount, Decimal("25000"))

    def test_a_sandbox_integration_marks_the_transaction_as_test(self):
        with self.gateway():
            txn, _url = start_payment(self.profile, "payme", Decimal("25000"))
        self.assertTrue(txn.is_test_mode)

        set_test_mode(self.profile, "payme", False)
        with self.gateway():
            txn, _url = start_payment(self.profile, "payme", Decimal("25000"))
        self.assertFalse(txn.is_test_mode)

    def test_a_provider_outage_leaves_a_failed_row_not_a_gap(self):
        FakeGateway.raises = RuntimeError("connection refused")
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("25000"))

        txn = Transaction.objects.get()
        self.assertEqual(txn.status, TransactionStatus.FAILED)

    def test_the_providers_own_error_never_reaches_the_customer(self):
        """It can name keys, hosts and account ids."""
        FakeGateway.raises = RuntimeError(f"bad merchant key {PAYME_KEY}")
        with self.gateway():
            try:
                start_payment(self.profile, "payme", Decimal("25000"))
            except PaymentError as exc:
                self.assertNotIn(PAYME_KEY, str(exc))
            else:
                self.fail("expected PaymentError")

    def test_an_empty_checkout_url_is_treated_as_a_failure(self):
        FakeGateway.checkout_url = ""
        with self.gateway(), self.assertRaises(PaymentError):
            start_payment(self.profile, "payme", Decimal("25000"))
        self.assertEqual(Transaction.objects.get().status, TransactionStatus.FAILED)


class PayPageTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("payments:pay", args=[self.profile.uid])

    def test_the_page_offers_the_enabled_provider(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Payme")
        self.assertContains(
            response, reverse("payments:go", args=[self.profile.uid, "payme"])
        )

    def test_a_disabled_provider_is_not_offered(self):
        set_enabled(self.profile, "payme", False)
        response = self.client.get(self.url)
        self.assertNotContains(
            response, reverse("payments:go", args=[self.profile.uid, "payme"])
        )

    def test_an_unapproved_seller_shows_the_gate_not_the_form(self):
        self.profile.suspend()
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="amount"')

    def test_choosing_a_provider_redirects_to_the_checkout(self):
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "25000"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, FakeGateway.checkout_url)

    def test_the_return_url_points_at_this_transactions_result_page(self):
        with self.gateway():
            self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "25000"},
            )
        txn = Transaction.objects.get()
        self.assertIn(
            reverse("payments:result", args=[self.profile.uid, txn.uid]),
            FakeGateway.called_with["return_url"],
        )

    def test_a_bad_amount_comes_back_with_a_message_and_no_transaction(self):
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "1"},
                follow=True,
            )
        self.assertContains(response, "field-error")
        self.assertFalse(Transaction.objects.exists())

    def test_a_failure_puts_the_customer_back_on_the_pay_page(self):
        """Not on /go/<provider>/, which answers only POST.

        Rendering the error there left that URL in the address bar, so the
        language switcher posted `next=/pay/<uid>/go/payme/` and the customer
        got a 405 for changing language after a failed payment. A reload or
        the Back button did the same.
        """
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)

    def test_a_provider_failure_puts_the_customer_back_on_the_pay_page(self):
        FakeGateway.raises = RuntimeError("connection refused")
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "25000"},
                follow=True,
            )
        self.assertEqual(response.redirect_chain[-1][0], self.url)
        self.assertContains(response, "toast-error")

    def test_the_amount_is_still_there_after_a_failure(self):
        """Nobody should have to retype what they just typed."""
        FakeGateway.raises = RuntimeError("connection refused")
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "25000"},
                follow=True,
            )
        self.assertContains(response, 'value="25000"')

    def test_the_message_is_gone_on_the_next_visit(self):
        """It belongs to that attempt, not to the page."""
        with self.gateway():
            self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "1"},
                follow=True,
            )
        self.assertNotContains(self.client.get(self.url), "field-error")

    def test_a_get_to_the_go_url_is_sent_to_the_pay_page_not_refused(self):
        """This URL is in history on the way to the provider.

        A Back button, a reload or a prefetcher will ask for it, and a 405 in
        a customer's address bar is a dead end they cannot act on.
        """
        response = self.client.get(
            reverse("payments:go", args=[self.profile.uid, "payme"])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)

    def test_a_get_to_the_go_url_creates_nothing(self):
        """Which is the reason it was POST-only in the first place."""
        self.client.get(reverse("payments:go", args=[self.profile.uid, "payme"]))
        self.assertFalse(Transaction.objects.exists())

    def test_changing_language_after_a_failed_payment_works(self):
        """The bug exactly as a customer met it."""
        with self.gateway():
            response = self.client.post(
                reverse("payments:go", args=[self.profile.uid, "payme"]),
                {"amount": "1"},
                follow=True,
            )
        # Whatever the page offers as the switcher's return path must be a
        # URL that answers GET.
        self.assertEqual(response.request["PATH_INFO"], self.url)
        switched = self.client.post(
            "/i18n/setlang/", {"language": "ru", "next": self.url}, follow=True
        )
        self.assertEqual(switched.status_code, 200)

    def test_the_pay_page_carries_the_support_contacts(self):
        response = self.client.get(self.url)
        self.assertContains(response, "tel:")

    def test_an_unknown_seller_is_a_plain_404(self):
        self.assertEqual(self.client.get("/pay/zzzzzz/").status_code, 404)


class ResultPageTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        with self.gateway():
            self.txn, _url = start_payment(self.profile, "payme", Decimal("25000"))
        self.url = reverse("payments:result", args=[self.profile.uid, self.txn.uid])

    def test_waiting_while_the_webhook_is_in_flight(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-settled=\"0\"")

    def test_a_paid_payment_says_so_and_stops_polling(self):
        self.txn.move_to(TransactionStatus.PAID)
        response = self.client.get(self.url)
        self.assertContains(response, "data-settled=\"1\"")
        self.assertNotContains(response, "payment-status.js")

    def test_the_status_endpoint_answers_the_poll(self):
        url = reverse("payments:status", args=[self.profile.uid, self.txn.uid])
        data = self.client.get(url).json()
        self.assertEqual(data["status"], TransactionStatus.PENDING)
        self.assertFalse(data["settled"])

        self.txn.move_to(TransactionStatus.PAID)
        data = self.client.get(url).json()
        self.assertTrue(data["settled"])
        self.assertTrue(data["paid"])

    def test_another_sellers_transaction_is_not_reachable(self):
        other = register_seller(
            phone="+998 90 999 88 77",
            full_name="Bob",
            business_name="Bob Cafe",
            password=PASSWORD,
        )
        other_profile = SellerProfile.objects.get(user=other)
        url = reverse("payments:result", args=[other_profile.uid, self.txn.uid])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_paid_payment_is_settled_even_though_a_refund_could_follow(self):
        """"Settled" and "final" are different questions.

        A paid payment can still be refunded, so it is not final — but the
        customer's payment is over and the page must stop polling. Reading
        finality here left every phone polling for ever after a successful
        payment.
        """
        self.txn.move_to(TransactionStatus.PAID)
        self.assertTrue(self.txn.is_settled)
        self.assertFalse(self.txn.is_final)

        url = reverse("payments:status", args=[self.profile.uid, self.txn.uid])
        self.assertTrue(self.client.get(url).json()["settled"])

    def test_the_result_page_is_not_addressed_by_primary_key(self):
        """Sequential ids would let anyone read the next customer's payment."""
        self.assertNotIn(f"/{self.txn.pk}/", self.url)
