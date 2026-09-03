"""Merchant portal.

Sellers can reach and configure everything while pending — only the public pay
page is gated on approval.

Every figure a seller reads as money goes through `payments/reporting.py`,
which is `real()` and `paid` by construction. Nothing here sums amounts
itself: an `aggregate(Sum("amount"))` that forgot the sandbox flag or counted
a refund would inflate a seller's takings, and it would look right.
"""
import csv

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from payments.forms import ProviderCredentialsForm
from payments.models import TransactionStatus
from payments.providers import PROVIDERS
from payments.reporting import (
    PERIODS,
    STATUS_FILTERS,
    TransactionFilter,
    by_provider,
    daily_series,
    period_start,
    recent,
    totals,
)
from payments.services import (
    IntegrationError,
    connected_count,
    enabled_providers,
    integrations_for,
    save_credentials,
    set_enabled,
    set_test_mode,
)

from .forms import BusinessForm, PayPageForm, ProfileForm, StyledPasswordChangeForm
from .services import get_profile, update_business, update_profile


class SellerRequiredMixin(LoginRequiredMixin):
    """Every merchant page needs a logged-in user with a seller profile.

    The profile check is not decoration. `get_profile` returns `None` for any
    account without one, and every view here then hands that `None` to code
    that dereferences it — `connected_count(None)` raises `AttributeError`,
    `set_enabled(None, …)` raises IntegrityError, the CSV export needs
    `profile.uid` for its filename.

    That is one step away, not a corner case: `LOGIN_REDIRECT_URL` is this
    dashboard, so a superuser made with `createsuperuser` who signs in through
    the public form lands directly on a 500.
    """

    active = ""

    def dispatch(self, request, *args, **kwargs):
        # After LoginRequiredMixin has had its say, so an anonymous visitor
        # still gets the sign-in redirect rather than this message. One place
        # rather than per-method, so HEAD and POST are covered too.
        if request.user.is_authenticated and get_profile(request.user) is None:
            messages.info(
                request,
                _("This account is not a seller account, so it has no "
                  "merchant pages."),
            )
            return redirect("core:home")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active"] = self.active
        context["profile"] = get_profile(self.request.user)
        return context


class DashboardView(SellerRequiredMixin, TemplateView):
    """One glance: how much came in today, through what, and lately.

    Everything here is `real()` and `paid` — a sandbox payment is a real row
    with a real amount and no money behind it, and a refund is money that
    went back. Neither belongs in a figure a seller reads as their takings.
    """

    template_name = "merchants/dashboard.html"
    active = "dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = context["profile"]

        context["totals"] = totals(profile)
        context["by_provider"] = by_provider(profile, since=period_start("month"))
        context["series"] = daily_series(profile, days=14)
        context["recent"] = recent(profile, limit=5)
        context["connected"], context["provider_count"] = connected_count(profile)
        # A dashboard with a chart but no payments is an empty grid with a
        # sad axis. Until there is something to draw, say so instead.
        context["has_payments"] = any(
            row["total"] for row in context["series"]
        ) or bool(context["recent"])
        return context


class PlaceholderView(SellerRequiredMixin, TemplateView):
    """A screen a later phase will build. Heading is lazy so it translates."""

    template_name = "merchants/placeholder.html"
    heading = ""
    phase = 0

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["heading"] = self.heading
        context["phase"] = self.phase
        return context


class PaymentPageView(SellerRequiredMixin, TemplateView):
    """What the customer sees, plus a live preview of their own pay page."""

    template_name = "merchants/payment_page.html"
    active = "paypage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", PayPageForm(instance=context["profile"]))
        return context

    def post(self, request, *args, **kwargs):
        profile = get_profile(request.user)
        form = PayPageForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, _("Your payment page is updated."))
            return redirect("merchants:payment_page")
        return self.render_to_response(self.get_context_data(form=form))


class IntegrationsView(SellerRequiredMixin, TemplateView):
    """One accordion row per provider, driven entirely by the registry."""

    template_name = "merchants/integrations.html"
    active = "integrations"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = context["profile"]
        integrations = integrations_for(profile)
        forms = kwargs.get("forms") or {}
        # After a save the seller should land back on the row they were
        # editing, not on a page of six collapsed panels.
        open_provider = kwargs.get("open_provider") or self.request.GET.get("open", "")

        # A seller can have both card aggregators switched on, but a customer
        # is only ever offered one — two buttons both saying "Bank card" is a
        # choice nobody can make. Say so on the row that is not the one being
        # used, rather than leaving the seller to wonder why their second card
        # provider never takes a payment.
        offered = {i.provider for i in enabled_providers(profile)}
        card_route = next(
            (
                integrations[key].label
                for key in PROVIDERS
                if PROVIDERS[key].get("card_route") and key in offered
            ),
            "",
        )

        rows = []
        for key, integration in integrations.items():
            rows.append(
                {
                    "key": key,
                    "integration": integration,
                    "logo_url": integration.logo_url,
                    "form": forms.get(key)
                    or ProviderCredentialsForm(provider=key, integration=integration),
                    "open": key == open_provider,
                    "not_offered": integration.is_live and key not in offered,
                }
            )
        context["rows"] = rows
        context["card_route"] = card_route
        context["connected"], context["total"] = connected_count(profile)
        return context

    @staticmethod
    def _back_to(provider: str):
        """Redirect that reopens the row the seller was working on."""
        url = reverse("merchants:integrations")
        if provider in PROVIDERS:
            return redirect(f"{url}?open={provider}#p-{provider}")
        return redirect(url)

    def post(self, request, *args, **kwargs):
        profile = get_profile(request.user)
        provider = request.POST.get("provider", "")
        action = request.POST.get("action", "save")

        if provider not in PROVIDERS:
            messages.error(request, _("Unknown payment provider."))
            return redirect("merchants:integrations")

        if action in {"enable", "disable"}:
            try:
                set_enabled(profile, provider, action == "enable")
            except IntegrationError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    _("%(provider)s is on.") % {"provider": PROVIDERS[provider]["label"]}
                    if action == "enable"
                    else _("%(provider)s is off.")
                    % {"provider": PROVIDERS[provider]["label"]},
                )
            return self._back_to(provider)

        if action == "test_mode":
            set_test_mode(profile, provider, request.POST.get("test_mode") == "on")
            messages.success(request, _("Saved."))
            return self._back_to(provider)

        integration = integrations_for(profile)[provider]
        form = ProviderCredentialsForm(
            request.POST, provider=provider, integration=integration
        )
        if form.is_valid():
            save_credentials(profile, provider, form.merged_credentials())
            messages.success(request, _("Saved."))
            return self._back_to(provider)

        context = self.get_context_data(forms={provider: form}, open_provider=provider)
        return self.render_to_response(context)


class TransactionsView(SellerRequiredMixin, TemplateView):
    """The seller's ledger: what came in, through what, and what became of it.

    Sandbox rows are excluded unless asked for. They are real rows with real
    amounts and no money behind them, so a seller scanning their own takings
    must not find any mixed in.
    """

    template_name = "merchants/transactions.html"
    active = "transactions"
    per_page = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = context["profile"]

        filters = TransactionFilter(self.request.GET, profile)
        paginator = Paginator(filters.queryset(), self.per_page)
        # `get_page` rather than `page`: an out-of-range or non-numeric page
        # number in a URL is a typo or a stale link, not something to answer
        # with an error page.
        page = paginator.get_page(self.request.GET.get("page"))

        context["filters"] = filters
        context["page"] = page
        context["summary"] = filters.summary()
        # A returned payment is the one status a seller cannot have caused
        # from inside TapCon, so it gets a line of explanation — but only
        # when one is actually on screen. A permanent notice about something
        # that almost never happens is just noise.
        context["has_returned"] = any(
            txn.status == TransactionStatus.REFUNDED for txn in page.object_list
        )
        context["periods"] = PERIODS
        context["status_filters"] = STATUS_FILTERS
        context["providers"] = PROVIDERS
        context["export_query"] = filters.as_query()
        return context


@login_required
def transactions_csv(request):
    """The rows currently on screen, as a file.

    Streamed rather than built in memory: a busy seller's year is more rows
    than belong in one string, and the response starts arriving immediately.
    """
    profile = get_profile(request.user)
    if profile is None:
        # A function view, so SellerRequiredMixin's guard does not cover it.
        # Without this an account with no seller profile gets an
        # AttributeError building the filename.
        return redirect("core:home")
    filters = TransactionFilter(request.GET, profile)

    def rows():
        writer = csv.writer(Echo())

        # Excel on Windows reads a bare UTF-8 CSV as cp1251 and turns every
        # Cyrillic and every o'zbek apostrophe into rubbish. The BOM is what
        # makes it open the file correctly, and this is a file sellers will
        # open in Excel.
        yield "﻿"

        yield writer.writerow([
            str(_("Date")),
            str(_("Reference")),
            str(_("Provider")),
            str(_("Amount")),
            str(_("Status")),
            str(_("Mode")),
        ])

        for txn in filters.queryset().iterator(chunk_size=500):
            yield writer.writerow([
                timezone.localtime(txn.created_at).strftime("%Y-%m-%d %H:%M"),
                txn.uid,
                txn.provider_label,
                # Plain digits, no thousands separator: this column is going
                # into a spreadsheet to be summed, not read.
                f"{txn.amount:.0f}",
                str(txn.get_status_display()),
                str(_("Sandbox") if txn.is_test_mode else _("Live")),
            ])

    stamp = timezone.localtime().strftime("%Y-%m-%d")
    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="tapcon-{profile.uid}-{stamp}.csv"'
    )
    return response


class Echo:
    """A file-like object that returns what it is given.

    csv.writer insists on writing somewhere; this hands each finished row back
    so it can be yielded rather than accumulated.
    """

    def write(self, value):
        return value


class SettingsView(SellerRequiredMixin, TemplateView):
    """Three independent forms on one page, told apart by a hidden field."""

    template_name = "merchants/settings.html"
    active = "settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("profile_form", ProfileForm(instance=self.request.user))
        context.setdefault("business_form", BusinessForm(instance=context["profile"]))
        context.setdefault("password_form", StyledPasswordChangeForm(user=self.request.user))
        return context

    def post(self, request, *args, **kwargs):
        which = request.POST.get("form")
        profile = get_profile(request.user)

        if which == "profile":
            form = ProfileForm(request.POST, instance=request.user)
            if form.is_valid():
                update_profile(request.user, full_name=form.cleaned_data["full_name"])
                messages.success(request, _("Your details are saved."))
                return redirect("merchants:settings")
            return self.render_to_response(self.get_context_data(profile_form=form))

        if which == "business":
            form = BusinessForm(request.POST, instance=profile)
            if form.is_valid():
                update_business(profile, **form.cleaned_data)
                messages.success(request, _("Business details are saved."))
                return redirect("merchants:settings")
            return self.render_to_response(self.get_context_data(business_form=form))

        if which == "password":
            form = StyledPasswordChangeForm(user=request.user, data=request.POST)
            if form.is_valid():
                form.save()
                # Without this the seller is logged out by their own change.
                update_session_auth_hash(request, form.user)
                messages.success(request, _("Your password is changed."))
                return redirect("merchants:settings")
            return self.render_to_response(self.get_context_data(password_form=form))

        return redirect("merchants:settings")
