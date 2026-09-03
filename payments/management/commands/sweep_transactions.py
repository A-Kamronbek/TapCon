"""Close out payments that will never resolve on their own.

    python manage.py sweep_transactions            # report only
    python manage.py sweep_transactions --apply    # actually close them

Run nightly from cron, alongside the backup.

Two things leave a row stuck for ever:

* A customer opens the checkout and walks away. The row stays `created` and
  nothing will ever move it.
* A provider takes the money and its callback never arrives. The row stays
  `pending` while the money really moved.

They look identical in the ledger and they are completely different. So this
closes the first kind and **refuses to touch the second**: anything that has
been `pending` long enough to be suspicious is reported for a human to
reconcile against the provider's cabinet, never quietly marked failed.
Writing "failed" on a payment that actually succeeded is precisely the lie a
seller would act on.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from payments.models import InvalidTransition, Transaction, TransactionStatus

# A customer who has not reached the provider in this long is not coming back.
ABANDONED_AFTER = timedelta(hours=2)

# Past this, a `pending` row means the callback is genuinely lost, not late.
# Providers retry for hours, so this is deliberately generous.
UNRECONCILED_AFTER = timedelta(hours=24)


class Command(BaseCommand):
    help = "Close abandoned checkouts and report payments that never resolved."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes. Without it, only report.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        now = timezone.now()

        abandoned = Transaction.objects.filter(
            status=TransactionStatus.CREATED,
            created_at__lt=now - ABANDONED_AFTER,
        )
        count = abandoned.count()
        self.stdout.write(
            f"{count} checkout(s) abandoned before reaching a provider "
            f"(older than {ABANDONED_AFTER})"
        )
        if apply_changes and count:
            closed = 0
            for txn in abandoned.iterator():
                try:
                    txn.move_to(TransactionStatus.FAILED,
                                payload={"swept": "abandoned before checkout"})
                    closed += 1
                except InvalidTransition:
                    # A callback landed while we were iterating. Leave it.
                    pass
            self.stdout.write(self.style.SUCCESS(f"  closed {closed}"))
        elif count:
            self.stdout.write("  (dry run — pass --apply to close them)")

        # Never touched, only reported. A `pending` row is a payment that may
        # well have succeeded, and the only way to know is the provider's
        # cabinet.
        unreconciled = (
            Transaction.objects.filter(
                status=TransactionStatus.PENDING,
                created_at__lt=now - UNRECONCILED_AFTER,
            )
            .select_related("seller")
            .order_by("created_at")
        )
        stuck = unreconciled.count()
        self.stdout.write(
            f"\n{stuck} payment(s) still pending after {UNRECONCILED_AFTER} — "
            "these need reconciling by hand, not closing"
        )
        for txn in unreconciled[:25]:
            self.stdout.write(
                f"  {txn.created_at:%Y-%m-%d %H:%M}  {txn.seller.uid:8} "
                f"{txn.provider:10} {txn.amount:>12}  {txn.uid}"
            )
        if stuck > 25:
            self.stdout.write(f"  ... and {stuck - 25} more")
        if stuck:
            self.stdout.write(
                self.style.WARNING(
                    "  Check each against the provider's cabinet. If the money "
                    "moved, the payment is real and the callback was lost."
                )
            )

        by_status = dict(
            Transaction.objects.values_list("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        self.stdout.write(f"\nledger: {by_status}")
