"""A payment that is over must never be started again.

Both of these took real money in a way nobody would have noticed.

tolov refuses a second attempt only while a *non-final* transaction exists
for the account — `SUCCESSFULLY` and `CANCELLED` are explicitly excluded, so
it opens a new one happily. The sequence is two taps: the checkout URL
carries our transaction id and stays in the browser's history, so cancelling
and then going Back and paying does it.

What happened then depended on the status:

* **cancelled → paid** is refused by the transition table, so the customer was
  charged, the provider was answered "performed", and the seller was never
  credited.
* **paid → paid** is a no-op, so the second charge was absorbed and the
  ledger showed one payment.

Neither raised, neither 500'd, and both answered the provider with success.
"""
import base64
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from accounts.services import register_seller
from merchants.models import SellerProfile
from payments.models import Transaction, TransactionStatus
from payments.services import save_credentials, set_enabled, set_test_mode

AMOUNT = Decimal("50000")


class SettledTransactionGuardTests(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone="+998 90 123 45 67", full_name="Ali",
            business_name="Anor Cafe", password="s3cretpw!x",
        )
        self.seller = SellerProfile.objects.get(user=user)
        self.seller.approve()
        save_credentials(self.seller, "payme",
                         {"payme_id": "p-id", "payme_key": "p-key"})
        set_enabled(self.seller, "payme", True)
        set_test_mode(self.seller, "payme", False)
        self.url = f"/webhooks/payme/{self.seller.uid}/"
        self.auth = {
            "HTTP_AUTHORIZATION": "Basic "
            + base64.b64encode(b"Paycom:p-key").decode()
        }

    def transaction(self, status):
        txn = Transaction.objects.create(
            seller=self.seller, provider="payme", amount=AMOUNT,
            status=TransactionStatus.PENDING, is_test_mode=False,
        )
        if status != TransactionStatus.PENDING:
            Transaction.objects.filter(pk=txn.pk).update(status=status)
            txn.refresh_from_db()
        return txn

    def rpc(self, method, params):
        return self.client.post(
            self.url,
            json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": method, "params": params}),
            content_type="application/json", **self.auth,
        ).json()

    def start_payment_on(self, txn, provider_id):
        return self.rpc("CreateTransaction", {
            "id": provider_id, "time": 1756800000000,
            "amount": int(AMOUNT * 100), "account": {"id": str(txn.pk)},
        })

    # --- the refusal, before any money moves ---------------------------

    def test_a_cancelled_payment_cannot_be_started_again(self):
        txn = self.transaction(TransactionStatus.CANCELLED)
        response = self.start_payment_on(txn, "a" * 24)
        self.assertIn("error", response,
                      "the customer would have been charged for a payment "
                      "that can never be recorded")
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.CANCELLED)

    def test_a_failed_payment_cannot_be_started_again(self):
        txn = self.transaction(TransactionStatus.FAILED)
        self.assertIn("error", self.start_payment_on(txn, "b" * 24))

    def test_a_paid_payment_cannot_be_paid_twice(self):
        txn = self.transaction(TransactionStatus.PENDING)
        self.start_payment_on(txn, "c" * 24)
        self.rpc("PerformTransaction", {"id": "c" * 24})
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

        second = self.start_payment_on(txn, "d" * 24)
        self.assertIn("error", second, "a second charge was allowed")

    def test_a_refunded_payment_cannot_be_started_again(self):
        txn = self.transaction(TransactionStatus.REFUNDED)
        self.assertIn("error", self.start_payment_on(txn, "e" * 24))

    # --- what must still work ------------------------------------------

    def test_a_pending_payment_is_still_accepted(self):
        """The guard must not refuse the payment it exists to protect."""
        txn = self.transaction(TransactionStatus.PENDING)
        created = self.start_payment_on(txn, "f" * 24)
        self.assertIn("result", created)
        performed = self.rpc("PerformTransaction", {"id": "f" * 24})
        self.assertIn("result", performed)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_replayed_callback_on_a_paid_payment_is_still_harmless(self):
        """The provider retrying the *same* payment must stay a no-op."""
        txn = self.transaction(TransactionStatus.PENDING)
        self.start_payment_on(txn, "g" * 24)
        self.rpc("PerformTransaction", {"id": "g" * 24})
        again = self.rpc("PerformTransaction", {"id": "g" * 24})
        self.assertIn("result", again)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)
        self.assertEqual(txn.provider_txn_id, "g" * 24)


class MulticardReplayTests(TestCase):
    """Multicard reaches `_find_account` on every callback, not just new ones.

    The settled-transaction guard lives in `_find_account` because for five of
    the six providers, getting there means a *new* provider transaction is
    being opened. Multicard is the exception: it looks the account up before
    its own deduplication runs, so the guard turned an ordinary retry of an
    already-recorded payment into a 404 — which a provider can read as
    "unknown invoice, stop retrying", and then a payment that succeeded is
    never confirmed.
    """

    def setUp(self):
        import hashlib

        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone="+998 90 123 45 67", full_name="Ali",
            business_name="Anor Cafe", password="s3cretpw!x",
        )
        self.seller = SellerProfile.objects.get(user=user)
        self.seller.approve()
        self.store_id, self.secret = "5511", "mc-secret"
        save_credentials(self.seller, "multicard", {
            "application_id": "app-1", "secret": self.secret,
            "store_id": self.store_id,
        })
        set_enabled(self.seller, "multicard", True)
        set_test_mode(self.seller, "multicard", False)
        self.md5 = hashlib.md5

    def callback(self, txn, uuid):
        tiyin = int(AMOUNT * 100)
        raw = f"{self.store_id}{txn.pk}{tiyin}{self.secret}"
        return self.client.post(
            f"/webhooks/multicard/{self.seller.uid}/",
            json.dumps({
                "store_id": self.store_id, "invoice_id": str(txn.pk),
                "amount": tiyin, "uuid": uuid,
                "sign": self.md5(raw.encode()).hexdigest(),
            }),
            content_type="application/json",
        )

    def transaction(self):
        return Transaction.objects.create(
            seller=self.seller, provider="multicard", amount=AMOUNT,
            status=TransactionStatus.PENDING, is_test_mode=False,
        )

    def test_a_retried_callback_is_still_accepted(self):
        txn = self.transaction()
        first = self.callback(txn, "mc-uuid-1")
        self.assertEqual(first.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

        again = self.callback(txn, "mc-uuid-1")
        self.assertEqual(
            again.status_code, 200,
            "a retry of the payment we already recorded was refused",
        )
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_different_uuid_on_a_paid_row_is_still_refused(self):
        """The guard must survive the exemption that makes replays work."""
        txn = self.transaction()
        self.callback(txn, "mc-uuid-1")
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.PAID)

        second = self.callback(txn, "mc-uuid-2")
        self.assertEqual(second.status_code, 404, "a second charge got through")
        txn.refresh_from_db()
        self.assertEqual(txn.provider_txn_id, "mc-uuid-1")


class DoubleChargeIsShoutedAboutTests(TestCase):
    """If money moves anyway, it must be impossible to miss."""

    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone="+998 90 123 45 67", full_name="Ali",
            business_name="Anor Cafe", password="s3cretpw!x",
        )
        self.seller = SellerProfile.objects.get(user=user)
        self.seller.approve()

    def test_a_second_provider_id_on_a_paid_row_logs_critical(self):
        from payments.webhooks import PaymeWebhook

        txn = Transaction.objects.create(
            seller=self.seller, provider="payme", amount=AMOUNT,
            status=TransactionStatus.PENDING, is_test_mode=False,
        )
        txn.move_to(TransactionStatus.PAID, provider_txn_id="first-id")

        handler = PaymeWebhook()
        handler.seller = self.seller

        class FakeTolovTransaction:
            transaction_id = "second-id"
            account_id = txn.pk
            amount = AMOUNT

        with self.assertLogs("payments", level="CRITICAL") as captured:
            handler._advance(FakeTolovTransaction(), TransactionStatus.PAID, {})

        message = "\n".join(captured.output)
        self.assertIn("charged twice", message)
        self.assertIn(txn.uid, message)
        txn.refresh_from_db()
        # The first payment's id is kept — it is the one that is recorded.
        self.assertEqual(txn.provider_txn_id, "first-id")
        self.assertIn("double_charge", txn.raw_payload)
