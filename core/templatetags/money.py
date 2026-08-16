"""Rupee formatting with Indian digit grouping (12,34,567 — not 1,234,567)."""

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _group_indian(digits):
    """Group a digit string as 12,34,567: last three, then pairs."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


@register.filter
def rupees(value):
    """``12345.5`` -> ``12,345.50``. Invalid input renders as ``0.00``."""
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return "0.00"

    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    return f"{sign}{_group_indian(whole)}.{frac}"


@register.filter
def rupees_short(value):
    """Same as :func:`rupees` but drops a ``.00`` tail for compact display."""
    text = rupees(value)
    return text[:-3] if text.endswith(".00") else text
