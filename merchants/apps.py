from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MerchantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "merchants"
    verbose_name = _("Merchants")
