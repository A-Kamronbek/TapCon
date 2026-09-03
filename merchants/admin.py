from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from .models import SellerProfile, SellerStatus


@admin.register(SellerProfile)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ("business_name", "uid", "user", "status", "created_at", "approved_at")
    list_filter = ("status", "created_at")
    search_fields = ("business_name", "uid", "user__phone", "contact_phone")
    readonly_fields = ("uid", "created_at", "updated_at", "approved_at", "approved_by")
    autocomplete_fields = ("user",)
    actions = ("approve_sellers", "suspend_sellers")

    fieldsets = (
        (None, {"fields": ("user", "uid", "business_name", "logo")}),
        (_("Contact"), {"fields": ("contact_phone", "address")}),
        (
            _("Pay page"),
            {"fields": ("welcome_text", "thank_you_text", "min_amount", "max_amount")},
        ),
        (_("Approval"), {"fields": ("status", "approved_at", "approved_by")}),
        (_("Dates"), {"fields": ("created_at", "updated_at")}),
    )

    # Both actions loop rather than calling .update(): each seller must get
    # their own notification, which a bulk update would skip.
    @admin.action(description=_("Approve selected sellers"))
    def approve_sellers(self, request, queryset):
        count = 0
        for profile in queryset.exclude(status=SellerStatus.APPROVED):
            profile.approve(by=request.user)
            count += 1
        self.message_user(
            request,
            ngettext("%d seller approved.", "%d sellers approved.", count) % count,
            messages.SUCCESS,
        )

    @admin.action(description=_("Suspend selected sellers"))
    def suspend_sellers(self, request, queryset):
        count = 0
        for profile in queryset.exclude(status=SellerStatus.SUSPENDED):
            profile.suspend()
            count += 1
        self.message_user(
            request,
            ngettext("%d seller suspended.", "%d sellers suspended.", count) % count,
            messages.WARNING,
        )
