from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import ProviderIntegration, Transaction


@admin.register(ProviderIntegration)
class ProviderIntegrationAdmin(admin.ModelAdmin):
    """Credentials are never shown here either — only whether they are set."""

    list_display = (
        "seller", "provider", "is_enabled", "is_test_mode",
        "credentials_set", "updated_at",
    )
    list_filter = ("provider", "is_enabled", "is_test_mode")
    search_fields = ("seller__business_name", "seller__uid", "seller__user__phone")
    readonly_fields = ("credentials_set", "created_at", "updated_at")
    exclude = ("credentials_blob",)
    autocomplete_fields = ("seller",)

    @admin.display(boolean=True, description=_("credentials set"))
    def credentials_set(self, obj):
        return obj.is_complete


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """Read-only on purpose.

    A payment's status is agreed between the customer, the provider and us.
    Editing it by hand in admin would put our ledger out of step with the
    provider's, and nothing would notice.
    """

    list_display = (
        "uid", "seller", "provider", "amount", "status", "is_test_mode", "created_at",
    )
    list_filter = ("status", "provider", "is_test_mode", "created_at")
    search_fields = ("uid", "provider_txn_id", "seller__business_name", "seller__uid")
    date_hierarchy = "created_at"
    autocomplete_fields = ("seller",)
    readonly_fields = (
        "uid", "seller", "provider", "amount", "status", "provider_txn_id",
        "raw_payload", "is_test_mode", "created_at", "updated_at", "paid_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
