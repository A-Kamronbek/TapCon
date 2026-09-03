"""Customer-facing pay page.

    /pay/<uid>/                  enter an amount, pick a provider
    /pay/<uid>/go/<provider>/    POST: create the Transaction, leave for the provider
    /pay/<uid>/result/<ref>/     came back — waiting, paid, or not
    /pay/<uid>/status/<ref>/     what the result page polls

None of it is language-prefixed: the address is written onto an NFC card once
and can never change. The customer's language comes from their phone.

Transactions are addressed by their random `uid`, never by primary key: the
customer holds this URL, and sequential ids would let anyone read the next
customer's payment.
"""
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import TemplateView

from core.limits import checkout_per_ip, checkout_per_seller
from merchants.models import SellerProfile

from .forms import AmountForm
from .models import Transaction, TransactionStatus
from .services import PaymentError, enabled_providers, start_payment


def _seller(uid):
    return get_object_or_404(SellerProfile, uid=uid)


def _pay_context(request, profile, form=None, error=""):
    providers = enabled_providers(profile)
    return {
        "profile": profile,
        # An unapproved seller must never be able to take money.
        "is_live": profile.is_live,
        "form": form or AmountForm(seller=profile),
        "providers": providers,
        # Every way of paying is a sandbox one, so no money will actually
        # move. The customer is told, rather than finding out afterwards.
        "all_test_mode": bool(providers) and all(p.is_test_mode for p in providers),
        "payment_error": error,
        "support_phone": settings.SUPPORT_PHONE,
        "support_telegram": settings.SUPPORT_TELEGRAM,
    }


# Where a failed attempt is parked between the POST and the redirect back.
# One pending retry per browser is all that can exist, because a customer is
# on one pay page at a time.
RETRY_SESSION_KEY = "pay_retry"


# The project sends X-Frame-Options: DENY everywhere. The seller's own preview
# in the merchant portal is an iframe of this page, so these views relax that
# to SAMEORIGIN — still no third-party framing of a payment page.
@method_decorator(xframe_options_sameorigin, name="dispatch")
class PayView(TemplateView):
    """/pay/<uid>/ — public, mobile-first, no login."""

    template_name = "payments/pay.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = _seller(kwargs["uid"])

        # A payment that could not be started redirected back here rather than
        # rendering itself at /go/<provider>/ (see `go`). Pick the attempt up,
        # once: a refresh after reading the message should be a clean page.
        form, error = None, ""
        retry = self.request.session.pop(RETRY_SESSION_KEY, None)
        if isinstance(retry, dict) and retry.get("uid") == profile.uid:
            error = retry.get("error", "")
            amount = retry.get("amount")
            if amount is not None:
                # Re-bound rather than carried over: the same input against
                # the same seller's limits reproduces the same field errors,
                # so a bad amount is still flagged next to the box, and a
                # good one that the provider refused simply comes back filled
                # in with the banner above it.
                form = AmountForm({"amount": amount}, seller=profile)

        context.update(_pay_context(self.request, profile, form, error=error))
        return context


@xframe_options_sameorigin
@checkout_per_seller
@checkout_per_ip
def go(request, uid, provider):
    """Create the Transaction and hand the customer to the provider.

    Only POST does anything: a GET would fire on any link prefetch and litter
    the seller's ledger with attempts nobody made. But a GET is answered with
    a redirect to the pay page rather than a 405 — this URL ends up in the
    address bar and in history on the way to the provider, so the browser's
    Back button, a reload, or a prefetcher will all ask for it eventually.

    A failure redirects back to the pay page instead of rendering there. It
    used to render, and the page looked right — but the address bar still said
    /go/octo/, so the language switcher posted `next=/pay/<uid>/go/octo/` and
    the customer got a **405** for changing language after a failed payment.
    Reloading or going back had the same problem. Nothing that answers only
    POST should ever be left showing in a customer's address bar.
    """
    profile = _seller(uid)

    if request.method != "POST":
        return redirect("payments:pay", uid=profile.uid)

    def retry(error="", amount=None):
        """Hand the attempt back to the pay page and go there."""
        request.session[RETRY_SESSION_KEY] = {
            "uid": profile.uid,
            "error": str(error),
            "amount": amount,
        }
        return redirect("payments:pay", uid=profile.uid)

    # Only a provider this page actually offers. `enabled_providers` shows one
    # card route — Octo wins over Multicard — and without this check a POST
    # straight to /go/multicard/ took a payment through the route the product
    # deliberately hides, with no button anywhere that produces it.
    if provider not in {i.provider for i in enabled_providers(profile)}:
        return redirect("payments:pay", uid=profile.uid)

    raw_amount = request.POST.get("amount", "")
    form = AmountForm(request.POST, seller=profile)

    if not form.is_valid():
        return retry(amount=raw_amount)

    def return_url(txn):
        """Where the provider sends the customer back to."""
        return request.build_absolute_uri(
            reverse("payments:result", args=[profile.uid, txn.uid])
        )

    try:
        txn, checkout_url = start_payment(
            profile, provider, form.cleaned_data["amount"], return_url=return_url
        )
    except PaymentError as exc:
        return retry(error=exc, amount=raw_amount)

    return redirect(checkout_url)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class ResultView(TemplateView):
    """Where the provider sends the customer back to."""

    template_name = "payments/result.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = _seller(kwargs["uid"])
        txn = get_object_or_404(Transaction, uid=kwargs["ref"], seller=profile)
        context["profile"] = profile
        context["txn"] = txn
        context["is_settled"] = txn.is_settled
        context["status_url"] = reverse("payments:status", args=[profile.uid, txn.uid])
        context["support_phone"] = settings.SUPPORT_PHONE
        context["support_telegram"] = settings.SUPPORT_TELEGRAM
        return context


def status(request, uid, ref):
    """What the result page polls while the provider's webhook is in flight.

    Deliberately thin: a status string and whether to stop asking. It says
    nothing a customer holding this transaction's own URL cannot already read
    off the page.
    """
    profile = _seller(uid)
    txn = get_object_or_404(Transaction, uid=ref, seller=profile)
    return JsonResponse(
        {
            "status": txn.status,
            "settled": txn.is_settled,
            "paid": txn.status == TransactionStatus.PAID,
        }
    )
