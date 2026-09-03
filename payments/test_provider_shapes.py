"""Each provider's real callback shape, not Payme's used six times.

Every webhook test written before Phase 8 used Payme's JSON-RPC envelope,
because that is the one that was built first. That hid two defects for
months, both of which would have cost a seller real money:

* **Uzum answered 500 to everything.** Its Biller API puts the operation in
  the path and tolov's handler reads it as a view argument; our URLconf never
  supplied one, so the handler raised TypeError before any of our code ran.
* **A declined Click payment crashed the callback** and left our transaction
  `pending` for ever, so the customer's result page polled "waiting" instead
  of telling them the card was refused.

Neither is visible from a passing test suite that only speaks Payme. So each
provider is exercised here in the shape it actually sends: its own body
format, its own authentication, its own units, its own field names.
"""
import base64
import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from tolov.integrations.django.models import PaymentTransaction

from accounts.services import register_seller
from merchants.models import SellerProfile
from payments.models import Transaction, TransactionStatus
from payments.patches import _coerce_reason
from payments.services import save_credentials, set_enabled, set_test_mode

AMOUNT = Decimal("50000")
SIGN_TIME = "2026-09-02 12:00:00"

CREDENTIALS = {
    "payme": {"payme_id": "t-payme-id", "payme_key": "t-payme-key"},
    "click": {"service_id": "12345", "merchant_id": "9001",
              "merchant_user_id": "9002", "secret_key": "t-click-secret"},
    "uzum": {"service_id": "54321", "username": "t-uzum-user",
             "password": "t-uzum-pass"},
    "paynet": {"merchant_id": "7001", "service_id": "260",
               "username": "t-paynet-user", "password": "t-paynet-pass"},
}


class ProviderShapeTestCase(TestCase):
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
        for provider, credentials in CREDENTIALS.items():
            save_credentials(self.seller, provider, credentials)
            set_enabled(self.seller, provider, True)
            set_test_mode(self.seller, provider, False)

    def transaction(self, provider):
        return Transaction.objects.create(
            seller=self.seller, provider=provider, amount=AMOUNT,
            status=TransactionStatus.PENDING, is_test_mode=False,
        )

    def basic(self, user, password):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"HTTP_AUTHORIZATION": f"Basic {token}"}

    def url(self, provider, suffix=""):
        return f"/webhooks/{provider}/{self.seller.uid}/{suffix}"


class PaymeShapeTests(ProviderShapeTestCase):
    """JSON-RPC, Basic auth on the key only, amounts in tiyin."""

    def rpc(self, method, params, rpc_id=1):
        return self.client.post(
            self.url("payme"),
            json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                        "method": method, "params": params}),
            content_type="application/json",
            **self.basic("Paycom", CREDENTIALS["payme"]["payme_key"]),
        )

    def test_a_performed_payment_is_recorded(self):
        txn = self.transaction("payme")
        self.rpc("CreateTransaction", {
            "id": "aaaa1111bbbb2222cccc3333", "time": 1756800000000,
            "amount": int(AMOUNT * 100), "account": {"id": str(txn.pk)},
        })
        response = self.rpc("PerformTransaction",
                            {"id": "aaaa1111bbbb2222cccc3333"})
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_the_amount_is_read_as_tiyin(self):
        """Passing soum where tiyin is expected would take 1% of the bill."""
        txn = self.transaction("payme")
        response = self.rpc("CreateTransaction", {
            "id": "dddd4444eeee5555ffff6666", "time": 1756800000000,
            "amount": int(AMOUNT),  # soum, i.e. a hundredth of the real charge
            "account": {"id": str(txn.pk)},
        })
        txn.refresh_from_db()
        self.assertIn("error", response.json())
        self.assertEqual(txn.status, TransactionStatus.PENDING)


class ClickShapeTests(ProviderShapeTestCase):
    """Form-encoded, md5 over eight concatenated parts, amounts in soum."""

    def signed(self, txn, click_id, action, prepare_id, error="0"):
        service = CREDENTIALS["click"]["service_id"]
        secret = CREDENTIALS["click"]["secret_key"]
        raw = (f"{click_id}{service}{secret}{txn.pk}{prepare_id}"
               f"{AMOUNT}{action}{SIGN_TIME}")
        return {
            "click_trans_id": click_id, "service_id": service,
            "merchant_trans_id": str(txn.pk),
            "merchant_prepare_id": prepare_id, "amount": str(AMOUNT),
            "action": action, "error": error, "sign_time": SIGN_TIME,
            "sign_string": hashlib.md5(raw.encode()).hexdigest(),
        }

    def prepare_then_complete(self, txn, click_id, error="0"):
        prepared = self.client.post(
            self.url("click"), self.signed(txn, click_id, "0", ""))
        prepare_id = prepared.json().get("merchant_prepare_id", "")
        return self.client.post(
            self.url("click"),
            self.signed(txn, click_id, "1", prepare_id, error))

    def test_a_completed_payment_is_recorded(self):
        txn = self.transaction("click")
        response = self.prepare_then_complete(txn, "1111111111")
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], 0)
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_declined_payment_is_settled_not_left_pending(self):
        """The bug this test exists for.

        tolov cancels with `reason="Error code: -5017"`, a sentence, into an
        IntegerField. Unpatched, Django raises, tolov answers Click with
        `error: -7`, and our transaction stays `pending` — so the customer's
        result page polls "waiting" for ever and Click retries a callback
        that can only fail again.
        """
        txn = self.transaction("click")
        response = self.prepare_then_complete(txn, "2222222222", error="-5017")
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(
            response.json().get("error"), -7,
            "the callback raised inside tolov instead of cancelling",
        )
        self.assertIn(
            txn.status,
            (TransactionStatus.CANCELLED, TransactionStatus.FAILED),
            "a declined card must not leave the payment pending",
        )

    def test_the_declining_error_code_is_kept(self):
        """The number is the useful part of the sentence."""
        txn = self.transaction("click")
        self.prepare_then_complete(txn, "3333333333", error="-5017")
        row = PaymentTransaction.objects.get(transaction_id="3333333333")
        self.assertEqual(row.reason, -5017)

    def test_a_forged_signature_is_refused(self):
        txn = self.transaction("click")
        body = self.signed(txn, "4444444444", "1", "")
        body["sign_string"] = "0" * 32
        response = self.client.post(self.url("click"), body)
        txn.refresh_from_db()
        self.assertEqual(response.json()["error"], -1)
        self.assertEqual(txn.status, TransactionStatus.PENDING)


class UzumShapeTests(ProviderShapeTestCase):
    """JSON, Basic auth on both halves, and the operation is in the path."""

    def call(self, action, body):
        return self.client.post(
            self.url("uzum", f"{action}/"),
            json.dumps(body), content_type="application/json",
            **self.basic(CREDENTIALS["uzum"]["username"],
                         CREDENTIALS["uzum"]["password"]),
        )

    def body(self, txn, trans_id):
        return {
            "serviceId": int(CREDENTIALS["uzum"]["service_id"]),
            "timestamp": 1756800000000, "transId": trans_id,
            "amount": int(AMOUNT * 100),
            "params": {"orderId": str(txn.pk)},
        }

    def test_a_confirmed_payment_is_recorded(self):
        """The bug this test exists for: every Uzum callback used to 500."""
        txn = self.transaction("uzum")
        body = self.body(txn, "uzum-trans-0001")
        self.call("check", body)
        self.call("create", body)
        response = self.call("confirm", body)
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_a_reversal_settles_the_payment(self):
        txn = self.transaction("uzum")
        body = self.body(txn, "uzum-trans-0002")
        self.call("create", body)
        response = self.call("reverse", body)
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(txn.status, TransactionStatus.PENDING)

    def test_a_callback_url_missing_the_operation_explains_itself(self):
        """A misconfigured cabinet entry must not look like a server fault.

        500 tells the provider to retry something that can never work; 400
        with a message tells whoever set the URL up what to change.
        """
        txn = self.transaction("uzum")
        response = self.client.post(
            self.url("uzum"),
            json.dumps(self.body(txn, "uzum-trans-0003")),
            content_type="application/json",
            **self.basic(CREDENTIALS["uzum"]["username"],
                         CREDENTIALS["uzum"]["password"]),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm", response.json().get("detail", ""))

    def test_wrong_credentials_are_refused(self):
        txn = self.transaction("uzum")
        response = self.client.post(
            self.url("uzum", "confirm/"),
            json.dumps(self.body(txn, "uzum-trans-0004")),
            content_type="application/json",
            **self.basic("someone-else", "wrong-password"),
        )
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errorCode"], "10001")
        self.assertEqual(txn.status, TransactionStatus.PENDING)


class PaynetShapeTests(ProviderShapeTestCase):
    """JSON-RPC, Basic auth on both halves, amounts in tiyin."""

    def rpc(self, method, params, auth=None):
        user, password = auth or (CREDENTIALS["paynet"]["username"],
                                  CREDENTIALS["paynet"]["password"])
        return self.client.post(
            self.url("paynet"),
            json.dumps({"jsonrpc": "2.0", "id": 7,
                        "method": method, "params": params}),
            content_type="application/json",
            **self.basic(user, password),
        )

    def test_a_performed_payment_is_recorded(self):
        txn = self.transaction("paynet")
        response = self.rpc("PerformTransaction", {
            "transactionId": "paynet-0001",
            "serviceId": int(CREDENTIALS["paynet"]["service_id"]),
            "amount": int(AMOUNT * 100), "time": 1756800000000,
            "fields": {"id": str(txn.pk)},
        })
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(txn.status, TransactionStatus.PAID)

    def test_the_amount_is_read_as_tiyin(self):
        """Paynet's `?a=` really is tiyin — passing soum charged 1%."""
        txn = self.transaction("paynet")
        response = self.rpc("PerformTransaction", {
            "transactionId": "paynet-0002",
            "serviceId": int(CREDENTIALS["paynet"]["service_id"]),
            "amount": int(AMOUNT), "time": 1756800000000,
            "fields": {"id": str(txn.pk)},
        })
        txn.refresh_from_db()
        self.assertIn("error", response.json())
        self.assertEqual(txn.status, TransactionStatus.PENDING)

    def test_wrong_credentials_are_refused(self):
        txn = self.transaction("paynet")
        response = self.rpc(
            "PerformTransaction",
            {"transactionId": "paynet-0003",
             "serviceId": int(CREDENTIALS["paynet"]["service_id"]),
             "amount": int(AMOUNT * 100), "fields": {"id": str(txn.pk)}},
            auth=("nobody", "wrong"),
        )
        txn.refresh_from_db()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(txn.status, TransactionStatus.PENDING)


class CancelReasonCoercionTests(TestCase):
    """The patch itself, without a webhook around it."""

    def test_a_sentence_keeps_its_number(self):
        self.assertEqual(_coerce_reason("Error code: -5017"), -5017)

    def test_an_integer_is_left_alone(self):
        self.assertEqual(_coerce_reason(5), 5)

    def test_none_stays_none(self):
        self.assertIsNone(_coerce_reason(None))

    def test_a_reason_with_no_number_becomes_none(self):
        """Better an empty column than an exception inside a callback."""
        self.assertIsNone(_coerce_reason("declined by issuer"))
