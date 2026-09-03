"""Business logic for orders, contact messages and notifications."""
import logging

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .models import CardOrder, ContactMessage, Notification, NotificationLevel
from .telegram import send_message

logger = logging.getLogger("core")


def _escape(value) -> str:
    return (
        str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def place_card_order(*, full_name, phone, address, quantity, comment="") -> CardOrder:
    order = CardOrder.objects.create(
        full_name=full_name,
        phone=phone,
        address=address,
        quantity=quantity,
        comment=comment,
    )
    # Notify after the row is safely committed, never inside the transaction.
    transaction.on_commit(lambda: _announce_order(order))
    return order


def _announce_order(order: CardOrder) -> None:
    """Written in Uzbek on purpose, and not through gettext.

    This goes to our own Telegram, not to a visitor, so it must not follow
    whatever language the customer happened to be browsing in.
    """
    ok = send_message(
        "<b>Yangi karta buyurtmasi</b>\n"
        f"Ism: {_escape(order.full_name)}\n"
        f"Telefon: {_escape(order.phone)}\n"
        f"Kartalar: {order.quantity}\n"
        f"Manzil: {_escape(order.address)}\n"
        f"Izoh: {_escape(order.comment) or '-'}"
    )
    if not ok:
        logger.warning("Card order %s saved but not announced to Telegram", order.pk)


def submit_contact_message(*, full_name, phone, message) -> ContactMessage:
    contact = ContactMessage.objects.create(
        full_name=full_name, phone=phone, message=message
    )
    transaction.on_commit(lambda: _announce_message(contact))
    return contact


def _announce_message(contact: ContactMessage) -> None:
    ok = send_message(
        "<b>Yangi xabar</b>\n"
        f"Ism: {_escape(contact.full_name)}\n"
        f"Telefon: {_escape(contact.phone)}\n\n"
        f"{_escape(contact.message)}"
    )
    if not ok:
        logger.warning("Message %s saved but not announced to Telegram", contact.pk)


def notify(user, *, title, body="", level=NotificationLevel.INFO, url="") -> Notification:
    """Put a message in the seller's bell. Titles arrive already translated."""
    return Notification.objects.create(
        user=user, title=str(title), body=str(body), level=level, url=url
    )


def notify_seller_approved(profile) -> Notification:
    from django.urls import reverse

    return notify(
        profile.user,
        title=_("Your business is approved"),
        body=_("Your payment page is live. You can start accepting payments."),
        level=NotificationLevel.SUCCESS,
        url=reverse("merchants:dashboard"),
    )


def notify_seller_suspended(profile) -> Notification:
    return notify(
        profile.user,
        title=_("Your account is suspended"),
        body=_("Your payment page no longer accepts payments. Please contact support."),
        level=NotificationLevel.WARNING,
    )


def unread_count(user) -> int:
    if not user.is_authenticated:
        return 0
    return Notification.objects.filter(user=user, is_read=False).count()
