"""Phone-based user. There is no email field: verification happens by SMS OTP."""
import hashlib
import hmac
import re

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

phone_validator = RegexValidator(
    regex=r"^\+998 \d{2} \d{3} \d{2} \d{2}$",
    message=_("Format: +998 XX XXX XX XX"),
)


def normalize_uz_phone(value: str | None) -> str | None:
    """Accept any spacing/format the user types, store one canonical form."""
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("998"):
        digits = digits[3:]
    if len(digits) == 9:
        return f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:9]}"
    return value


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, phone, password, **extra):
        if not phone:
            raise ValueError(_("Phone number is required."))
        user = self.model(phone=normalize_uz_phone(phone), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, phone, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(phone, password, **extra)

    def create_superuser(self, phone, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        extra.setdefault("phone_verified", True)
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError(_("Superuser must have is_staff and is_superuser set."))
        return self._create_user(phone, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    phone = models.CharField(
        _("phone number"),
        max_length=17,
        unique=True,
        validators=[phone_validator],
        error_messages={"unique": _("This number is already registered.")},
    )
    phone_verified = models.BooleanField(_("phone verified"), default=False)
    full_name = models.CharField(_("full name"), max_length=150, blank=True)
    is_seller = models.BooleanField(_("is seller"), default=False)
    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff status"), default=False)
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self):
        return self.phone

    def save(self, *args, **kwargs):
        self.phone = normalize_uz_phone(self.phone)
        super().save(*args, **kwargs)


class OTPPurpose(models.TextChoices):
    REGISTER = "register", _("Registration")
    RESET = "reset", _("Password reset")


def hash_otp(phone: str, code: str) -> str:
    """Codes are never stored in the clear — not in the DB, not in logs."""
    payload = f"{settings.SECRET_KEY}:{phone}:{code}".encode()
    return hashlib.sha256(payload).hexdigest()


class PhoneOTP(models.Model):
    """One issued SMS code. Single-use, time-limited, attempt-limited."""

    phone = models.CharField(_("phone number"), max_length=17, db_index=True)
    purpose = models.CharField(
        _("purpose"), max_length=16, choices=OTPPurpose.choices, default=OTPPurpose.REGISTER
    )
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("phone code")
        verbose_name_plural = _("phone codes")
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["phone", "purpose", "-created_at"])]

    def __str__(self):
        return f"{self.phone} / {self.purpose}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self) -> bool:
        return (
            self.consumed_at is None
            and not self.is_expired
            and self.attempts < settings.OTP_MAX_ATTEMPTS
        )

    def matches(self, code: str) -> bool:
        # compare_digest rather than ==. The compared value is a hash the
        # attacker cannot precompute, so this is not a practical timing
        # oracle today — but a constant-time comparison of two secrets costs
        # nothing, and the next person to touch this should not have to
        # re-derive why == was safe here.
        return hmac.compare_digest(self.code_hash, hash_otp(self.phone, code))
