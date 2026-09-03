"""Money formatting.

Amounts are stored as Decimal and always displayed with a space as the
thousands separator, the way sums are written in Uzbekistan: 100 000.
Kept out of Django's locale formatting on purpose — the separator must not
change when the customer switches the page language.

The separator is a NON-BREAKING space so an amount never wraps onto two
lines mid-number, which on a payment page would be genuinely confusing.
"""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()

NBSP = "\u00a0"


@register.filter
def uzs(value):
    """1250000 -> '1 250 000'. Drops a .00 tail, keeps real tiyin."""
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value

    whole = int(amount)
    fraction = amount - whole
    grouped = f"{whole:,}".replace(",", NBSP)

    if fraction:
        return f"{grouped},{str(fraction).split('.')[1][:2].ljust(2, '0')}"
    return grouped
