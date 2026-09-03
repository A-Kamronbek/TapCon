"""Octo — the card-payment route.

Octo differs from the other five providers in ways that each had to be found
by reading tolov's source, and each of which would break real payments:

  * its webhook raises in ``__init__`` unless credentials are already in
    settings, so it cannot be constructed before the seller is known;
  * it signs callbacks with a `unique_key` that is neither the secret nor
    something the other providers have;
  * it **skips signature verification entirely in test mode**, so that flag
    has to follow the seller and not a global setting;
  * it is told where to send its callback at construction time, and that URL
    is per seller.

These tests hold each of those in place.
"""
import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import Transaction, TransactionStatus
from .providers import PROVIDERS
from .services import (
    build_gateway,
    save_credentials,
    set_enabled,
    set_test_mode,
    start_payment,
    webhook_url,
)

PASSWORD = "s3cretpw!x"
SHOP_ID = "77001"
SECRET = "octo-secret-abcdef"
UNIQUE_KEY = "octo-unique-key-123456"


class OctoTestCase(TestCase):
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

        save_credentials(
            self.profile,
            "octo",
            {
                "octo_shop_id": SHOP_ID,
                "octo_secret": SECRET,
                "octo_unique_key": UNIQUE_KEY,
            },
        )
        set_enabled(self.profile, "octo", True)


class RegistryTests(OctoTestCase):
    def test_octo_collects_the_callback_key(self):
        """Without it a live callback cannot be verified at all."""
        names = [f["name"] for f in PROVIDERS["octo"]["fields"]]
        self.assertIn("octo_unique_key", names)

    def test_the_callback_key_is_treated_as_a_secret(self):
        spec = next(
            f for f in PROVIDERS["octo"]["fields"] if f["name"] == "octo_unique_key"
        )
        self.assertTrue(spec["secret"])

    def test_the_callback_key_is_not_passed_to_the_gateway_constructor(self):
        """OctoGateway does not take it; an unexpected kwarg would raise."""
        integration = self.profile.integrations.get(provider="octo")
        self.assertNotIn("octo_unique_key", integration.gateway_kwargs())

    def test_a_seller_sees_octo_but_a_customer_sees_a_bank_card(self):
        from django.utils import translation

        integration = self.profile.integrations.get(provider="octo")
        # The seller's name for it is a brand and never translated.
        self.assertEqual(integration.label, "Octo")
        self.assertTrue(integration.customer_logo_url.endswith("card.png"))

        # The customer's name for it is ordinary words, and is translated.
        with translation.override("en"):
            self.assertEqual(str(integration.customer_label), "Bank card")
        with translation.override("uz"):
            self.assertEqual(str(integration.customer_label), "Bank kartasi")


class NotifyUrlTests(OctoTestCase):
    def test_the_notify_url_names_this_seller(self):
        url = webhook_url(self.profile, "octo")
        self.assertTrue(url.startswith(settings.SITE_URL))
        self.assertIn(f"/webhooks/octo/{self.profile.uid}/", url)

    def test_the_gateway_is_built_with_that_url(self):
        integration = self.profile.integrations.get(provider="octo")
        captured = {}

        class FakeOcto:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("payments.services.import_string", return_value=FakeOcto):
            build_gateway(integration)

        self.assertEqual(
            captured["notify_url"], webhook_url(self.profile, "octo")
        )
        self.assertEqual(captured["octo_secret"], SECRET)

    def test_the_shop_id_reaches_octo_as_a_number(self):
        """A seller types it; Octo's API types it as an integer.

        Octo puts `octo_shop_id` straight into its JSON body and compares it
        as a number on the way back. Quoted, it is a different value to a
        strict API — and the failure would be a rejected `prepare_payment`
        for a real customer, not anything visible here.
        """
        integration = self.profile.integrations.get(provider="octo")
        captured = {}

        class FakeOcto:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("payments.services.import_string", return_value=FakeOcto):
            build_gateway(integration)

        self.assertEqual(captured["octo_shop_id"], int(SHOP_ID))
        self.assertIsInstance(captured["octo_shop_id"], int)

    def test_a_provider_without_notify_url_is_not_given_one(self):
        """Payme's constructor would reject an unexpected keyword."""
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        integration = self.profile.integrations.get(provider="payme")
        captured = {}

        class FakePayme:
            def __init__(self, payme_id=None, payme_key=None, is_test_mode=False):
                captured.update(
                    {"payme_id": payme_id, "payme_key": payme_key,
                     "is_test_mode": is_test_mode}
                )

        with patch("payments.services.import_string", return_value=FakePayme):
            build_gateway(integration)

        self.assertNotIn("notify_url", captured)


class CheckoutTests(OctoTestCase):
    def test_starting_a_card_payment(self):
        class FakeOcto:
            def __init__(self, **kwargs):
                pass

            def create_payment(self, **kwargs):
                FakeOcto.called = kwargs
                return "https://pay.octo.uz/checkout/abc"

        with patch("payments.services.import_string", return_value=FakeOcto):
            txn, url = start_payment(self.profile, "octo", Decimal("42000"))

        self.assertEqual(url, "https://pay.octo.uz/checkout/abc")
        self.assertEqual(txn.status, TransactionStatus.PENDING)
        self.assertEqual(FakeOcto.called["id"], txn.pk)
        self.assertEqual(FakeOcto.called["amount"], Decimal("42000"))


class SignedCallbackTests(OctoTestCase):
    """Octo signs sha1(unique_key + uuid + status)."""

    def setUp(self):
        super().setUp()
        set_test_mode(self.profile, "octo", False)
        self.txn = Transaction.objects.create(
            seller=self.profile,
            provider="octo",
            amount=Decimal("42000"),
            status=TransactionStatus.PENDING,
            is_test_mode=False,
        )
        self.url = reverse("webhooks:provider", args=["octo", self.profile.uid])

    def sign(self, uuid, status, key=UNIQUE_KEY):
        return hashlib.sha1(f"{key}{uuid}{status}".encode("utf-8")).hexdigest()

    def call(self, status="succeeded", uuid="octo-uuid-1", signature=None, amount=None):
        payload = {
            "octo_payment_UUID": uuid,
            "shop_transaction_id": str(self.txn.pk),
            "status": status,
            "total_sum": float(amount if amount is not None else self.txn.amount),
            "signature": signature if signature is not None else self.sign(uuid, status),
        }
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )

    def test_a_correctly_signed_success_marks_the_payment_paid(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PAID)

    def test_a_wrongly_signed_callback_is_refused(self):
        """The whole point of the callback key."""
        response = self.call(signature="0" * 40)
        self.assertEqual(response.status_code, 403)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PENDING)

    def test_another_sellers_callback_key_does_not_work(self):
        response = self.call(signature=self.sign("octo-uuid-1", "succeeded", "someone-else"))
        self.assertEqual(response.status_code, 403)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PENDING)

    def test_a_callback_with_no_signature_is_refused(self):
        response = self.call(signature="")
        self.assertEqual(response.status_code, 403)

    def test_a_cancelled_callback_marks_the_payment_cancelled(self):
        response = self.call(status="canceled")
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.CANCELLED)

    def test_a_refund_is_recorded_as_a_refund_not_ignored(self):
        """The one that would have shown refunded money as money received.

        Octo funnels `refunded` through the same hook as `canceled`. A paid
        payment cannot be cancelled, so the transition was refused and the row
        stayed `paid` — the seller would still have seen the money as theirs.
        """
        self.call()
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PAID)

        response = self.call(status="refunded", uuid="octo-uuid-1")
        self.assertEqual(response.status_code, 200)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.REFUNDED)

    def test_the_result_page_says_refunded_not_waiting(self):
        """Every status needs its own branch, or it falls through to the
        spinner and tells the customer their money is still on its way."""
        self.call()
        self.call(status="refunded", uuid="octo-uuid-1")
        self.txn.refresh_from_db()

        body = self.client.get(
            reverse("payments:result", args=[self.profile.uid, self.txn.uid])
        ).content.decode("utf-8")

        self.assertIn('data-settled="1"', body)
        self.assertNotIn("payment-status.js", body)
        with translation.override("en"):
            self.assertIn("Refunded", self.client.get(
                reverse("payments:result", args=[self.profile.uid, self.txn.uid]),
                headers={"accept-language": "en"},
            ).content.decode("utf-8"))

    def test_a_refund_is_not_counted_as_takings(self):
        from django.db.models import Sum

        self.call()
        self.call(status="refunded", uuid="octo-uuid-1")

        total = (
            Transaction.objects.real().paid().aggregate(t=Sum("amount"))["t"] or 0
        )
        self.assertEqual(total, 0)

    def test_a_cancellation_is_still_a_cancellation(self):
        """The refund handling must not swallow ordinary cancellations."""
        fresh = Transaction.objects.create(
            seller=self.profile, provider="octo", amount=Decimal("500"),
            status=TransactionStatus.PENDING, is_test_mode=False,
        )
        uuid, status = "octo-uuid-cancel", "canceled"
        payload = {
            "octo_payment_UUID": uuid,
            "shop_transaction_id": str(fresh.pk),
            "status": status,
            "total_sum": float(fresh.amount),
            "signature": self.sign(uuid, status),
        }
        response = self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, TransactionStatus.CANCELLED)

    def test_a_replayed_callback_changes_nothing(self):
        self.call()
        self.txn.refresh_from_db()
        paid_at = self.txn.paid_at

        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PAID)
        self.assertEqual(self.txn.paid_at, paid_at)

    def test_a_live_seller_does_not_inherit_a_global_test_mode(self):
        """tolov skips signature checks in test mode.

        If that flag came from settings rather than the seller, a live
        seller's callbacks would be accepted unsigned.
        """
        response = self.call(signature="0" * 40)
        self.assertEqual(response.status_code, 403)

    def test_a_sandbox_seller_is_not_asked_for_a_signature(self):
        set_test_mode(self.profile, "octo", True)
        response = self.call(signature="")
        self.assertEqual(response.status_code, 200)


class SellerIsolationTests(OctoTestCase):
    def test_a_seller_cannot_settle_another_sellers_card_payment(self):
        other_user = register_seller(
            phone="+998 90 888 77 66",
            full_name="Bob",
            business_name="Bob Cafe",
            password=PASSWORD,
        )
        other = SellerProfile.objects.get(user=other_user)
        other.approve()
        save_credentials(
            other,
            "octo",
            {"octo_shop_id": "999", "octo_secret": "x", "octo_unique_key": "bobs-key"},
        )
        set_enabled(other, "octo", True)
        set_test_mode(other, "octo", False)

        mine = Transaction.objects.create(
            seller=self.profile, provider="octo", amount=Decimal("42000"),
            status=TransactionStatus.PENDING, is_test_mode=False,
        )

        uuid, status = "octo-uuid-9", "succeeded"
        payload = {
            "octo_payment_UUID": uuid,
            "shop_transaction_id": str(mine.pk),
            "status": status,
            "total_sum": float(mine.amount),
            "signature": hashlib.sha1(
                f"bobs-key{uuid}{status}".encode("utf-8")
            ).hexdigest(),
        }
        response = self.client.post(
            reverse("webhooks:provider", args=["octo", other.uid]),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertNotEqual(response.status_code, 200)
        mine.refresh_from_db()
        self.assertEqual(mine.status, TransactionStatus.PENDING)


class PayPageTests(OctoTestCase):
    def test_the_card_button_says_bank_card_not_octo(self):
        body = self.client.get(
            reverse("payments:pay", args=[self.profile.uid])
        ).content.decode("utf-8")
        self.assertIn("card.png", body)
        self.assertNotIn(">Octo<", body)
