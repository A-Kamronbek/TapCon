"""Every rate limit in the project, in one file.

Kept together rather than scattered over the views that use them, because the
only way to tell whether a set of limits is sensible is to read them side by
side. Each one says what it is protecting — a limit whose purpose nobody
remembers gets loosened the first time it inconveniences someone.

The numbers are chosen so that a real person having a bad day never meets
one. Someone who mistypes an OTP four times, reloads, and asks for a new code
is well inside every limit here; the ceilings are set where behaviour stops
looking like a person and starts looking like a script.
"""
from django.utils.translation import gettext_lazy as _

from .throttle import client_ip, throttle

MINUTE = 60
HOUR = 3600


def phone_key(request, *args, **kwargs):
    """Count OTP requests per phone number, not per address.

    An attacker draining SMS credit works through a *list* of numbers, so
    every request has a different phone and the same IP; someone trying to
    brute-force one account has the same phone and, over a botnet, a
    different IP each time. Neither key catches both, so the OTP endpoints
    carry one of each.
    """
    posted = (request.POST.get("phone") or "").strip()
    if posted:
        # Normalised, or the limit counts spellings rather than people.
        # "+998901234567", "998 90 123 45 67" and "90-123-45-67" are one
        # account with one password, and an attacker can produce an unbounded
        # number of such variants — each getting its own fresh allowance,
        # which is the same as having no per-number limit at all.
        from accounts.models import normalize_uz_phone

        try:
            return normalize_uz_phone(posted)
        except Exception:  # noqa: BLE001 - not a phone number at all
            return posted
    # Resending a code and entering one both take the number from the
    # session rather than the form, and they are the requests that most need
    # counting per number: they are what a script hammers once it has one.
    pending = (request.session.get("otp_phone") or "").strip()
    return pending or client_ip(request)


def seller_key(request, uid=None, *args, **kwargs):
    """Count per seller, from the uid already in the URL."""
    return uid or client_ip(request)


# --- SMS ------------------------------------------------------------------
# Every code costs money and lands on someone's phone. accounts/services.py
# already limits codes per number per hour (OTP_MAX_PER_HOUR); these sit in
# front of it so a script cannot walk a list of numbers and spend the balance
# a number at a time, which the per-number limit cannot see.
sms_per_ip = throttle("sms.ip", limit=15, period=HOUR)

# Deliberately looser than settings.OTP_MAX_PER_HOUR (5), which is the real
# ceiling on codes *sent* to one number and is enforced in
# accounts/services.py after the form has validated.
#
# This one counts requests, and a request that fails validation sends no SMS
# and costs nothing. At 5 it locked someone out of password reset for an hour
# for mistyping their own number five times — no code had been sent, no money
# spent, and the person who most needs the form is the one who cannot use it.
# Twelve is past any plausible fumbling and still bounds a flood.
sms_per_phone = throttle("sms.phone", limit=12, period=HOUR, key=phone_key)

# --- guessing -------------------------------------------------------------
# A code is six digits and dies after five wrong attempts, so guessing means
# asking for code after code. This is the ceiling on how fast that can go.
otp_attempts = throttle("otp.verify", limit=30, period=HOUR)

# Passwords: slow enough that an online guessing run is pointless, loose
# enough that a person who has forgotten which of their two passwords it is
# never notices. Counted per address and per number for the same reason as
# the SMS limits.
login_per_ip = throttle("login.ip", limit=20, period=15 * MINUTE)
login_per_phone = throttle("login.phone", limit=10, period=15 * MINUTE, key=phone_key)

# --- public forms ---------------------------------------------------------
# Each of these sends a Telegram message to you. Without a limit, the contact
# form is a free way to fill your phone with noise.
#
# Two scopes, not one: ordering a card and asking a question are separate
# things, and someone who has just ordered should not find the contact form
# refused when they think of something to ask.
#
# The count is of *attempts*, not successes, because a limiter that only
# counts what got through is trivially bypassed by sending rubbish. That
# makes the ceiling a question about the clumsiest honest visitor rather than
# the spammer: someone fighting the phone-number format can easily submit a
# form eight or ten times, and locking them out of ordering a card for an
# hour would be a self-inflicted lost sale. Fifteen leaves them room and
# still cuts off anything automated.
order_form = throttle("public.order", limit=15, period=HOUR)
contact_form = throttle("public.contact", limit=15, period=HOUR)

# --- payments -------------------------------------------------------------
# Starting a payment writes a row and calls a provider's API, so a flood is
# expensive on both sides. The per-seller ceiling is set well above a busy
# till — a shop taking a payment every five seconds for a solid minute is
# still inside it — because refusing a real customer at the counter is much
# worse than the flood.
checkout_per_seller = throttle("pay.start.seller", limit=60, period=MINUTE,
                               key=seller_key)
checkout_per_ip = throttle("pay.start.ip", limit=20, period=MINUTE)

# --- webhooks -------------------------------------------------------------
# This is the one limit that can cost real money if it is wrong. A provider
# confirming a payment we refuse means a seller who was paid and never told.
#
# So it is set far above any provider's retry behaviour and counted per
# seller, which is also the only key that makes sense: callbacks for one
# seller arrive from that provider's addresses, and an IP limit would let a
# busy provider's traffic for seller A refuse a callback for seller B.
#
# It still does its job. A forged flood is cut off after 300 in a minute,
# and a refusal is a 429 with Retry-After, which providers treat as
# "come back shortly" rather than as a failed delivery.
webhook_per_seller = throttle("webhook.seller", limit=300, period=MINUTE,
                              key=seller_key, methods=("POST", "GET"))

# Shown on the 429 page. Here so the page and the limits stay in one place.
TOO_MANY = _("Too many attempts. Please wait a moment and try again.")
