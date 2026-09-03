"""Two callbacks arriving at the same instant, on a real database.

Everything that stops a payment being recorded twice is a read followed by a
write: `_find_account` reads `is_settled`, `_advance` reads the status and
then moves it. Between those two moments another worker can be doing the
same thing. `select_for_update` is what makes that safe — and **SQLite
ignores `SELECT ... FOR UPDATE` entirely**, so under the development database
these tests would pass whether the lock were there or not. They are therefore
skipped on anything but PostgreSQL rather than passing dishonestly.

Gunicorn runs several workers. A provider retrying a callback while its first
attempt is still in flight is ordinary, and a customer with the checkout page
open in two tabs is not exotic. So this is the shape of the two Phase 8 money
defects again, with the sequence compressed to zero.

Run with:

    python manage.py test payments.test_concurrency --settings=config.settings.pgtest
"""
import base64
import json
import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase

from accounts.services import register_seller
from merchants.models import SellerProfile
from payments.models import Transaction, TransactionStatus
from payments.services import save_credentials, set_enabled, set_test_mode

AMOUNT = Decimal("50000")
WORKERS = 8
# A race is probabilistic. Ten rounds turns "sometimes" into "reliably".
ROUNDS = 10

postgres_only = unittest.skipUnless(
    connection.vendor == "postgresql",
    "row locks are a no-op on SQLite; run with --settings=config.settings.pgtest",
)


@postgres_only
class ConcurrentWebhookTests(TransactionTestCase):
    """Fire the same callback from several workers at the same moment."""

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
        self.txn = Transaction.objects.create(
            seller=self.seller, provider="payme", amount=AMOUNT,
            status=TransactionStatus.PENDING, is_test_mode=False,
        )

    # --- helpers -------------------------------------------------------

    def rpc(self, method, params):
        """One JSON-RPC call, on this thread's own database connection."""
        from django.test import Client

        return Client().post(
            self.url,
            json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": method, "params": params}),
            content_type="application/json", **self.auth,
        ).json()

    def create(self, provider_id):
        return self.rpc("CreateTransaction", {
            "id": provider_id, "time": 1756800000000,
            "amount": int(AMOUNT * 100), "account": {"id": str(self.txn.pk)},
        })

    def in_parallel(self, work):
        """Run `work(i)` on `WORKERS` threads released together."""
        start = threading.Barrier(WORKERS)
        results, errors = [None] * WORKERS, []

        def run(i):
            try:
                start.wait(timeout=20)
                results[i] = work(i)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,))
                   for i in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse([t for t in threads if t.is_alive()],
                         "a worker never finished — a lock was never released")
        self.assertEqual(errors, [], f"a worker raised: {errors[:1]}")
        return results

    # --- the races -----------------------------------------------------

    def test_the_same_callback_from_eight_workers_pays_once(self):
        """A provider retrying while its first attempt is still in flight."""
        self.create("a" * 24)
        answers = self.in_parallel(
            lambda _: self.rpc("PerformTransaction", {"id": "a" * 24})
        )

        self.assertTrue(all("result" in a for a in answers),
                        "a genuine retry was answered with an error, which a "
                        f"provider reads as 'stop retrying': {answers}")
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PAID)
        self.assertEqual(self.txn.provider_txn_id, "a" * 24)
        self.assertEqual(
            Transaction.objects.filter(status=TransactionStatus.PAID).count(), 1
        )

    def test_eight_simultaneous_new_payments_open_at_most_one(self):
        """Two tabs, both tapped. Only one charge may ever be opened.

        This is the concurrent form of the defect that let a customer pay
        twice against one transaction: `_find_account` reads `is_settled`,
        and without a lock every worker reads it before any of them writes.

        A race does not lose every time, so once is not a test. Without the
        lock this loses roughly two rounds in five; over ten rounds it is a
        near certainty, and with the lock it cannot lose at all.
        """
        for round_ in range(ROUNDS):
            self.txn = Transaction.objects.create(
                seller=self.seller, provider="payme", amount=AMOUNT,
                status=TransactionStatus.PENDING, is_test_mode=False,
            )
            answers = self.in_parallel(
                lambda i, r=round_: self.create(f"{r:02}{i:0>22}")
            )
            opened = [a for a in answers if "result" in a]
            self.assertEqual(
                len(opened), 1,
                f"round {round_}: {len(opened)} charges were opened against "
                f"one payment — the customer is asked for {AMOUNT} once per "
                f"tab, and only one of them can ever be recorded: {answers}",
            )

    def test_only_one_worker_can_move_a_status(self):
        """The lock itself, contended directly.

        This is the test the other three are not: they go through tolov's own
        deduplication, which absorbs a repeat before it ever reaches
        `move_to`. Removing `select_for_update` leaves all three green — so
        they exercise the lock without proving it.

        Here a Perform and a Cancel arrive on one pending payment at the same
        instant, which is ordinary: a customer taps Cancel on the provider's
        page while the confirmation is already in flight. Both read the row,
        both find `pending`, both find their own transition legal. Without the
        lock they both write and the later one wins, so **a payment that was
        taken can end up recorded as cancelled** — the seller is not credited
        and the customer's receipt disagrees with the ledger.

        With the lock the second worker waits, re-reads, and finds a status
        that refuses it. Exactly one move may succeed.
        """
        from payments.models import InvalidTransition

        outcomes, lock = [], threading.Lock()

        def work(i):
            target = (TransactionStatus.PAID if i % 2 == 0
                      else TransactionStatus.CANCELLED)
            txn = Transaction.objects.get(pk=self.txn.pk)
            try:
                changed = txn.move_to(target, provider_txn_id=f"{i:0>24}")
            except InvalidTransition:
                changed = False
            with lock:
                outcomes.append((target, changed))
            return changed

        self.in_parallel(work)

        moved = [t for t, changed in outcomes if changed]
        self.assertEqual(
            len(moved), 1,
            "two workers both moved the same payment — the later write wins "
            f"and the status is whichever arrived last: {outcomes}",
        )
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, moved[0])
        if self.txn.status == TransactionStatus.CANCELLED:
            self.assertIsNone(self.txn.paid_at,
                              "a cancelled payment is carrying a paid_at")

    def test_a_second_charge_racing_a_confirmation_is_refused(self):
        """Confirming one payment while another worker opens a new one."""
        self.create("b" * 24)

        def work(i):
            if i % 2 == 0:
                return self.rpc("PerformTransaction", {"id": "b" * 24})
            return self.create(f"c{i:0>23}")

        self.in_parallel(work)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TransactionStatus.PAID)
        self.assertEqual(self.txn.provider_txn_id, "b" * 24,
                         "the row was re-pointed at a second charge")
        self.assertEqual(
            Transaction.objects.filter(status=TransactionStatus.PAID).count(), 1
        )
