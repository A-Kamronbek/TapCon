"""Rate limiting.

Small on purpose. A third-party package would bring its own key scheme, its
own response, and its own idea of what to do behind a proxy — and the two
places where TapCon's limits are unusual (a webhook that must never be
refused in practice, and an OTP counted per phone number rather than per IP)
are exactly the places a package's defaults would be wrong.

The counters live in the database cache, so every Gunicorn worker sees the
same number. See the CACHES comment in settings/base.py for why.

Two things this deliberately does NOT do:

* It does not fail closed. If the cache is unreachable the request is
  allowed through. A rate limiter that takes the site down when its counter
  store hiccups has caused a worse outage than the one it prevents — and on
  the pay page, a customer standing at a till would be told to go away.
* It does not use a sliding window. A fixed window is one increment and no
  bookkeeping; the worst it allows is a double burst across a boundary, which
  for "5 SMS per hour" means 10 in a pathological minute, not a flood.
"""
import functools
import hashlib
import logging
import time

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as _

logger = logging.getLogger("core.throttle")

# A client that has tripped a limit is told how long to wait, and the header
# is what a well-behaved provider or browser reads.
RETRY_AFTER = "Retry-After"


def client_ip(request, *args, **kwargs) -> str:
    """The caller's address, trusting X-Forwarded-For only from our own proxy.

    Nginx sets X-Forwarded-For; anyone on the internet can also send one. The
    header is a list and the *last* entry is the one our proxy appended, so
    that is the only one worth reading — taking the first would let a caller
    forge a new identity per request and walk straight through every limit.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _window_key(scope: str, identity: str, period: int) -> str:
    """One cache key per (limit, caller, window).

    The identity is hashed rather than pasted in, because part of it is
    attacker-controlled: the OTP limits are keyed on the `phone` field of an
    unvalidated POST. Used raw, a very long value would produce a key past
    the 255-character cache column and the write would fail — which this
    module deliberately treats as "allow", so sending a 300-character phone
    number would have walked straight through the limit meant to stop it.
    Hashing also removes the spaces in a formatted number, which are illegal
    in a memcached key and warn on every request.
    """
    digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=16).hexdigest()
    return f"throttle:{scope}:{digest}:{int(time.time()) // period}"


def hit(scope: str, identity: str, limit: int, period: int) -> tuple[bool, int, int]:
    """Count one attempt.

    Returns (allowed, seconds until the window resets, attempts used).

    Uses `cache.add` then `cache.incr`: add only writes if the key is absent,
    so two workers racing on the first request of a window cannot both create
    it and lose a count.
    """
    key = _window_key(scope, identity, period)
    reset_in = period - int(time.time()) % period
    try:
        # Expire a little after the window so a counter cannot outlive it.
        cache.add(key, 0, period + 10)
        used = cache.incr(key)
    except ValueError:
        # The key expired between add and incr. That is one lost count at a
        # window boundary, not a reason to refuse anyone.
        return True, reset_in, 0
    except Exception:  # noqa: BLE001 - cache backend unavailable
        logger.exception("rate limit store unavailable; allowing %s", scope)
        return True, reset_in, 0
    return used <= limit, reset_in, used


def _refuse(request, scope, reset_in, used, limit):
    # One line per window, not one per refused request. A flood is exactly the
    # situation where per-request logging turns a nuisance into an outage:
    # ten thousand requests would write ten thousand lines and fill the disk
    # of a small VPS, which is a better result for the attacker than the
    # flood itself. The first refusal is the one worth knowing about.
    if used == limit + 1:
        logger.warning(
            "rate limit hit: %s by %s on %s (limit %s)",
            scope, client_ip(request), request.path, limit,
        )
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or request.path.startswith("/webhooks/")
    )
    if wants_json:
        response = JsonResponse(
            {"error": "too_many_requests", "retry_after": reset_in}, status=429
        )
    else:
        try:
            response = render(
                request, "429.html", {"retry_after": reset_in}, status=429
            )
        except Exception:  # noqa: BLE001 - never fail inside the refusal
            response = HttpResponse(
                _("Too many attempts. Please wait a moment and try again."),
                status=429,
                content_type="text/plain; charset=utf-8",
            )
    response[RETRY_AFTER] = str(reset_in)
    return response


def throttle(scope: str, limit: int, period: int, key=client_ip, methods=("POST",)):
    """Refuse more than `limit` requests per `period` seconds from one caller.

    `key` decides what "one caller" means — an IP by default, but a phone
    number for the OTP limits, because an attacker with a phone list is not
    trying the same number twice and an IP limit would not see them.

    `methods` is POST by default: a GET on these pages renders a form and
    costs nothing worth defending, and limiting it would make an ordinary
    person reloading a page look like an attack.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return view(request, *args, **kwargs)
            # Key functions get the view's own arguments too, so a limit can
            # be counted per seller uid rather than per caller.
            identity = key(request, *args, **kwargs)
            allowed, reset_in, used = hit(scope, str(identity), limit, period)
            if not allowed:
                return _refuse(request, scope, reset_in, used, limit)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
