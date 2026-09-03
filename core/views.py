"""Marketing site, card orders, contact, and the notification bell."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from .forms import CardOrderForm, ContactForm
from .limits import contact_form, order_form
from .models import Notification
from .seo import alternates
from .services import place_card_order, submit_contact_message


class MarketingView(TemplateView):
    """Shared by every marketing page. `nav` highlights the current link.

    These are the only pages that ask to be indexed. Setting `url_name` is
    what opts a page in: it produces the canonical link and the hreflang set,
    and `indexable` flips the robots tag. A page that forgets both is simply
    not indexed, which is the right way round for a project where the wrong
    thing to publish would be a seller's payment form.
    """

    nav = ""
    url_name = ""
    indexable = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = self.nav
        if self.url_name and self.indexable:
            context["indexable"] = True
            context.update(alternates(self.url_name))
        return context


class HomeView(MarketingView):
    template_name = "core/home.html"
    url_name = "core:home"


class HowItWorksView(MarketingView):
    template_name = "core/how_it_works.html"
    url_name = "core:how_it_works"
    nav = "how"


class ForBusinessView(MarketingView):
    template_name = "core/for_business.html"
    url_name = "core:for_business"
    nav = "business"


class PricingView(MarketingView):
    template_name = "core/pricing.html"
    url_name = "core:pricing"
    nav = "pricing"


class AboutView(MarketingView):
    template_name = "core/about.html"
    url_name = "core:about"


class FaqView(MarketingView):
    template_name = "core/faq.html"
    url_name = "core:faq"
    nav = "faq"


class PrivacyView(MarketingView):
    template_name = "core/privacy.html"
    url_name = "core:privacy"


class TermsView(MarketingView):
    template_name = "core/terms.html"
    url_name = "core:terms"


class StyleGuideView(MarketingView):
    """Living reference for the design system. Dev-facing, not linked publicly."""

    template_name = "core/styleguide.html"


@order_form
def order(request):
    form = CardOrderForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        place_card_order(
            full_name=data["full_name"],
            phone=data["phone"],
            address=data["address"],
            quantity=data["quantity"],
            comment=data["comment"],
        )
        return redirect("core:order_done")

    return render(request, "core/order.html", {"form": form})


class OrderDoneView(MarketingView):
    template_name = "core/order_done.html"


@contact_form
def contact(request):
    form = ContactForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        submit_contact_message(
            full_name=data["full_name"], phone=data["phone"], message=data["message"]
        )
        messages.success(request, _("Thank you. We will call you back shortly."))
        return redirect("core:contact")

    return render(request, "core/contact.html", {"form": form, "nav": "contact"})


# --- notifications -------------------------------------------------------


@login_required
def notifications(request):
    items = Notification.objects.filter(user=request.user)[:50]
    return render(request, "core/notifications.html", {"items": items})


@login_required
def notifications_read(request):
    """Mark everything read. Answers JSON to the bell, redirects a plain form.

    A GET changes nothing and is sent to the list. It is not `@require_POST`
    because a 405 is a dead end in a browser's address bar: this URL can be
    reloaded, gone back to, or prefetched, and a page nobody asked for is a
    better answer than an error nobody can act on.
    """
    if request.method != "POST":
        return redirect("core:notifications")

    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"unread": 0})
    return redirect(_safe_next(request) or reverse("core:notifications"))


@login_required
def notification_read(request, pk):
    """Mark one notification read, then go where it points.

    Only POST marks anything: a plain <a> would be followed by anything that
    prefetches links. The row is a submit button, so it still works with no
    JavaScript. A GET marks nothing and is sent to the list, rather than
    answering 405 to a reload or a Back button.
    """
    if request.method != "POST":
        return redirect("core:notifications")

    item = get_object_or_404(Notification, pk=pk, user=request.user)
    if not item.is_read:
        item.is_read = True
        item.save(update_fields=["is_read"])

    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"unread": unread, "url": item.url or ""})

    return redirect(item.url or _safe_next(request) or reverse("core:notifications"))


def _safe_next(request):
    """A redirect target from the request, only if it points back at us."""
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return ""
