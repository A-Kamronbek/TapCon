"""Prove a restored database is actually usable, not merely present.

    python manage.py verify_restore

Restoring a dump is the easy half. The half that goes wrong is silent: if
FIELD_ENCRYPTION_KEY does not match the one the data was written with, the
site comes back looking perfectly healthy — sellers sign in, the ledger is
intact, every past payment is there — and not one seller can take a payment,
because their provider credentials are permanently unreadable.

This decrypts one credential per connected seller and says so out loud.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from merchants.models import SellerProfile
from payments.models import ProviderIntegration, Transaction


class Command(BaseCommand):
    help = "Check that a restored database can still be read and decrypted."

    def handle(self, *args, **options):
        problems = []

        sellers = SellerProfile.objects.count()
        transactions = Transaction.objects.count()
        integrations = ProviderIntegration.objects.count()
        self.stdout.write(
            f"{sellers} sellers, {transactions} transactions, "
            f"{integrations} provider integrations"
        )
        if sellers == 0:
            problems.append("no sellers at all — is this the right database?")

        # The real test. Every stored credential blob is decrypted and read.
        readable, unreadable, empty = 0, [], 0
        for integration in ProviderIntegration.objects.select_related("seller"):
            try:
                credentials = integration.credentials
            except Exception as exc:  # noqa: BLE001 - any failure is the answer
                unreadable.append(
                    f"{integration.seller.uid}/{integration.provider}: "
                    f"{type(exc).__name__}"
                )
                continue
            if not credentials:
                empty += 1
            else:
                readable += 1

        self.stdout.write(
            f"credentials: {readable} readable, {empty} empty, "
            f"{len(unreadable)} unreadable"
        )
        for line in unreadable[:10]:
            self.stdout.write(self.style.ERROR(f"  {line}"))

        if unreadable:
            problems.append(
                f"{len(unreadable)} credential blob(s) could not be decrypted. "
                "FIELD_ENCRYPTION_KEY does not match the one this data was "
                "written with. See deploy/RESTORE.md."
            )
        elif readable == 0 and integrations > 0:
            problems.append(
                "every integration decrypted to nothing, which is what a "
                "wrong key can also look like"
            )

        # A seller whose pay page is live but whose credentials are gone would
        # show a customer a payment button that cannot work.
        live_but_empty = [
            integration.seller.uid
            for integration in ProviderIntegration.objects.filter(
                is_enabled=True
            ).select_related("seller")
            if not integration.credentials
        ]
        if live_but_empty:
            problems.append(
                f"{len(live_but_empty)} enabled integration(s) have no "
                f"credentials: {', '.join(sorted(set(live_but_empty))[:5])}"
            )

        by_status = dict(
            Transaction.objects.values_list("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        self.stdout.write(f"transactions by status: {by_status}")

        if problems:
            for problem in problems:
                self.stdout.write(self.style.ERROR(f"FAIL {problem}"))
            raise CommandError("the restore is not usable")

        self.stdout.write(
            self.style.SUCCESS(
                "OK - the data is present and every credential decrypts"
            )
        )
