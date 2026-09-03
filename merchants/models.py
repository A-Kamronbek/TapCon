"""Seller profile: the business behind an NFC card.

Sellers register and configure themselves freely, but their pay page only
goes live once an admin approves the profile.
"""
import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# No 0/O/1/l/i — these get read off a printed card and typed by hand.
UID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
UID_LENGTH = 6


def generate_uid() -> str:
    return "".join(secrets.choice(UID_ALPHABET) for _ in range(UID_LENGTH))


class SellerStatus(models.TextChoices):
    PENDING = "pending", _("Awaiting approval")
    APPROVED = "approved", _("Approved")
    SUSPENDED = "suspended", _("Suspended")


class SellerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="seller_profile",
        verbose_name=_("user"),
    )
    # Public id in the NFC link: /pay/<uid>/. Random, not sequential, so the
    # number of sellers isn't public and cards can't be guessed.
    uid = models.CharField(_("pay link id"), max_length=12, unique=True, db_index=True)

    business_name = models.CharField(_("business name"), max_length=120)
    logo = models.ImageField(_("logo"), upload_to="seller_logos/", blank=True, null=True)
    contact_phone = models.CharField(_("contact phone"), max_length=17, blank=True)
    address = models.CharField(_("address"), max_length=255, blank=True)

    # Pay page settings
    welcome_text = models.CharField(_("welcome text"), max_length=200, blank=True)
    thank_you_text = models.CharField(_("thank-you text"), max_length=200, blank=True)
    min_amount = models.DecimalField(
        _("minimum amount"),
        max_digits=12,
        decimal_places=2,
        default=settings.SELLER_DEFAULT_MIN_AMOUNT,
    )
    max_amount = models.DecimalField(
        _("maximum amount"),
        max_digits=12,
        decimal_places=2,
        default=settings.SELLER_DEFAULT_MAX_AMOUNT,
    )

    status = models.CharField(
        _("status"), max_length=16, choices=SellerStatus.choices, default=SellerStatus.PENDING
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_sellers",
        verbose_name=_("approved by"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("seller")
        verbose_name_plural = _("sellers")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.business_name} ({self.uid})"

    def save(self, *args, **kwargs):
        if not self.uid:
            # Collisions are vanishingly rare but cheap to rule out.
            for _attempt in range(10):
                candidate = generate_uid()
                if not SellerProfile.objects.filter(uid=candidate).exists():
                    self.uid = candidate
                    break
            else:
                raise RuntimeError("Could not allocate a unique seller uid")
        super().save(*args, **kwargs)

    @property
    def is_live(self) -> bool:
        """Only an approved seller can take payments."""
        return self.status == SellerStatus.APPROVED

    def get_pay_url(self) -> str:
        return reverse("payments:pay", kwargs={"uid": self.uid})

    def approve(self, by=None):
        """Switch the seller on and tell them so in their notification bell."""
        from core.services import notify_seller_approved

        was_live = self.is_live
        self.status = SellerStatus.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = by
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
        if not was_live:
            notify_seller_approved(self)

    def suspend(self):
        from core.services import notify_seller_suspended

        was_live = self.is_live
        self.status = SellerStatus.SUSPENDED
        self.save(update_fields=["status", "updated_at"])
        if was_live:
            notify_seller_suspended(self)
