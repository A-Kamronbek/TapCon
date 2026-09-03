"""Card orders, contact messages, and in-app notifications."""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.models import normalize_uz_phone, phone_validator


class OrderStatus(models.TextChoices):
    NEW = "new", _("New")
    CONTACTED = "contacted", _("Contacted")
    DELIVERED = "delivered", _("Delivered")
    CANCELLED = "cancelled", _("Cancelled")


class CardOrder(models.Model):
    """Someone wants NFC cards. Handled by hand: we call them back."""

    full_name = models.CharField(_("full name"), max_length=150)
    phone = models.CharField(
        _("phone number"), max_length=17, validators=[phone_validator]
    )
    address = models.CharField(_("delivery address"), max_length=255)
    quantity = models.PositiveSmallIntegerField(
        _("number of cards"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
    )
    comment = models.TextField(_("comment"), max_length=1000, blank=True)
    status = models.CharField(
        _("status"), max_length=16, choices=OrderStatus.choices, default=OrderStatus.NEW
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("card order")
        verbose_name_plural = _("card orders")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.full_name} — {self.quantity}"

    def save(self, *args, **kwargs):
        self.phone = normalize_uz_phone(self.phone)
        super().save(*args, **kwargs)

    @property
    def total_price(self):
        """Card cost only. Connecting merchant accounts is quoted separately."""
        return self.quantity * settings.CARD_PRICE_UZS


class ContactMessage(models.Model):
    full_name = models.CharField(_("full name"), max_length=150)
    phone = models.CharField(
        _("phone number"), max_length=17, validators=[phone_validator]
    )
    message = models.TextField(_("message"), max_length=2000)
    is_handled = models.BooleanField(_("handled"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("contact message")
        verbose_name_plural = _("contact messages")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        self.phone = normalize_uz_phone(self.phone)
        super().save(*args, **kwargs)


class NotificationLevel(models.TextChoices):
    INFO = "info", _("Information")
    SUCCESS = "success", _("Good news")
    WARNING = "warning", _("Attention")


class Notification(models.Model):
    """A message for one seller, shown in the bell in the portal header.

    Used instead of SMS for things like approval: it costs nothing, it is
    already translated, and the seller sees it next time they sign in.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name=_("user"),
    )
    title = models.CharField(_("title"), max_length=150)
    body = models.CharField(_("text"), max_length=500, blank=True)
    level = models.CharField(
        _("kind"), max_length=16, choices=NotificationLevel.choices,
        default=NotificationLevel.INFO,
    )
    url = models.CharField(_("link"), max_length=200, blank=True)
    is_read = models.BooleanField(_("read"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self):
        return f"{self.user} — {self.title}"
