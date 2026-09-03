from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import CardOrder, ContactMessage, Notification, OrderStatus


@admin.register(CardOrder)
class CardOrderAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "quantity", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("full_name", "phone", "address")
    readonly_fields = ("created_at",)
    list_editable = ("status",)
    actions = ("mark_contacted", "mark_delivered")

    @admin.action(description=_("Mark as contacted"))
    def mark_contacted(self, request, queryset):
        queryset.update(status=OrderStatus.CONTACTED)

    @admin.action(description=_("Mark as delivered"))
    def mark_delivered(self, request, queryset):
        queryset.update(status=OrderStatus.DELIVERED)


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "is_handled", "created_at")
    list_filter = ("is_handled", "created_at")
    search_fields = ("full_name", "phone", "message")
    readonly_fields = ("created_at",)
    list_editable = ("is_handled",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "level", "is_read", "created_at")
    list_filter = ("level", "is_read", "created_at")
    search_fields = ("user__phone", "title", "body")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("user",)
