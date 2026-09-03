"""Webhooks, driven with hand-built Payme JSON-RPC payloads.

The point of these is the thing tolov was not written for: TapCon has a
different merchant account per seller, so each request must authenticate
against *that* seller's key and may only touch *that* seller's payments.

Payme's protocol is used because it is the one going live first. The
credential resolution being tested is shared by all six providers.
"""
import base64
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from tolov.integrations.django.models import PaymentTransaction

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import Transaction, TransactionStatus
from .services import save_credentials, set_enabled

PASSWORD = "s3cretpw!x"


def payme_auth(key, merchant="Paycom"):
    raw = f"{merchant}:{key}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class WebhookTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

        self.alice, self.alice_profile = self.make_seller(
            "+998 90 111 11 11", "Anor Cafe", "alice-payme-key"
        )
        self.bob, self.bob_profile = self.make_seller(
            "+998 90 222 22 22", "Bob Cafe", "bob-payme-key"
        )

        self.alice_txn = Transaction.objects.create(
            seller=self.alice_profile,
            provider="payme",
            amount=Decimal("25000"),
            status=TransactionStatus.PENDING,
        )

    def make_seller(self, phone, name, payme_key):
        user = register_seller(
            phone=phone, full_name=name, business_name=name, password=PASSWORD
        )
        profile = SellerProfile.objects.get(user=user)
        profile.approve()
        save_credentials(
            profile, "payme", {"payme_id": f"id-{name}", "payme_key": payme_key}
        )
        set_enabled(profile, "payme", True)
        return user, profile

    def url(self, profile):
        return reverse("webhooks:provider", args=["payme", profile.uid])

    def call(self, profile, method, params, key=None, merchant="Paycom"):
        return self.client.post(
            self.url(profile),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
            content_type="application/json",
            headers={"authorization": payme_auth(key or "alice-payme-key", merchant)},
        )


class AuthenticationTests(WebhookTestCase):
    def test_the_sellers_own_key_is_accepted(self):
        response = self.call(
            self.alice_profile,
            "CheckPerformTransaction",
            {"account": {"id": self.alice_txn.pk}, "amount": 2500000},
            key="alice-payme-key",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["allow"])

    def test_another_sellers_key_is_refused(self):
        """The whole point of the per-seller resolution."""
        response = self.call(
            self.alice_profile,
            "CheckPerformTransaction",
            {"account": {"id": self.alice_txn.pk}, "amount": 2500000},
            key="bob-payme-key",
        )
        self.assertIn("error", response.json())

    def test_a_request_with_no_authorization_header_is_refused(self):
        response = self.client.post(
            self.url(self.alice_profile),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "CheckPerformTransaction",
                             "params": {"account": {"id": self.alice_txn.pk},
                                        "amount": 2500000}}),
            content_type="application/json",
        )
        self.assertIn("error", response.json())

    def test_an_empty_key_is_refused(self):
        response = self.call(
            self.alice_profile,
            "CheckPerformTransaction",
            {"account": {"id": self.alice_txn.pk}, "amount": 2500000},
            key="wrong",
        )
        self.assertIn("error", response.json())

    def test_an_unknown_seller_uid_is_a_404(self):
        response = self.client.post(
            reverse("webhooks:provider", args=["payme", "zzzzzz"]),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "CheckPerformTransaction", "params": {}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_switching_a_provider_off_does_not_reject_its_callbacks(self):
        """Money that already moved must still be recorded.

        `is_enabled` decides whether the pay page offers a provider. If it
        also gated the webhook, a seller switching Payme off a minute after a
        customer paid would have that confirmation rejected — and the payment
        would sit as `pending` for ever while the money had actually moved.
        """
        set_enabled(self.alice_profile, "payme", False)
        self.call(
            self.alice_profile,
            "CreateTransaction",
            {"id": "late-txn", "time": 1, "account": {"id": self.alice_txn.pk},
             "amount": 2500000},
        )
        response = self.call(
            self.alice_profile, "PerformTransaction", {"id": "late-txn"}
        )
        self.assertNotIn("error", response.json())
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.PAID)

    def test_a_provider_with_no_credentials_at_all_is_a_404(self):
        from payments.models import ProviderIntegration

        ProviderIntegration.objects.filter(
            seller=self.alice_profile, provider="payme"
        ).update(credentials_blob="")
        response = self.call(
            self.alice_profile, "CheckPerformTransaction", {"account": {"id": 1}}
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_provider_name_is_a_404(self):
        response = self.client.post(
            reverse("webhooks:provider", args=["notaprovider", self.alice_profile.uid]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_webhooks_are_exempt_from_csrf(self):
        """A provider is not a browser and carries no token."""
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(
            self.url(self.alice_profile),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "CheckPerformTransaction",
                             "params": {"account": {"id": self.alice_txn.pk}, "amount": 2500000}}),
            content_type="application/json",
            headers={"authorization": payme_auth("alice-payme-key")},
        )
        self.assertEqual(response.status_code, 200)


class SellerIsolationTests(WebhookTestCase):
    def test_a_seller_cannot_reach_another_sellers_transaction(self):
        """Bob holds valid credentials of his own. The account id travels
        through the provider, so without scoping he could name Alice's."""
        response = self.call(
            self.bob_profile,
            "CheckPerformTransaction",
            {"account": {"id": self.alice_txn.pk}, "amount": 2500000},
            key="bob-payme-key",
        )
        self.assertIn("error", response.json())
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.PENDING)

    def test_and_writes_nothing_down_when_it_refuses(self):
        self.call(
            self.bob_profile,
            "CreateTransaction",
            {"id": "bob-attempt", "time": 1, "account": {"id": self.alice_txn.pk},
             "amount": 2500000},
            key="bob-payme-key",
        )
        self.assertFalse(PaymentTransaction.objects.exists())


class PaymentFlowTests(WebhookTestCase):
    def create(self, transaction_id="payme-txn-1"):
        return self.call(
            self.alice_profile,
            "CreateTransaction",
            {"id": transaction_id, "time": 1735689600000,
             "account": {"id": self.alice_txn.pk}, "amount": 2500000},
        )

    def perform(self, transaction_id="payme-txn-1"):
        return self.call(
            self.alice_profile, "PerformTransaction", {"id": transaction_id}
        )

    def test_the_amount_must_match_ours(self):
        response = self.call(
            self.alice_profile,
            "CheckPerformTransaction",
            {"account": {"id": self.alice_txn.pk}, "amount": 100},
        )
        self.assertIn("error", response.json())

    def test_a_successful_payment_marks_our_transaction_paid(self):
        self.create()
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.PENDING)

        response = self.perform()
        self.assertNotIn("error", response.json())

        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.PAID)
        self.assertIsNotNone(self.alice_txn.paid_at)
        self.assertEqual(self.alice_txn.provider_txn_id, "payme-txn-1")

    def test_a_replayed_webhook_changes_nothing(self):
        """Providers retry. The second call must be as harmless as the first."""
        self.create()
        self.perform()
        self.alice_txn.refresh_from_db()
        paid_at = self.alice_txn.paid_at

        response = self.perform()
        self.assertNotIn("error", response.json())

        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.PAID)
        self.assertEqual(self.alice_txn.paid_at, paid_at)
        self.assertEqual(PaymentTransaction.objects.count(), 1)

    def test_a_cancelled_payment_marks_ours_cancelled(self):
        self.create()
        response = self.call(
            self.alice_profile,
            "CancelTransaction",
            {"id": "payme-txn-1", "reason": 3},
        )
        self.assertNotIn("error", response.json())
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.CANCELLED)

    def test_an_unknown_method_is_refused_politely(self):
        response = self.call(self.alice_profile, "DoSomethingElse", {})
        self.assertIn("error", response.json())
        self.assertEqual(response.status_code, 200)

    def test_a_callback_that_contradicts_a_settled_payment_never_500s(self):
        """A Perform arriving after a Cancel, or any other impossible order.

        Answering 500 would make the provider retry the same impossible thing
        for hours. The payment stays as it was and the disagreement is logged.
        """
        self.create()
        self.call(
            self.alice_profile, "CancelTransaction", {"id": "payme-txn-1", "reason": 3}
        )
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.CANCELLED)

        with self.assertLogs("payments", level="ERROR"):
            response = self.perform()

        self.assertLess(response.status_code, 500)
        self.alice_txn.refresh_from_db()
        self.assertEqual(self.alice_txn.status, TransactionStatus.CANCELLED)
