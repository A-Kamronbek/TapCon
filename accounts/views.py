"""Auth flows, all by phone number + SMS OTP. No email anywhere.

The pending phone lives in the session between the form and the code entry,
so a code can never be verified for a number the visitor didn't just submit.
"""
from django.contrib import messages
from django.contrib.auth import (
    get_user_model,
    login as auth_login,
    logout as auth_logout,
)
from django.contrib.auth.hashers import make_password
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from core.limits import (
    login_per_ip,
    login_per_phone,
    otp_attempts,
    sms_per_ip,
    sms_per_phone,
)

from .forms import LoginForm, OTPForm, PhoneOnlyForm, RegisterForm, SetPasswordForm
from .models import OTPPurpose
from .services import (
    OTPInvalid,
    OTPThrottled,
    issue_otp,
    mark_phone_verified,
    register_seller,
    set_new_password,
    verify_otp,
)
from .sms import SMSError

# HEAD belongs on every page a browser can reach. Django does not add it to
# require_http_methods for us, and without it a link prefetcher, an uptime
# monitor or a chat app building a link preview gets 405 from a page that
# answers GET perfectly well.
PAGE_METHODS = ["GET", "HEAD", "POST"]

PENDING_PHONE = "otp_phone"
PENDING_PURPOSE = "otp_purpose"
RESET_VERIFIED = "reset_verified_phone"
# The registration details, held until the code proves the number is theirs.
# Never a plaintext password — the view hashes it before it goes in here.
PENDING_REGISTRATION = "pending_registration"

User = get_user_model()


def _safe_next(request):
    """Where to go after signing in — only if it points back at TapCon.

    ``?next=`` arrives from whoever built the link. Following it unchecked
    turns the real sign-in page into a springboard: a seller opens a link,
    signs in against tapcon.uz, sees the real form and the real domain, and
    lands on a page an attacker controls, which then asks for the password
    or provider keys they have just proved they know. Django's own
    LoginView guards this the same way.
    """
    target = request.GET.get("next") or request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return ""


def _start_otp(request, phone, purpose):
    """Send a code and remember who it was sent to. Returns True on success."""
    try:
        issue_otp(phone, purpose)
    except OTPThrottled as exc:
        messages.error(request, str(exc))
        return False
    except SMSError:
        messages.error(request, _("Could not send the SMS. Please try again."))
        return False

    request.session[PENDING_PHONE] = phone
    request.session[PENDING_PURPOSE] = purpose
    return True


@require_http_methods(PAGE_METHODS)
@sms_per_ip
@sms_per_phone
def register(request):
    if request.user.is_authenticated:
        return redirect("merchants:dashboard")

    form = RegisterForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        # Nothing is created or changed here. `register_seller` rewrites the
        # password and business name of any unverified account on the number,
        # so calling it at this point meant anyone who merely knew such a
        # seller's phone number could lock them out and rename the business on
        # their live pay page — no code, no proof, one form submission.
        #
        # The details wait in the session until `verify_otp` passes. The
        # password is hashed first, so a plaintext one never sits in the
        # session store while we wait.
        request.session[PENDING_REGISTRATION] = {
            "phone": data["phone"],
            "full_name": data["full_name"],
            "business_name": data["business_name"],
            "password_hash": make_password(data["password"]),
        }
        if _start_otp(request, data["phone"], OTPPurpose.REGISTER):
            return redirect("accounts:verify")
        request.session.pop(PENDING_REGISTRATION, None)

    return render(request, "accounts/register.html", {"form": form})


@require_http_methods(PAGE_METHODS)
@otp_attempts
def verify(request):
    phone = request.session.get(PENDING_PHONE)
    purpose = request.session.get(PENDING_PURPOSE)
    if not phone or not purpose:
        messages.error(request, _("Start again — we don't know which number to confirm."))
        return redirect("accounts:register")

    form = OTPForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            verify_otp(phone, form.cleaned_data["code"], purpose)
        except OTPInvalid as exc:
            messages.error(request, str(exc))
        else:
            if purpose == OTPPurpose.REGISTER:
                pending = request.session.get(PENDING_REGISTRATION)
                if pending and pending.get("phone") == phone:
                    # The account is created here, not on the form — this is
                    # the first moment we know the person holds the number.
                    try:
                        register_seller(
                            phone=pending["phone"],
                            full_name=pending["full_name"],
                            business_name=pending["business_name"],
                            password_hash=pending["password_hash"],
                        )
                    except ValueError as exc:
                        # The number was verified by someone else between the
                        # form and the code. Say so rather than 500.
                        messages.error(request, str(exc))
                        request.session.pop(PENDING_REGISTRATION, None)
                        request.session.pop(PENDING_PHONE, None)
                        request.session.pop(PENDING_PURPOSE, None)
                        return redirect("accounts:login")
                elif not User.objects.filter(phone=phone).exists():
                    # A confirmed code with nothing to confirm — the session
                    # was lost, so there is no account and nothing to sign in
                    # to. Send them back rather than 500 on mark_phone_verified.
                    messages.error(
                        request, _("Start again — your session expired.")
                    )
                    request.session.pop(PENDING_PHONE, None)
                    request.session.pop(PENDING_PURPOSE, None)
                    return redirect("accounts:register")

                user = mark_phone_verified(phone)
                request.session.pop(PENDING_REGISTRATION, None)
                request.session.pop(PENDING_PHONE, None)
                request.session.pop(PENDING_PURPOSE, None)
                auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                messages.success(request, _("Your number is confirmed. Welcome to TapCon."))
                return redirect("merchants:dashboard")

            # Password reset: hand off to the new-password form.
            request.session[RESET_VERIFIED] = phone
            request.session.pop(PENDING_PHONE, None)
            request.session.pop(PENDING_PURPOSE, None)
            return redirect("accounts:password_reset_set")

    return render(
        request,
        "accounts/verify.html",
        {"form": form, "phone": phone, "resend_url": reverse("accounts:resend")},
    )


@sms_per_ip
@sms_per_phone
def resend(request):
    """Send the code again.

    Only POST sends one — a GET goes back to the code screen. Answering 405
    instead would strand anyone who reloaded or used the Back button on a URL
    that has no page of its own.
    """
    if request.method != "POST":
        return redirect("accounts:verify")

    phone = request.session.get(PENDING_PHONE)
    purpose = request.session.get(PENDING_PURPOSE)
    if not phone or not purpose:
        return redirect("accounts:register")
    if _start_otp(request, phone, purpose):
        messages.success(request, _("A new code is on its way."))
    return redirect("accounts:verify")


@require_http_methods(PAGE_METHODS)
@login_per_ip
@login_per_phone
def login(request):
    if request.user.is_authenticated:
        return redirect("merchants:dashboard")

    form = LoginForm(request.POST if request.method == "POST" else None, request=request)
    if request.method == "POST" and form.is_valid():
        user = form.user
        if not user.phone_verified:
            # Never let an unconfirmed number in; send them back to the code.
            if _start_otp(request, user.phone, OTPPurpose.REGISTER):
                messages.info(request, _("Confirm your number to continue."))
                return redirect("accounts:verify")
            return render(request, "accounts/login.html", {"form": form})

        auth_login(request, user)
        return redirect(_safe_next(request) or "merchants:dashboard")

    return render(request, "accounts/login.html", {"form": form})


@require_http_methods(PAGE_METHODS)
def logout(request):
    """Only POST signs a seller out.

    GET answers with the home page instead of 405, so a reload or a Back
    button is not a dead end — but it must not sign anyone out, or an
    ``<img src=".../logout/">`` on any page on the web would log a seller
    out of their own dashboard. The sidebar posts, with a CSRF token.
    """
    if request.method == "POST":
        auth_logout(request)
    return redirect("core:home")


@require_http_methods(PAGE_METHODS)
@sms_per_ip
@sms_per_phone
def password_reset(request):
    form = PhoneOnlyForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        phone = form.cleaned_data["phone"]
        # Send the code either way — don't reveal whether the number exists.
        _start_otp(request, phone, OTPPurpose.RESET)
        return redirect("accounts:verify")

    return render(request, "accounts/password_reset.html", {"form": form})


@require_http_methods(PAGE_METHODS)
def password_reset_set(request):
    phone = request.session.get(RESET_VERIFIED)
    if not phone:
        return redirect("accounts:password_reset")

    form = SetPasswordForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            set_new_password(phone, form.cleaned_data["password"])
        except OTPInvalid:
            messages.error(request, _("No account uses that number."))
            request.session.pop(RESET_VERIFIED, None)
            return redirect("accounts:password_reset")

        request.session.pop(RESET_VERIFIED, None)
        messages.success(request, _("Password changed. You can sign in now."))
        return redirect("accounts:login")

    return render(request, "accounts/password_reset_set.html", {"form": form, "phone": phone})
