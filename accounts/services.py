"""Account business logic: registration, phone OTP, password reset.

Views stay thin and the future DRF API (v2) calls these same functions.
Every entry point takes and returns plain values — no request objects.
"""
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import OTPPurpose, PhoneOTP, hash_otp, normalize_uz_phone
from .sms import SMSError, send_sms

logger = logging.getLogger("accounts")
User = get_user_model()


class OTPThrottled(Exception):
    """Too many codes requested. ``retry_after`` is in seconds."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class OTPInvalid(Exception):
    """Wrong, expired, already used, or out of attempts."""


def _generate_code() -> str:
    upper = 10 ** settings.OTP_LENGTH
    return str(secrets.randbelow(upper)).zfill(settings.OTP_LENGTH)


# Eskiz approves one exact string per template and rejects anything that does
# not match it character for character. So these are NOT translated: the text
# must stay identical whatever language the visitor is browsing in, and it must
# stay identical to what was submitted for moderation. Change the wording here
# only together with a new moderation request.
SMS_TEMPLATES = {
    OTPPurpose.REGISTER: (
        "Tapcon.uz saytidan ro'yxatdan o'tish uchun kodingiz: %(code)s"
    ),
    OTPPurpose.RESET: (
        "Tapcon.uz saytida parolni tiklash uchun kodingiz: %(code)s"
    ),
}


def sms_template(purpose: str) -> str:
    try:
        return SMS_TEMPLATES[purpose]
    except KeyError:
        raise ValueError(f"No moderated SMS template for purpose {purpose!r}")


def issue_otp(phone: str, purpose: str = OTPPurpose.REGISTER) -> PhoneOTP:
    """Create and send a fresh code, enforcing cooldown and hourly cap.

    Any earlier unconsumed code for the same phone+purpose is invalidated, so
    only the newest code ever works.
    """
    phone = normalize_uz_phone(phone)
    now = timezone.now()

    recent = PhoneOTP.objects.filter(phone=phone, purpose=purpose).order_by("-created_at")

    last = recent.first()
    if last:
        elapsed = (now - last.created_at).total_seconds()
        cooldown = settings.OTP_RESEND_COOLDOWN_SECONDS
        if elapsed < cooldown:
            raise OTPThrottled(
                _("Please wait before requesting another code."),
                retry_after=int(cooldown - elapsed),
            )

    hour_ago = now - timedelta(hours=1)
    if recent.filter(created_at__gte=hour_ago).count() >= settings.OTP_MAX_PER_HOUR:
        raise OTPThrottled(_("Too many codes requested. Try again in an hour."), retry_after=3600)

    code = _generate_code()

    with transaction.atomic():
        # Burn any still-open codes so a stale SMS can't be replayed.
        recent.filter(consumed_at__isnull=True).update(consumed_at=now)
        otp = PhoneOTP.objects.create(
            phone=phone,
            purpose=purpose,
            code_hash=hash_otp(phone, code),
            expires_at=now + timedelta(seconds=settings.OTP_TTL_SECONDS),
        )

    message = sms_template(purpose) % {"code": code}
    try:
        send_sms(phone, message)
    except SMSError:
        logger.exception("Could not deliver OTP to %s", phone)
        raise

    return otp


def verify_otp(phone: str, code: str, purpose: str = OTPPurpose.REGISTER) -> PhoneOTP:
    """Consume the newest open code. Raises OTPInvalid on any failure.

    Two things make the five-attempt limit real:

    * The row is **locked** while it is read and consumed, so two correct
      submissions racing cannot both succeed.
    * The counter is incremented with **F()**, not a value read earlier in the
      request. Without it a hundred guesses sent at once all read
      `attempts = 0`, all write `1`, and five attempts cost an attacker one.

    The increment deliberately happens **outside** the transaction. Raising
    inside an atomic block rolls the block back — including the increment —
    so wrapping the whole function in `@transaction.atomic` would mean every
    wrong guess undid its own counter and the limit never advanced at all.
    That is worse than the race it was meant to fix, and it is invisible
    except in a test that reads the counter back.
    """
    phone = normalize_uz_phone(phone)

    with transaction.atomic():
        otp = (
            PhoneOTP.objects.select_for_update()
            .filter(phone=phone, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            raise OTPInvalid(_("No active code. Request a new one."))
        if otp.is_expired:
            raise OTPInvalid(_("The code has expired. Request a new one."))
        if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            raise OTPInvalid(_("Too many wrong attempts. Request a new code."))

        correct = otp.matches(code)
        if correct:
            otp.consumed_at = timezone.now()
            otp.save(update_fields=["consumed_at"])

    if not correct:
        PhoneOTP.objects.filter(pk=otp.pk).update(attempts=F("attempts") + 1)
        raise OTPInvalid(_("Incorrect code."))

    return otp


@transaction.atomic
def register_seller(
    *,
    phone: str,
    full_name: str,
    business_name: str,
    password: str = "",
    password_hash: str = "",
):
    """Create (or take over) an account and its pending seller profile.

    An unverified account on the same number is reused rather than blocking
    the number forever — someone who abandoned signup can start again.

    **Call this only after the code has been verified.** It rewrites the
    password and the business name of any unverified account on that number,
    so calling it straight from the registration form let anyone who merely
    knew an unverified seller's phone number lock them out and rename the
    business on their live pay page, with no proof of anything.
    `accounts/views.py` therefore holds the submitted details in the session
    until `verify_otp` has passed.

    `password_hash` exists so the view never has to keep a plaintext password
    in the session store while it waits for the code.
    """
    from merchants.models import SellerProfile, SellerStatus

    phone = normalize_uz_phone(phone)
    user = User.objects.filter(phone=phone).first()

    if user is not None and user.phone_verified:
        raise ValueError(_("This number is already registered."))

    if user is None:
        user = User(phone=phone)

    user.full_name = full_name
    user.is_seller = True
    user.phone_verified = False
    if password_hash:
        user.password = password_hash
    else:
        user.set_password(password)
    user.save()

    profile, created = SellerProfile.objects.get_or_create(
        user=user, defaults={"business_name": business_name}
    )
    if not created and profile.business_name != business_name:
        # An approval is an admin's judgement about a *particular* business.
        # Someone taking over the number and renaming it is a different
        # business, so it goes back in the queue rather than inheriting a live
        # pay page under a name nobody checked.
        profile.business_name = business_name
        fields = ["business_name"]
        if profile.status == SellerStatus.APPROVED:
            profile.status = SellerStatus.PENDING
            fields.append("status")
            logger.warning(
                "Seller %s was renamed on re-registration and is pending again",
                profile.uid,
            )
        profile.save(update_fields=fields)
    return user


def mark_phone_verified(phone: str):
    phone = normalize_uz_phone(phone)
    user = User.objects.filter(phone=phone).first()
    if user is None:
        raise OTPInvalid(_("Account not found."))
    if not user.phone_verified:
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
    return user


def set_new_password(phone: str, password: str):
    phone = normalize_uz_phone(phone)
    user = User.objects.filter(phone=phone).first()
    if user is None:
        raise OTPInvalid(_("Account not found."))
    user.set_password(password)
    user.save(update_fields=["password"])
    return user
