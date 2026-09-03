from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import PhoneOTP, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = ("phone", "full_name", "is_seller", "phone_verified", "is_staff")
    list_filter = ("is_seller", "phone_verified", "is_staff", "is_active")
    search_fields = ("phone", "full_name")
    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        (_("Personal info"), {"fields": ("full_name",)}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_seller",
                    "phone_verified",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("phone", "password1", "password2")}),
    )


@admin.register(PhoneOTP)
class PhoneOTPAdmin(admin.ModelAdmin):
    """Read-only: codes are hashed and must never be editable from here."""

    list_display = ("phone", "purpose", "created_at", "expires_at", "attempts", "consumed_at")
    list_filter = ("purpose", "created_at")
    search_fields = ("phone",)
    readonly_fields = (
        "phone",
        "purpose",
        "code_hash",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
