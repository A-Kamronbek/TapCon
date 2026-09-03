"""What a seller is told they earned.

Every test here is a way a seller could be shown a number that is not true.
That is the whole risk of this phase: the dashboard has no side effects, so
nothing here can break a payment — it can only lie about one, and a figure a
seller trusts and acts on is worse than an error they can see.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import Transaction, TransactionStatus
from .reporting import (
    TransactionFilter,
    by_provider,
    daily_series,
    day_start,
    period_start,
    recent,
    totals,
)
from .services import save_credentials, set_enabled

PASSWORD = "s3cretpw!x"


class ReportingTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

        self.user = register_seller(
            phone="+998 90 123 45 67",
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=self.user)
        self.profile.approve()
        save_credentials(
            self.profile, "payme", {"payme_id": "p1", "payme_key": "k1"}
        )
        set_enabled(self.profile, "payme", True)

    def other_seller(self):
        user = register_seller(
            phone="+998 90 777 66 55",
            full_name="Bek",
            business_name="Boshqa",
            password=PASSWORD,
        )
        profile = SellerProfile.objects.get(user=user)
        profile.approve()
        return profile

    def payment(
        self,
        amount="50000",
        *,
        seller=None,
        status=TransactionStatus.PAID,
        provider="payme",
        sandbox=False,
        when=None,
    ):
        """A transaction already in its final state, dated where we want it.

        `paid_at` and `created_at` are written directly rather than driven
        through `move_to()`: these tests are about arithmetic over history,
        and history includes days that are not today.
        """
        when = when or timezone.now()
        txn = Transaction.objects.create(
            seller=seller or self.profile,
            provider=provider,
            amount=Decimal(amount),
            status=status,
            is_test_mode=sandbox,
            paid_at=when if status == TransactionStatus.PAID else None,
        )
        Transaction.objects.filter(pk=txn.pk).update(created_at=when)
        txn.refresh_from_db()
        return txn


class TakingsTests(ReportingTestCase):
    """What counts as money, and what only looks like it."""

    def test_a_paid_payment_counts(self):
        self.payment("50000")
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("50000"))

    def test_a_sandbox_payment_does_not_count(self):
        """It is a real row with a real amount and no money behind it.

        One flag is the only thing separating it from income, which is
        exactly why every total has to go through `real()`.
        """
        self.payment("50000", sandbox=True)
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("0"))
        self.assertEqual(totals(self.profile)["today"]["count"], 0)

    def test_a_refunded_payment_does_not_count(self):
        """The money went back. Counting it is telling a seller they have it."""
        self.payment("50000", status=TransactionStatus.REFUNDED)
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("0"))

    def test_an_unfinished_payment_does_not_count(self):
        for status in (
            TransactionStatus.CREATED,
            TransactionStatus.PENDING,
            TransactionStatus.CANCELLED,
            TransactionStatus.FAILED,
        ):
            with self.subTest(status=status):
                Transaction.objects.all().delete()
                self.payment("50000", status=status)
                self.assertEqual(
                    totals(self.profile)["today"]["total"], Decimal("0")
                )

    def test_another_sellers_money_is_not_mine(self):
        self.payment("50000", seller=self.other_seller())
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("0"))

    def test_the_three_windows_nest(self):
        now = timezone.now()
        self.payment("10000", when=now)
        self.payment("20000", when=now - timedelta(days=3))
        self.payment("40000", when=now - timedelta(days=20))
        # A payment older than every window, to prove the windows have an edge.
        self.payment("80000", when=now - timedelta(days=200))

        figures = totals(self.profile)
        self.assertEqual(figures["today"]["total"], Decimal("10000"))
        self.assertEqual(figures["week"]["total"], Decimal("30000"))
        self.assertEqual(figures["month"]["total"], Decimal("70000"))

    def test_takings_are_dated_by_when_the_money_arrived(self):
        """A payment started at 23:58 and confirmed at 00:01 is tomorrow's.

        `created_at` is when the customer tapped; `paid_at` is when the money
        was ours. The seller's day is counted by the second.
        """
        yesterday = day_start() - timedelta(hours=1)
        txn = self.payment("50000", when=yesterday)
        Transaction.objects.filter(pk=txn.pk).update(paid_at=timezone.now())

        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("50000"))


class DayBoundaryTests(ReportingTestCase):
    """A day is the seller's day, not UTC's.

    Tashkent is UTC+5, so a UTC-based boundary would start "today" at 5am and
    file every payment taken between midnight and dawn as yesterday's — the
    late-evening trade of a cafe, counted against the wrong day.
    """

    def test_a_payment_just_after_local_midnight_is_todays(self):
        just_after = day_start() + timedelta(minutes=30)
        if just_after > timezone.localtime():
            self.skipTest("the test is running in that first half hour")
        self.payment("50000", when=just_after)
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("50000"))

    def test_a_payment_just_before_local_midnight_is_yesterdays(self):
        just_before = day_start() - timedelta(minutes=1)
        self.payment("50000", when=just_before)

        figures = totals(self.profile)
        self.assertEqual(figures["today"]["total"], Decimal("0"))
        self.assertEqual(figures["week"]["total"], Decimal("50000"))

    def test_the_day_starts_at_local_midnight(self):
        start = day_start()
        self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
        self.assertEqual(
            start.utcoffset(), timezone.localtime().utcoffset()
        )

    def test_a_period_is_whole_days_not_a_rolling_window(self):
        """'Last 7 days' means seven days of trading, not the last 168 hours."""
        self.assertEqual(period_start("week"), day_start() - timedelta(days=6))
        self.assertEqual(period_start("today"), day_start())
        self.assertIsNone(period_start("all"))


class BreakdownTests(ReportingTestCase):
    def test_providers_are_ordered_by_money_not_by_taps(self):
        """A seller weighing up a provider's paperwork cares about the soum."""
        self.payment("100000", provider="payme")
        for _ in range(5):
            self.payment("1000", provider="click")

        rows = by_provider(self.profile)
        self.assertEqual(rows[0]["provider"], "payme")
        self.assertEqual(rows[0]["total"], Decimal("100000"))
        self.assertEqual(rows[1]["count"], 5)

    def test_the_breakdown_uses_the_sellers_name_for_a_provider(self):
        self.payment("1000", provider="octo")
        self.assertEqual(by_provider(self.profile)[0]["label"], "Octo")

    def test_sandbox_and_refunds_stay_out_of_the_breakdown(self):
        self.payment("50000", sandbox=True)
        self.payment("50000", status=TransactionStatus.REFUNDED)
        self.assertEqual(by_provider(self.profile), [])


class DailySeriesTests(ReportingTestCase):
    def test_it_returns_one_entry_per_day_including_the_empty_ones(self):
        """A chart that skipped quiet days would draw a closed shop as trading."""
        self.payment("10000", when=timezone.now() - timedelta(days=3))
        series = daily_series(self.profile, days=14)

        self.assertEqual(len(series), 14)
        self.assertEqual(sum(1 for day in series if day["total"]), 1)

    def test_it_runs_oldest_first_and_ends_today(self):
        series = daily_series(self.profile, days=14)
        self.assertEqual(series[-1]["date"], day_start().date())
        self.assertTrue(series[-1]["is_today"])
        self.assertEqual([day["date"] for day in series],
                         sorted(day["date"] for day in series))

    def test_the_tallest_bar_is_full_height_and_the_rest_are_relative(self):
        self.payment("100000", when=timezone.now())
        self.payment("50000", when=timezone.now() - timedelta(days=1))

        by_day = {day["date"]: day for day in daily_series(self.profile)}
        today = by_day[day_start().date()]
        yesterday = by_day[(day_start() - timedelta(days=1)).date()]

        self.assertEqual(today["height"], 100)
        self.assertEqual(yesterday["height"], 50)

    def test_a_seller_with_nothing_gets_a_flat_chart_not_a_crash(self):
        """Division by a zero peak is the obvious way this breaks."""
        series = daily_series(self.profile)
        self.assertEqual(len(series), 14)
        self.assertTrue(all(day["height"] == 0 for day in series))

    def test_sandbox_money_is_not_drawn(self):
        self.payment("100000", sandbox=True)
        self.assertTrue(all(day["total"] == 0 for day in daily_series(self.profile)))


class RecentTests(ReportingTestCase):
    def test_failures_are_shown_too(self):
        """A seller looking after 'it didn't work' needs to see the failure."""
        self.payment("1000", status=TransactionStatus.FAILED)
        self.assertEqual(len(recent(self.profile)), 1)

    def test_it_is_newest_first_and_capped(self):
        now = timezone.now()
        for offset in range(8):
            self.payment("1000", when=now - timedelta(minutes=offset))
        rows = recent(self.profile, limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows, sorted(rows, key=lambda t: t.created_at, reverse=True))

    def test_another_sellers_payments_are_not_recent_for_me(self):
        self.payment("1000", seller=self.other_seller())
        self.assertEqual(recent(self.profile), [])


class FilterTests(ReportingTestCase):
    def filters(self, **params):
        return TransactionFilter(params, self.profile)

    def test_it_defaults_to_thirty_days_of_real_payments(self):
        f = self.filters()
        self.assertEqual(f.period, "month")
        self.assertFalse(f.sandbox)
        self.assertFalse(f.is_narrowed)

    def test_junk_in_the_query_string_falls_back_rather_than_matching_nothing(self):
        """A stale or hand-edited URL must not show a seller an empty ledger."""
        f = self.filters(period="lifetime", provider="paypal", status="exploded")
        self.assertEqual(f.period, "month")
        self.assertEqual(f.provider, "")
        self.assertEqual(f.status, "")

    def test_sandbox_rows_are_hidden_by_default(self):
        self.payment("1000", sandbox=True)
        self.assertEqual(self.filters().queryset().count(), 0)

    def test_asking_for_sandbox_shows_only_sandbox(self):
        self.payment("1000", sandbox=True)
        self.payment("2000")
        rows = self.filters(sandbox="1").queryset()
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.first().is_test_mode)

    def test_it_never_leaves_this_seller(self):
        self.payment("1000", seller=self.other_seller())
        self.assertEqual(self.filters(period="all").queryset().count(), 0)

    def test_the_summary_describes_what_is_on_screen(self):
        self.payment("50000")
        self.payment("1000", status=TransactionStatus.FAILED)

        summary = self.filters().summary()
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["paid_count"], 1)
        self.assertEqual(summary["paid_total"], Decimal("50000"))

    def test_a_sandbox_summary_says_it_is_not_money(self):
        self.payment("50000", sandbox=True)
        self.assertTrue(self.filters(sandbox="1").summary()["is_sandbox"])

    def test_a_query_can_be_rebuilt_with_one_part_replaced(self):
        f = self.filters(period="week", provider="payme")
        self.assertIn("period=week", f.as_query())
        self.assertIn("provider=payme", f.as_query())
        self.assertIn("page=3", f.as_query(page=3))


class DashboardPageTests(ReportingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("merchants:dashboard")

    def test_it_shows_todays_takings(self):
        self.payment("125000")
        response = self.client.get(self.url)
        # The non-breaking space is the money filter's, and is deliberate.
        self.assertContains(response, "125 000")

    def test_a_new_seller_sees_no_chart_and_no_apology(self):
        response = self.client.get(self.url)
        self.assertNotContains(response, 'class="bars"')
        self.assertEqual(response.status_code, 200)

    def test_the_chart_appears_once_there_is_something_to_draw(self):
        self.payment("1000")
        self.assertContains(self.client.get(self.url), 'class="bars"')

    def test_it_never_shows_another_sellers_money(self):
        self.payment("999999", seller=self.other_seller())
        self.assertNotContains(self.client.get(self.url), "999 999")

    def test_sandbox_money_is_not_on_the_dashboard(self):
        self.payment("777777", sandbox=True)
        self.assertNotContains(self.client.get(self.url), "777 777")

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


class TransactionsPageTests(ReportingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("merchants:transactions")

    def test_a_payment_is_listed_with_its_reference(self):
        txn = self.payment("50000")
        self.assertContains(self.client.get(self.url), txn.uid)

    def test_a_new_seller_is_told_what_happens_next(self):
        """Not "nothing matches your filters" — they have not filtered anything.

        The two empty states say different things and offer different ways
        out, so they carry different classes rather than being told apart by
        their wording, which changes with the language.
        """
        response = self.client.get(self.url)
        self.assertContains(response, "empty-no-payments")
        self.assertNotContains(response, "empty-no-matches")

    def test_filtering_to_nothing_says_so_and_offers_a_way_back(self):
        self.payment("50000", provider="payme")
        response = self.client.get(self.url, {"provider": "click"})
        self.assertContains(response, "empty-no-matches")
        self.assertNotContains(response, "empty-no-payments")

    def test_sandbox_rows_are_absent_until_asked_for(self):
        txn = self.payment("50000", sandbox=True)
        self.assertNotContains(self.client.get(self.url), txn.uid)
        self.assertContains(self.client.get(self.url, {"sandbox": "1"}), txn.uid)

    def test_another_sellers_payment_is_never_listed(self):
        txn = self.payment("50000", seller=self.other_seller())
        self.assertNotContains(self.client.get(self.url, {"period": "all"}), txn.uid)

    def test_it_pages_rather_than_rendering_a_year_at_once(self):
        now = timezone.now()
        for offset in range(30):
            self.payment("1000", when=now - timedelta(minutes=offset))
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["page"].object_list), 25)
        self.assertContains(response, "pager")

    def test_a_nonsense_page_number_lands_on_a_page_not_an_error(self):
        """A stale link or a typo is not worth an error screen."""
        self.payment("1000")
        for value in ("0", "9999", "abc", ""):
            with self.subTest(page=value):
                self.assertEqual(
                    self.client.get(self.url, {"page": value}).status_code, 200
                )

    def test_a_filtered_page_link_keeps_the_filter(self):
        now = timezone.now()
        for offset in range(30):
            self.payment("1000", when=now - timedelta(minutes=offset))
        response = self.client.get(self.url, {"period": "week"})
        self.assertContains(response, "period=week&amp;page=2")


class ReturnedPaymentTests(ReportingTestCase):
    """TapCon never starts a refund, and a seller must not think it did.

    There is no refund button, no refund view and no call to a provider's
    refund API anywhere in the project. The status exists only because Octo
    can report a refund made in *its* cabinet or through a bank dispute, and
    a payment whose money has gone back must stop counting as takings.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("merchants:transactions")

    def test_nothing_in_the_project_starts_a_refund(self):
        """The guard against someone adding one without meaning to.

        tolov exposes `cancel_payment` on the Octo gateway. Calling it would
        move real money, so if it ever appears in our code it should be a
        deliberate decision with its own confirmation, not a line that slipped
        into a service function.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.py"):
            if ".venv" in path.parts or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            if "cancel_payment" in text or "gateway.refund" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_the_seller_sees_who_returned_the_money(self):
        """Not a bare "Refunded", which reads as something TapCon did."""
        from django.utils import translation as trans

        txn = self.payment("50000", status=TransactionStatus.REFUNDED)
        with trans.override("en"):
            self.assertEqual(str(txn.get_status_display()), "Returned to customer")

    def test_a_returned_payment_is_explained_when_one_is_on_screen(self):
        self.payment("50000", status=TransactionStatus.REFUNDED)
        self.assertContains(self.client.get(self.url), "txn-note")

    def test_the_explanation_stays_away_when_there_is_nothing_to_explain(self):
        """A permanent notice about something that almost never happens is noise."""
        self.payment("50000")
        self.assertNotContains(self.client.get(self.url), "txn-note")

    def test_a_returned_payment_is_not_takings_anywhere(self):
        self.payment("50000", status=TransactionStatus.REFUNDED)
        self.assertEqual(totals(self.profile)["today"]["total"], Decimal("0"))
        self.assertEqual(by_provider(self.profile), [])
        self.assertTrue(all(d["total"] == 0 for d in daily_series(self.profile)))

    def test_it_is_still_listed_so_the_seller_can_see_it_happened(self):
        """Excluded from the totals, not hidden. Both facts matter."""
        txn = self.payment("50000", status=TransactionStatus.REFUNDED)
        self.assertContains(self.client.get(self.url), txn.uid)


class CsvExportTests(ReportingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("merchants:transactions_csv")

    def body(self, response):
        return b"".join(response.streaming_content).decode("utf-8")

    def test_it_downloads_as_a_file(self):
        response = self.client.get(self.url)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_it_starts_with_a_bom_so_excel_reads_it(self):
        """Without this, Excel on Windows renders every Cyrillic name as junk."""
        self.assertTrue(self.body(self.client.get(self.url)).startswith("﻿"))

    def test_a_payment_appears_with_its_amount_as_plain_digits(self):
        """The column is going into a spreadsheet to be summed, not read."""
        txn = self.payment("50000")
        body = self.body(self.client.get(self.url))
        self.assertIn(txn.uid, body)
        self.assertIn("50000", body)
        self.assertNotIn("50 000", body)

    def test_it_exports_exactly_what_the_filters_show(self):
        kept = self.payment("50000", provider="payme")
        dropped = self.payment("60000", provider="click")
        body = self.body(self.client.get(self.url, {"provider": "payme"}))
        self.assertIn(kept.uid, body)
        self.assertNotIn(dropped.uid, body)

    def test_sandbox_rows_are_not_exported_by_default(self):
        txn = self.payment("50000", sandbox=True)
        self.assertNotIn(txn.uid, self.body(self.client.get(self.url)))

    def test_an_exported_sandbox_row_is_labelled(self):
        self.payment("50000", sandbox=True)
        body = self.body(self.client.get(self.url, {"sandbox": "1"}))
        self.assertIn("Sinov", body)

    def test_another_sellers_rows_are_never_exported(self):
        txn = self.payment("50000", seller=self.other_seller())
        self.assertNotIn(
            txn.uid, self.body(self.client.get(self.url, {"period": "all"}))
        )

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)
