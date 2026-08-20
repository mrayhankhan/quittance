"""Paisa-exact money arithmetic.

Every rupee amount in this codebase is an ``int`` number of paisa. There are no
floats anywhere in the money path, and that is a load-bearing decision rather
than a stylistic one: reconciliation is an equality test, and floating point
does not have exact equality. ``0.1 + 0.2 != 0.3`` is a curiosity in most
programs and a wrong journal entry here.

The only place a float is permitted is display formatting, at the very edge.
"""

from __future__ import annotations

# Basis points, so rate arithmetic also stays integral.
# 200 bps = 2.00% MDR, 1800 bps = 18.00% GST.
BPS = 10_000


def rupees(amount: str | int) -> int:
    """Parse a rupee string such as ``"942.31"`` into paisa (94231).

    Accepts ints as whole rupees. Rejects anything with sub-paisa precision
    rather than silently truncating it -- a settlement file carrying three
    decimal places means an assumption is wrong upstream, and we want to hear
    about it now instead of at month end.
    """
    if isinstance(amount, int):
        return amount * 100

    text = amount.strip().replace(",", "").replace("₹", "")
    negative = text.startswith("-")
    if negative:
        text = text[1:]

    if "." in text:
        whole, _, frac = text.partition(".")
        if len(frac) > 2:
            raise ValueError(f"sub-paisa precision in {amount!r}")
        frac = frac.ljust(2, "0")
    else:
        whole, frac = text, "00"

    value = int(whole or "0") * 100 + int(frac)
    return -value if negative else value


def fmt(paisa: int) -> str:
    """Format paisa for display: ``94231`` -> ``"942.31"``."""
    sign = "-" if paisa < 0 else ""
    p = abs(paisa)
    return f"{sign}{p // 100}.{p % 100:02d}"


def inr(paisa: int) -> str:
    """Format paisa with an Indian-grouped rupee symbol: ``"₹9,42,31.17"``."""
    sign = "-" if paisa < 0 else ""
    p = abs(paisa)
    whole, frac = divmod(p, 100)

    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])

    return f"{sign}₹{digits}.{frac:02d}"


def apply_bps(base_paisa: int, rate_bps: int) -> int:
    """Apply a basis-point rate with half-up rounding, in integers only.

    Half-up is what Indian payment processors and the GST rules both use. It is
    also *not* what Python's ``round`` does -- ``round(2.5) == 2`` -- which is a
    real source of single-paisa drift if you reach for the builtin.
    """
    if base_paisa < 0:
        return -apply_bps(-base_paisa, rate_bps)
    numerator = base_paisa * rate_bps
    return (numerator + BPS // 2) // BPS


def within(a: int, b: int, tolerance_paisa: int = 0) -> bool:
    """True when two amounts agree inside an explicit tolerance.

    Tolerance defaults to zero. Every caller that loosens it has to say so at
    the call site, which keeps sloppy matching from creeping in by default.
    """
    return abs(a - b) <= tolerance_paisa
