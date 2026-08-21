"""Domain model.

Field names on :class:`ReconRow` deliberately mirror Razorpay's
``GET /v1/settlements/recon/combined`` response so the generator produces
something shaped like the real export and a reader can check it against the
published API reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class EntityType(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class Channel(StrEnum):
    """Where the order originated. Razorpay only ever sees ``WEBSITE``."""

    WEBSITE = "website"
    AMAZON = "amazon"
    FLIPKART = "flipkart"


# --------------------------------------------------------------------------
# The three sources being reconciled
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconRow:
    """One line of the Razorpay settlement recon report."""

    entity_id: str
    type: EntityType
    debit: int
    credit: int
    amount: int
    fee: int
    tax: int
    settled: bool
    settled_at: date | None
    created_at: date
    settlement_id: str | None
    settlement_utr: str | None
    payment_id: str | None
    order_id: str | None
    order_receipt: str | None
    method: str
    card_network: str | None = None
    currency: str = "INR"
    description: str = ""

    @property
    def net(self) -> int:
        """Contribution of this row to the settlement's net bank credit."""
        return self.credit - self.debit - self.fee - self.tax


@dataclass(frozen=True, slots=True)
class BankLine:
    """One credit line on the merchant's bank statement."""

    line_id: str
    value_date: date
    narration: str
    amount: int
    utr: str | None
    """``None`` when the narration was truncated past the UTR by the bank."""


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One order in the merchant's own system. The third leg of the match."""

    order_id: str
    order_receipt: str
    amount: int
    order_date: date
    channel: Channel
    status: str = "paid"


@dataclass(frozen=True, slots=True)
class TaxInvoice:
    """Razorpay's monthly GSTIN-bearing invoice for fees and GST on fees."""

    invoice_no: str
    period: str  # "2026-07"
    taxable_value: int  # total MDR
    gst: int  # GST charged on that MDR
    gstin: str


@dataclass(frozen=True, slots=True)
class Gstr2bEntry:
    """A line as it appears in the government's auto-drafted GSTR-2B."""

    invoice_no: str
    period: str
    taxable_value: int
    gst: int
    supplier_gstin: str


@dataclass(frozen=True, slots=True)
class Dataset:
    """Everything a reconciliation run consumes."""

    recon_rows: tuple[ReconRow, ...]
    bank_lines: tuple[BankLine, ...]
    orders: tuple[OrderRow, ...]
    tax_invoices: tuple[TaxInvoice, ...]
    gstr2b: tuple[Gstr2bEntry, ...]
    seed: int
    injected: tuple[str, ...] = ()
    """Defect labels the generator injected. Used only to score the run."""
    truth: dict[str, str] = field(default_factory=dict)
    """Answer key: ``bank_line_id -> settlement_id``.

    The pipeline never reads this. It exists so the metrics harness can
    distinguish a correct match from a confident wrong one, which is the
    number this whole design is built around.
    """


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


class ExceptionCode(StrEnum):
    """Typed reasons a row could not be auto-matched.

    An honest, specific exception list is the deliverable. "Unexplained" is a
    last resort, and a run that produces many of them is telling you the
    taxonomy is incomplete, not that the data was bad.
    """

    MISSING_ORDER = "missing_order"
    DUPLICATE_PAYMENT = "duplicate_payment"
    AMOUNT_MISMATCH = "amount_mismatch"
    UNSETTLED = "unsettled"
    DUPLICATE_UTR = "duplicate_utr"
    NARRATION_TRUNCATED = "narration_truncated"
    LATE_REFUND = "late_refund"
    CHARGEBACK_REVERSAL = "chargeback_reversal"
    FX_CONVERSION = "fx_conversion"
    ROUNDING_DRIFT = "rounding_drift"
    ITC_NOT_IN_2B = "itc_not_in_2b"
    ITC_AMOUNT_MISMATCH = "itc_amount_mismatch"
    UNEXPLAINED = "unexplained"


class Layer(StrEnum):
    EXACT = "L0_exact"
    SOLVER = "L1_solver"
    LLM = "L2_llm"


@dataclass(frozen=True, slots=True)
class Match:
    """A settlement batch tied to a bank line, with provenance.

    ``layer`` and ``rule`` are not decoration. Every match in the audit trail
    can be traced to the specific piece of logic that produced it, which is the
    difference between a reconciliation and a guess.
    """

    bank_line_id: str
    settlement_id: str
    amount: int
    layer: Layer
    rule: str
    confidence: float
    evidence: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True, slots=True)
class Exception_:
    """A row the pipeline refused to auto-match, and why."""

    ref: str
    code: ExceptionCode
    detail: str
    amount: int = 0
    layer: Layer | None = None


@dataclass(slots=True)
class ReconResult:
    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    rejected: list[Match] = field(default_factory=list)
    """LLM proposals the verifier threw out. Kept deliberately -- this list is
    evidence the gate is doing work."""

    @property
    def matched_amount(self) -> int:
        return sum(m.amount for m in self.matches)

    @property
    def unexplained_amount(self) -> int:
        return sum(e.amount for e in self.exceptions)
