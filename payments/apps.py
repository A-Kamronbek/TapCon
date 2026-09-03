from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"
    verbose_name = _("Payments")

    def ready(self):
        # Repairs to defects in the tolov SDK that break a payment path
        # outright. See payments/patches.py — each one says what fails
        # without it, so it is clear which to drop when tolov fixes them.
        from . import patches

        patches.apply_all()
