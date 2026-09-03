"""Repairs to the tolov SDK, applied once at startup.

Nothing here is a preference. Each entry is a defect that makes a payment
path fail outright, in a library we do not control, where the alternative is
reimplementing the whole flow around it. Each says exactly what breaks
without it, so that when a new tolov release fixes one upstream it is obvious
which patch to delete.
"""
import logging
import re

logger = logging.getLogger("payments")

_APPLIED = False


def _coerce_reason(reason):
    """Whatever tolov passes, turned into something an IntegerField accepts.

    Pulls a signed integer out of a string if there is one — "Error code:
    -5017" becomes -5017, which is the number worth keeping — and gives up
    with None rather than raising.
    """
    if reason is None or isinstance(reason, int):
        return reason
    match = re.search(r"-?\d+", str(reason))
    return int(match.group()) if match else None


def patch_mark_as_cancelled():
    """A declined Click payment must not crash the callback.

    tolov's Click handler cancels with a *sentence*::

        transaction.mark_as_cancelled(reason=f"Error code: {error}")

    and `PaymentTransaction.reason` is an IntegerField. `mark_as_cancelled`
    only converts a string when `str.isdigit()` is true, which is false for
    anything with a minus sign or a word in it, so the value reaches the
    database verbatim and Django raises::

        ValueError: Field 'reason' expected a number but got
        'Error code: -5017'.

    tolov catches that in its own catch-all and answers Click with
    `{"error": -7, "error_note": "Internal error"}`. The consequences run all
    the way to the customer: our transaction is never moved off `pending`, so
    the result page polls "waiting" for ever instead of saying the card was
    declined; the seller's ledger keeps a row that never resolves; and Click,
    reading -7 as a failure, retries a callback that will fail the same way
    every time.

    This is not a PostgreSQL-only problem. SQLite is dynamically typed, but
    Django validates the value before it reaches the driver, so it fails
    identically in development — it had simply never been exercised, because
    every webhook test in the suite used Payme's shape.
    """
    from tolov.integrations.django.models import PaymentTransaction

    original = PaymentTransaction.mark_as_cancelled
    if getattr(original, "_tapcon_patched", False):
        return

    def mark_as_cancelled(self, reason=None):
        coerced = _coerce_reason(reason)
        if reason is not None and coerced != reason:
            logger.info(
                "cancel reason %r from the provider stored as %r", reason, coerced
            )
        return original(self, reason=coerced)

    mark_as_cancelled._tapcon_patched = True
    PaymentTransaction.mark_as_cancelled = mark_as_cancelled


def apply_all():
    """Called once from PaymentsConfig.ready()."""
    global _APPLIED
    if _APPLIED:
        return
    patch_mark_as_cancelled()
    _APPLIED = True
