"""The tax layer: turning a settlement deduction into a defensible ITC claim.

This is the part of reconciliation that is actually worth money, and the part
no settlement report can answer on its own.

When Razorpay deducts MDR it also charges 18% GST on that MDR. That GST is
input tax credit -- but only if four things hold at once:

1. the fee is booked as an expense
2. Razorpay issued a GSTIN-bearing tax invoice for it
3. that invoice appears in the merchant's GSTR-2B
4. the GSTR-3B claim matches both

Miss it and the credit is not deferred, it is gone. On ₹10 crore of GMV at
roughly 2% MDR that is about ₹3.6 lakh a year evaporating quietly.

The recurring failure is (3): the supplier files late or files a different
value, so the credit silently never appears in 2B and nobody notices until an
audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .money import apply_bps
from .schema import Dataset, ExceptionCode

GST_BPS = 1_800


class ItcStatus(StrEnum):
    CLAIMABLE = "claimable"
    NOT_IN_2B = "not_in_2b"
    AMOUNT_MISMATCH = "amount_mismatch"
    ROUNDING_DRIFT = "rounding_drift"


@dataclass(frozen=True, slots=True)
class ItcLine:
    period: str
    invoice_no: str
    booked_fee: int
    """MDR summed from the settlement rows -- what the books say."""
    invoiced_fee: int
    """What Razorpay's monthly tax invoice says."""
    invoiced_gst: int
    gstr2b_gst: int | None
    status: ItcStatus
    at_risk: int
    """GST that cannot currently be claimed."""
    detail: str

    @property
    def exception_code(self) -> ExceptionCode | None:
        return {
            ItcStatus.NOT_IN_2B: ExceptionCode.ITC_NOT_IN_2B,
            ItcStatus.AMOUNT_MISMATCH: ExceptionCode.ITC_AMOUNT_MISMATCH,
            ItcStatus.ROUNDING_DRIFT: ExceptionCode.ROUNDING_DRIFT,
        }.get(self.status)


def reconcile_itc(ds: Dataset) -> list[ItcLine]:
    """Match booked fees to tax invoices to GSTR-2B, one period at a time."""
    booked: dict[str, int] = {}
    booked_tax: dict[str, int] = {}
    for row in ds.recon_rows:
        if row.settled_at and row.fee:
            period = row.settled_at.strftime("%Y-%m")
            booked[period] = booked.get(period, 0) + row.fee
            booked_tax[period] = booked_tax.get(period, 0) + row.tax

    by_period = {e.period: e for e in ds.gstr2b}
    out: list[ItcLine] = []

    for inv in sorted(ds.tax_invoices, key=lambda i: i.period):
        fee = booked.get(inv.period, 0)
        entry = by_period.get(inv.period)

        if entry is None:
            status, at_risk = ItcStatus.NOT_IN_2B, inv.gst
            detail = (
                f"invoice {inv.invoice_no} absent from GSTR-2B; supplier has not filed. "
                f"ITC not claimable this period."
            )
        elif entry.gst != inv.gst:
            status, at_risk = ItcStatus.AMOUNT_MISMATCH, abs(inv.gst - entry.gst)
            detail = (
                f"GSTR-2B shows GST {entry.gst} against invoice {inv.gst}; "
                f"claim the lower figure and raise the difference with the supplier."
            )
        elif booked_tax.get(inv.period, 0) != inv.gst:
            # Per-row GST was rounded per transaction; the invoice rounds the
            # monthly aggregate. A few paisa of drift is expected and benign,
            # but it must be surfaced rather than absorbed.
            drift = abs(booked_tax.get(inv.period, 0) - inv.gst)
            status, at_risk = ItcStatus.ROUNDING_DRIFT, 0
            detail = (
                f"{drift} paisa rounding drift between per-row GST and the monthly "
                f"invoice. Expected; book the invoice figure."
            )
        else:
            status, at_risk = ItcStatus.CLAIMABLE, 0
            detail = "invoice present in GSTR-2B and agrees with books; ITC claimable."

        out.append(
            ItcLine(
                period=inv.period,
                invoice_no=inv.invoice_no,
                booked_fee=fee,
                invoiced_fee=inv.taxable_value,
                invoiced_gst=inv.gst,
                gstr2b_gst=entry.gst if entry else None,
                status=status,
                at_risk=at_risk,
                detail=detail,
            )
        )

    return out


def expected_gst(fee_paisa: int) -> int:
    """GST on a fee, half-up, integers only."""
    return apply_bps(fee_paisa, GST_BPS)
