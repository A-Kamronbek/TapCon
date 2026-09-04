"""Marketing site, card orders, contact, and the notification bell."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
# NOT aliased: xgettext extracts by function name and does not know an
# alias, so `gettext_lazy as _lazy` silently skips every string.
from django.utils.translation import gettext_lazy
from django.views.generic import TemplateView

from .forms import CardOrderForm, ContactForm
from .limits import contact_form, order_form
from .models import Notification
from .seo import page_meta
from .services import place_card_order, submit_contact_message


class MarketingView(TemplateView):
    """Shared by every marketing page. `nav` highlights the current link.

    These are the only pages that ask to be indexed. Setting `url_name` is
    what opts a page in: it produces the canonical link and the hreflang set,
    and `indexable` flips the robots tag. A page that forgets both is simply
    not indexed, which is the right way round for a project where the wrong
    thing to publish would be a seller's payment form.

    `meta_title` and `meta_description` live here rather than in the template
    because each is needed in two places the template cannot fill from one
    block — see core/seo.py.
    """

    nav = ""
    url_name = ""
    indexable = True
    meta_title = ""
    meta_description = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_meta(
                self.url_name,
                self.meta_title,
                self.meta_description,
                nav=self.nav,
                indexable=self.indexable,
            )
        )
        return context


class HomeView(MarketingView):
    template_name = "core/home.html"
    url_name = "core:home"
    meta_title = gettext_lazy("TapCon — accept payments with a single tap")
    meta_description = gettext_lazy(
        "An NFC card for your business. The customer taps it, enters the "
        "amount and pays with Click, Payme, Uzum or a bank card — straight "
        "to your own merchant account. No terminal, no monthly fee."
    )


class HowItWorksView(MarketingView):
    template_name = "core/how_it_works.html"
    url_name = "core:how_it_works"
    nav = "how"
    meta_title = gettext_lazy("How it works — TapCon")
    meta_description = gettext_lazy(
        "Three steps: the customer taps your card, their phone opens your "
        "payment page, they pay. The money goes to your merchant account and "
        "you see it immediately. No app to install."
    )


class ForBusinessView(MarketingView):
    template_name = "core/for_business.html"
    url_name = "core:for_business"
    nav = "business"
    meta_title = gettext_lazy("For business — TapCon")
    meta_description = gettext_lazy(
        "For cafes, shops, salons, taxis and couriers in Uzbekistan. Take "
        "cashless payments without a POS terminal — one card on the counter "
        "and every payment lands in your own account."
    )


class PricingView(MarketingView):
    template_name = "core/pricing.html"
    url_name = "core:pricing"
    nav = "pricing"
    meta_title = gettext_lazy("Pricing — TapCon")
    meta_description = gettext_lazy(
        "100 000 soum for the card, paid once. No commission on payments, no "
        "monthly fee, no contract. Your customers' money goes directly to "
        "your merchant account — TapCon never holds it."
    )


class AboutView(MarketingView):
    template_name = "core/about.html"
    url_name = "core:about"
    meta_title = gettext_lazy("About us — TapCon")
    meta_description = gettext_lazy(
        "TapCon makes cashless payments simple for small businesses in "
        "Uzbekistan. Who we are, why we built it, and how to reach us."
    )


class FaqView(MarketingView):
    template_name = "core/faq.html"
    url_name = "core:faq"
    nav = "faq"
    meta_title = gettext_lazy("Questions and answers — TapCon")
    meta_description = gettext_lazy(
        "Which phones work, which payment methods are supported, when the "
        "money arrives, what happens if a payment fails, and how to get a "
        "card for your business."
    )


class PrivacyView(MarketingView):
    template_name = "core/privacy.html"
    url_name = "core:privacy"
    meta_title = gettext_lazy("Privacy policy — TapCon")
    meta_description = gettext_lazy(
        "What TapCon collects, why, how long it is kept, and who it is "
        "shared with. TapCon never sees or stores your customers' card "
        "details."
    )


class TermsView(MarketingView):
    template_name = "core/terms.html"
    url_name = "core:terms"
    meta_title = gettext_lazy("Terms of service — TapCon")
    meta_description = gettext_lazy(
        "The terms for sellers using TapCon: what the service does, what it "
        "does not do, how payments are settled, and why refunds are handled "
        "by your payment provider rather than by TapCon."
    )


class StyleGuideView(MarketingView):
    """Living reference for the design system. Dev-facing, not linked publicly."""

    template_name = "core/styleguide.html"
    indexable = False
    meta_title = gettext_lazy("Design system — TapCon")


@order_form
def order(request):
    """Ordering a card is a marketing page that happens to have a form on it.

    It was a plain `render()` for a long time, which meant it never received
    the SEO context the class-based pages get — so it went out as `noindex,
    nofollow` while `sitemap.xml` listed it. Google reports that pair as
    "Submitted URL marked noindex", and the page a buyer most needs to find
    was the one asking not to be found.
    """
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

    context = page_meta(
        "core:order",
        gettext_lazy("Order an NFC card — TapCon"),
        gettext_lazy(
            "Order a TapCon card for your business. 100 000 soum, one "
            "payment, delivered in Tashkent. Leave your number and we will "
            "call you back."
        ),
    )
    context["form"] = form
    return render(request, "core/order.html", context)


class OrderDoneView(MarketingView):
    """Deliberately not indexed: a confirmation page is nobody's search result."""

    template_name = "core/order_done.html"
    indexable = False
    meta_title = gettext_lazy("Order received — TapCon")


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

    context = page_meta(
        "core:contact",
        gettext_lazy("Contact — TapCon"),
        gettext_lazy(
            "Questions about TapCon, or want a card for your business? Call "
            "us, message us on Telegram, or leave your number and we will "
            "call you back."
        ),
        nav="contact",
    )
    context["form"] = form
    return render(request, "core/contact.html", context)


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
