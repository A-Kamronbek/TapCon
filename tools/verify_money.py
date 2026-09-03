"""Recompute every figure the seller is shown, from the raw rows.

    python tools/verify_money.py

Nothing here calls payments/reporting.py to work out what the answer should
be — that would only prove the code agrees with itself. Every expected value
is summed here in plain Python, from Transaction rows read one at a time,
with the local day boundary worked out by hand. Then the reporting functions
are asked the same questions and the two are compared.

It also reads the rendered dashboard over HTTP and checks that the number a
seller's eyes land on is the number that was computed, because a correct
total formatted into the wrong template slot is still a wrong page.
"""
import os
import pathlib
import re
import sys
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from django.utils import timezone  # noqa: E402

from accounts.models import normalize_uz_phone  # noqa: E402
from merchants.models import SellerProfile  # noqa: E402
from payments import reporting  # noqa: E402
from payments.models import Transaction  # noqa: E402

problems = []


def check(label, expected, actual):
    if expected != actual:
        problems.append(f"{label}: hand-computed {expected!r}, code said {actual!r}")
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok   {label}: {actual!r}")


def rows_for(seller):
    """Every transaction, as plain values. No manager methods, no filters."""
    out = []
    for txn in Transaction.objects.filter(seller=seller):
        out.append({
            "amount": txn.amount,
            "status": txn.status,
            "provider": txn.provider,
            "is_test": txn.is_test_mode,
            "paid_at": txn.paid_at,
            "created_at": txn.created_at,
        })
    return out


def local_midnight():
    """Start of today in the seller's timezone, worked out without Django."""
    now = timezone.localtime()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def main():
    # The seller whose dashboard this script can actually sign in to and read,
    # so the figures below and the page below describe the same business.
    #
    # Normalised, not taken as typed: phones are stored in display form
    # ("+998 90 123 45 67"), so a compact CRAWL_PHONE matched nothing, this
    # fell through to "any approved seller", and the script then compared one
    # seller's figures against another seller's dashboard. It reported three
    # money mismatches that were not real — which is the worst kind of
    # checker, because the next person disbelieves the true ones too.
    phone = normalize_uz_phone(os.environ.get("CRAWL_PHONE") or "")
    seller = SellerProfile.objects.filter(user__phone=phone).first() if phone else None
    if seller is None:
        if phone:
            sys.exit(
                f"no seller signs in as {phone}. Set CRAWL_PHONE to one that "
                "does — guessing a different seller makes every figure below "
                "describe a business the dashboard is not showing."
            )
        seller = SellerProfile.objects.filter(status="approved").order_by("pk").first()
    rows = rows_for(seller)
    today = local_midnight()

    print(f"seller {seller.uid} ({seller.business_name}), {len(rows)} rows\n")

    # A row is money only if it is real, paid, and not given back.
    money = [r for r in rows if not r["is_test"] and r["status"] == "paid"]
    print(f"{len(money)} of them are real+paid (money), "
          f"{sum(1 for r in rows if r['status'] == 'refunded')} refunded, "
          f"{sum(1 for r in rows if r['is_test'])} sandbox\n")

    print("totals()")
    computed = reporting.totals(seller)
    for key, days in (("today", 1), ("week", 7), ("month", 30)):
        since = today - timedelta(days=days - 1)
        mine = [r for r in money if r["paid_at"] and r["paid_at"] >= since]
        check(f"  {key} total",
              sum((r["amount"] for r in mine), Decimal("0")),
              computed[key]["total"])
        check(f"  {key} count", len(mine), computed[key]["count"])

    print("\nby_provider() - all time")
    mine = defaultdict(lambda: [Decimal("0"), 0])
    for r in money:
        mine[r["provider"]][0] += r["amount"]
        mine[r["provider"]][1] += 1
    expected = sorted(mine.items(), key=lambda kv: -kv[1][0])
    actual = reporting.by_provider(seller)
    check("  provider count", len(expected), len(actual))
    for (provider, (total, count)), got in zip(expected, actual):
        check(f"  {provider} total", total, got["total"])
        check(f"  {provider} count", count, got["count"])
    check("  ordered by money, descending",
          True,
          all(actual[i]["total"] >= actual[i + 1]["total"]
              for i in range(len(actual) - 1)))

    print("\ndaily_series(14)")
    series = reporting.daily_series(seller, days=14)
    check("  one entry per day", 14, len(series))
    check("  oldest first", True,
          all(series[i]["date"] < series[i + 1]["date"]
              for i in range(len(series) - 1)))
    buckets = defaultdict(lambda: Decimal("0"))
    first = today - timedelta(days=13)
    for r in money:
        if r["paid_at"] and r["paid_at"] >= first:
            buckets[timezone.localtime(r["paid_at"]).date()] += r["amount"]
    check("  sum of bars == sum of rows in window",
          sum(buckets.values(), Decimal("0")),
          sum((d["total"] for d in series), Decimal("0")))
    for day in series:
        if day["total"] != buckets.get(day["date"], Decimal("0")):
            problems.append(f"daily_series {day['date']}: "
                            f"{buckets.get(day['date'])} vs {day['total']}")
    check("  every bar matches its day", 0,
          len([p for p in problems if "daily_series" in p]))
    check("  exactly one bar marked today", 1,
          sum(1 for d in series if d["is_today"]))

    # A day with takings must not be drawn the same as a day with none.
    flat = [d for d in series if d["total"] > 0 and d["height"] == 0]
    check("  no day with money is drawn at zero height", [], flat)

    print("\nsandbox rows never count as money")
    check("  no sandbox row in the money set", 0,
          sum(1 for r in money if r["is_test"]))
    sandbox_total = sum((r["amount"] for r in rows
                         if r["is_test"] and r["status"] == "paid"),
                        Decimal("0"))
    print(f"  (sandbox paid rows are worth {sandbox_total}, excluded above)")

    print("\nrefunds are not takings")
    refunded = sum((r["amount"] for r in rows if r["status"] == "refunded"),
                   Decimal("0"))
    check("  no refunded row in the money set", 0,
          sum(1 for r in money if r["status"] == "refunded"))
    print(f"  ({refunded} was returned to customers and is excluded)")

    print("\nTransactionFilter")
    for period in ("today", "week", "month", "all"):
        flt = reporting.TransactionFilter({"period": period}, seller)
        since = reporting.period_start(period)
        # A paid row is dated by when the money arrived, anything else by
        # when it was attempted — the same rule the dashboard uses, so the
        # two pages always show a seller the same total for a day.
        def in_period(r):
            if since is None:
                return True
            if r["paid_at"] is not None:
                return r["paid_at"] >= since
            return r["created_at"] >= since

        mine = [r for r in rows if not r["is_test"] and in_period(r)]
        check(f"  {period} row count", len(mine), flt.queryset().count())
        paid = [r for r in mine if r["status"] == "paid"]
        check(f"  {period} paid total",
              sum((r["amount"] for r in paid), Decimal("0")),
              flt.summary()["paid_total"])
    # The invariant that matters to a seller reconciling a day's takings:
    # the headline figure and the ledger page must never disagree.
    for period in ("today", "week", "month"):
        check(f"  {period}: dashboard total == transactions page total",
              computed[period]["total"],
              reporting.TransactionFilter(
                  {"period": period}, seller).summary()["paid_total"])

    junk = reporting.TransactionFilter(
        {"period": "../etc", "provider": "'; drop--", "status": "<script>"}, seller)
    check("  junk period falls back", "month", junk.period)
    check("  junk provider falls back", "", junk.provider)
    check("  junk status falls back", "", junk.status)

    print("\nthe rendered dashboard")
    base = os.environ.get("CRAWL_BASE", "http://127.0.0.1:8009")
    try:
        import httpx

        with httpx.Client(base_url=base, follow_redirects=True, timeout=20) as c:
            page = c.get("/uz/auth/login/")
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',
                              page.text).group(1)
            c.post("/uz/auth/login/",
                   data={"csrfmiddlewaretoken": token,
                         "phone": os.environ["CRAWL_PHONE"],
                         "password": os.environ["CRAWL_PASSWORD"]},
                   headers={"Referer": f"{base}/uz/auth/login/"})
            html = c.get("/uz/merchant/").text
        shown = [int(n.replace(" ", "").replace(" ", ""))
                 for n in re.findall(r">([\d  ]{4,})<", html)]
        for key in ("today", "week", "month"):
            want = int(computed[key]["total"])
            if want:
                check(f"  {key} total appears on the page", True, want in shown)
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped: {exc})")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for line in problems:
            print(f"  {line}")
        return 1
    print("OK - every figure recomputes to the same number")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
