"""The reporting cases that seeded data never produces.

Both of these are quiet: nothing errors, no page breaks, and a seller reading
the wrong number has no way to tell. They only show up when you construct the
row deliberately.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from merchants.models import SellerProfile
from payments import reporting
from payments.models import Transaction, TransactionStatus

from accounts.services import register_seller
from unittest.mock import patch


class ReportingEdgeCaseTests(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone="+998 90 123 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password="s3cretpw!x",
        )
        self.seller = SellerProfile.objects.get(user=user)
        self.seller.approve()

    def pay(self, amount, created_at, paid_at):
        # is_test_mode defaults to True — safe, but these rows have to be
        # money for the figures under test to see them at all.
        txn = Transaction.objects.create(
            seller=self.seller,
            provider="payme",
            amount=Decimal(amount),
            status=TransactionStatus.PAID,
            is_test_mode=False,
        )
        # created_at is auto_now_add, so it has to be forced afterwards.
        Transaction.objects.filter(pk=txn.pk).update(
            created_at=created_at, paid_at=paid_at
        )
        return Transaction.objects.get(pk=txn.pk)

    def test_a_payment_confirmed_after_midnight_counts_in_the_same_day_everywhere(self):
        """Started 23:58 yesterday, confirmed 00:01 today.

        The dashboard dates it by paid_at, so it is today's money. The
        transactions page must agree, or the seller reads two different
        totals for today and cannot reconcile them.
        """
        midnight = reporting.day_start()
        self.pay(
            "40000",
            created_at=midnight - timedelta(minutes=2),
            paid_at=midnight + timedelta(minutes=1),
        )

        dashboard = reporting.totals(self.seller)["today"]
        page = reporting.TransactionFilter({"period": "today"}, self.seller)

        self.assertEqual(dashboard["total"], Decimal("40000"))
        self.assertEqual(page.summary()["paid_total"], dashboard["total"])
        self.assertEqual(page.summary()["paid_count"], dashboard["count"])
        self.assertEqual(page.queryset().count(), 1, "the row must be listed too")

    def test_an_unpaid_attempt_is_dated_by_when_it_was_attempted(self):
        """A failure has no paid_at, so it belongs to the day it happened."""
        midnight = reporting.day_start()
        txn = Transaction.objects.create(
            seller=self.seller,
            provider="payme",
            amount=Decimal("5000"),
            is_test_mode=False,
        )
        Transaction.objects.filter(pk=txn.pk).update(
            created_at=midnight + timedelta(hours=1)
        )
        page = reporting.TransactionFilter({"period": "today"}, self.seller)
        self.assertEqual(page.queryset().count(), 1)
        self.assertEqual(page.summary()["paid_total"], Decimal("0"))

    def test_yesterdays_payment_is_not_in_todays_list(self):
        """The guard must not widen the period into a catch-all."""
        midnight = reporting.day_start()
        self.pay(
            "70000",
            created_at=midnight - timedelta(hours=5),
            paid_at=midnight - timedelta(hours=5),
        )
        page = reporting.TransactionFilter({"period": "today"}, self.seller)
        self.assertEqual(page.queryset().count(), 0)
        self.assertEqual(reporting.totals(self.seller)["today"]["total"], Decimal("0"))

    def test_a_small_day_is_not_drawn_as_an_empty_one(self):
        """0.2% of the peak must not round down to a closed shop."""
        midnight = reporting.day_start()
        self.pay("1000000", created_at=midnight, paid_at=midnight)
        yesterday = midnight - timedelta(days=1)
        self.pay("2000", created_at=yesterday, paid_at=yesterday)

        series = {day["date"]: day for day in reporting.daily_series(self.seller)}
        tiny = series[yesterday.date()]
        empty = series[(midnight - timedelta(days=5)).date()]

        self.assertEqual(tiny["total"], Decimal("2000"))
        self.assertGreater(tiny["height"], 0, "a trading day drawn as a closed one")
        self.assertEqual(empty["height"], 0, "a closed day must stay at zero")

    def test_the_tallest_bar_is_full_height(self):
        midnight = reporting.day_start()
        self.pay("1000000", created_at=midnight, paid_at=midnight)
        series = reporting.daily_series(self.seller)
        self.assertEqual(max(day["height"] for day in series), 100)
