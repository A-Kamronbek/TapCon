"""What a seller is shown about their own money.

Read-only, and deliberately separate from `services.py`: nothing here writes
anything, and the v2 API will want exactly these numbers.

Three rules run through all of it, and each one is a way a seller could be
told they earned more than they did:

  * **`real()` always.** A sandbox payment is a real row with a real amount,
    and the only thing separating it from money is one flag.
  * **A refund is not takings.** `paid` and `refunded` are different statuses
    precisely so a returned payment stops counting, while the fact that it
    happened is not erased.
  * **A day is the seller's day.** Boundaries come from Django's active
    timezone (Asia/Tashkent), not from UTC — otherwise "today" starts at 5am
    and a payment taken at 2am counts as yesterday's.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Transaction, TransactionStatus
from .providers import PROVIDERS

# What the list and the CSV can be narrowed by. Keys are what appears in the
# query string; a value that is not here is ignored rather than trusted.
PERIODS = {
    "today": _("Today"),
    "week": _("Last 7 days"),
    "month": _("Last 30 days"),
    "all": _("All time"),
}

STATUS_FILTERS = {
    "paid": _("Paid"),
    "refunded": _("Returned to customer"),
    "pending": _("Waiting"),
    "cancelled": _("Cancelled"),
    "failed": _("Failed"),
}


def day_start(when=None):
    """Midnight at the start of `when`, in the seller's own timezone."""
    when = when or timezone.localtime()
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def period_start(period: str):
    """The moment a period begins, or None for 'all time'.

    Periods are counted in whole local days, not rolling 24-hour windows: a
    seller asking for 'last 7 days' at 9am means seven days of trading, not
    the 168 hours since 9am last Tuesday.
    """
    today = day_start()
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    return None


def taken(seller, since=None, until=None):
    """Money actually received: real, paid, and not given back.

    Dated by `paid_at`, not `created_at` — a payment started at 23:58 and
    confirmed at 00:01 belongs to the day the money arrived.
    """
    rows = Transaction.objects.filter(seller=seller).real().paid()
    if since is not None:
        rows = rows.filter(paid_at__gte=since)
    if until is not None:
        rows = rows.filter(paid_at__lt=until)
    return rows


def totals(seller) -> dict:
    """The three figures the dashboard leads with, plus their counts."""
    today = day_start()
    result = {}
    for key, since in (
        ("today", today),
        ("week", today - timedelta(days=6)),
        ("month", today - timedelta(days=29)),
    ):
        row = taken(seller, since=since).aggregate(
            total=Sum("amount"), count=Count("id")
        )
        result[key] = {
            "total": row["total"] or Decimal("0"),
            "count": row["count"] or 0,
        }
    return result


def by_provider(seller, since=None) -> list:
    """Which ways of paying customers actually use.

    Ordered by money rather than by count: a seller deciding whether a
    provider is worth its paperwork cares about the soum, not the taps.
    """
    rows = (
        taken(seller, since=since)
        .values("provider")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    return [
        {
            "provider": row["provider"],
            "label": PROVIDERS.get(row["provider"], {}).get(
                "label", row["provider"]
            ),
            "total": row["total"] or Decimal("0"),
            "count": row["count"],
        }
        for row in rows
    ]


def _bar_height(total: Decimal, peak: Decimal) -> int:
    """A day's bar as a percentage of the tallest, 0 only when it took nothing."""
    if not peak or total <= 0:
        return 0
    return max(1, int(total / peak * 100))


def daily_series(seller, days: int = 14) -> list:
    """One entry per local day, oldest first, including the empty ones.

    The gaps matter: a chart that silently skips a day with no payments draws
    a week of trading as though it were continuous, and a seller reading it
    would see a flat line where there was a closed shop.
    """
    today = day_start()
    first = today - timedelta(days=days - 1)

    # One query, then bucketed in Python. `TruncDate` would push the grouping
    # into SQL, but SQLite does it in UTC regardless of Django's timezone, so
    # the dev database would disagree with production about where a day ends.
    rows = taken(seller, since=first).values_list("paid_at", "amount")

    buckets = {(first + timedelta(days=offset)).date(): Decimal("0")
               for offset in range(days)}
    for paid_at, amount in rows:
        local_day = timezone.localtime(paid_at).date()
        if local_day in buckets:
            buckets[local_day] += amount

    peak = max(buckets.values()) if buckets else Decimal("0")
    return [
        {
            "date": day,
            "total": total,
            # Percentage of the tallest bar, so the template does no maths.
            # A day with nothing gets 0 and is drawn as a baseline tick, not
            # as a missing bar.
            #
            # A day that took money never rounds down to 0: 2,000 soum beside
            # a 1,000,000 soum peak is 0.2%, and int() would draw that day
            # exactly like a day the shop was shut. Trading is always at least
            # one percent tall, so "small" and "none" stay distinguishable.
            "height": _bar_height(total, peak),
            "is_today": day == today.date(),
        }
        for day, total in sorted(buckets.items())
    ]


def recent(seller, limit: int = 5):
    """The last few payment attempts, whatever became of them.

    Not filtered to `paid`: a seller looking at their dashboard after a
    customer says "it didn't work" needs to see the failure.
    """
    return list(
        Transaction.objects.filter(seller=seller).real().order_by("-created_at")[
            :limit
        ]
    )


class TransactionFilter:
    """The transactions page's query string, validated once.

    Built from GET data but never trusting it: an unknown period, provider or
    status falls back to the default rather than raising or, worse, silently
    matching nothing and showing a seller an empty ledger.
    """

    def __init__(self, params, seller):
        self.seller = seller

        period = params.get("period", "month")
        self.period = period if period in PERIODS else "month"

        provider = params.get("provider", "")
        self.provider = provider if provider in PROVIDERS else ""

        status = params.get("status", "")
        self.status = status if status in STATUS_FILTERS else ""

        # Sandbox rows are hidden unless asked for. They are real rows with
        # real amounts and no money behind them, so a seller's normal view of
        # their own ledger must not contain any.
        self.sandbox = params.get("sandbox") == "1"

    @property
    def is_narrowed(self) -> bool:
        """Whether anything but the default period is in force.

        Used to tell "you have no payments yet" apart from "nothing matches
        these filters", which are different things to say to a seller.
        """
        return bool(
            self.provider or self.status or self.sandbox or self.period != "month"
        )

    def queryset(self):
        rows = Transaction.objects.filter(seller=self.seller)
        rows = rows.sandbox() if self.sandbox else rows.real()

        since = period_start(self.period)
        if since is not None:
            # A paid row belongs to the day the money arrived; anything else
            # belongs to the day it was attempted.
            #
            # Dating everything by `created_at` would put this page and the
            # dashboard on different calendars: a payment started at 23:58 and
            # confirmed at 00:01 counts in today's headline figure (totals()
            # dates by paid_at) but would be missing from today's list, and
            # from this page's own summary. The seller would then read two
            # different numbers for the same day and have no way to reconcile
            # them. Same rule on both pages, so the totals always tie out.
            rows = rows.filter(
                Q(paid_at__gte=since)
                | Q(paid_at__isnull=True, created_at__gte=since)
            )

        if self.provider:
            rows = rows.filter(provider=self.provider)
        if self.status:
            rows = rows.filter(status=self.status)

        return rows.order_by("-created_at")

    def summary(self) -> dict:
        """Totals for exactly what is on screen.

        Deliberately not `totals()`: those are always real and paid, and this
        has to describe whatever the seller filtered to — including a sandbox
        view, where the sum is emphatically not money.
        """
        rows = self.queryset()
        paid = rows.filter(status=TransactionStatus.PAID).aggregate(
            total=Sum("amount"), count=Count("id")
        )
        return {
            "count": rows.count(),
            "paid_count": paid["count"] or 0,
            "paid_total": paid["total"] or Decimal("0"),
            "is_sandbox": self.sandbox,
        }

    def as_query(self, **overrides) -> str:
        """This filter as a query string, with parts replaced.

        Lets a template link to "the same view, page 2" or "the same view,
        Payme only" without rebuilding the whole thing and losing a filter.
        """
        parts = {
            "period": self.period,
            "provider": self.provider,
            "status": self.status,
            "sandbox": "1" if self.sandbox else "",
        }
        parts.update({k: ("" if v is None else str(v)) for k, v in overrides.items()})
        return "&".join(f"{k}={v}" for k, v in parts.items() if v)
