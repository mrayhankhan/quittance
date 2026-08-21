"""Seeded synthetic settlement data with deliberately injected defects.

Reconciliation code is easy to fool with clean data. Every matcher scores 100%
on a file where each bank credit equals exactly one settlement. The defects
below are the ones that actually show up in Indian settlement files, and they
are the reason the pipeline needs four layers instead of one dictionary lookup.

The generator is fully deterministic given a seed. ``make demo`` reproduces the
numbers in the README on any machine, which is the point: a reader can check
the claims rather than take them.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, timedelta

from .money import apply_bps, rupees
from .schema import (
    BankLine,
    Channel,
    Dataset,
    EntityType,
    Gstr2bEntry,
    OrderRow,
    ReconRow,
    TaxInvoice,
)

GST_BPS = 1_800  # 18% GST on the fee

MDR_BPS = {
    "upi": 0,  # zero-MDR by regulation, so no ITC arises
    "card": 200,
    "netbanking": 190,
    "wallet": 210,
    "intl_card": 350,
}

METHOD_WEIGHTS = [("upi", 55), ("card", 28), ("netbanking", 10), ("wallet", 5), ("intl_card", 2)]

SUPPLIER_GSTIN = "29AAGCR4375J1ZU"  # Razorpay Software Pvt Ltd, Karnataka


def _utr(rng: random.Random) -> str:
    return f"{rng.randrange(10**9, 10**10)}{rng.choice('abcdefghjkmnpqrstuvwxyz')}{rng.randrange(10000, 99999)}"


def _pick_method(rng: random.Random) -> str:
    total = sum(w for _, w in METHOD_WEIGHTS)
    roll = rng.randrange(total)
    for method, weight in METHOD_WEIGHTS:
        roll -= weight
        if roll < 0:
            return method
    return "upi"


def _amount(rng: random.Random) -> int:
    """Log-ish distribution: lots of small orders, a few large ones."""
    bucket = rng.random()
    if bucket < 0.55:
        return rupees(f"{rng.randrange(99, 1500)}.{rng.randrange(0, 100):02d}")
    if bucket < 0.90:
        return rupees(f"{rng.randrange(1500, 12000)}.{rng.randrange(0, 100):02d}")
    return rupees(f"{rng.randrange(12000, 95000)}.{rng.randrange(0, 100):02d}")


def generate(
    seed: int = 20260905,
    n_payments: int = 500,
    start: date = date(2026, 7, 1),
    days: int = 21,
) -> Dataset:
    """Build a reproducible dataset. See module docstring for the contract."""
    rng = random.Random(seed)
    injected: list[str] = []

    orders: list[OrderRow] = []
    rows: list[ReconRow] = []

    # ---- payments ---------------------------------------------------------
    for i in range(n_payments):
        created = start + timedelta(days=rng.randrange(days))
        method = _pick_method(rng)
        amount = _amount(rng)
        fee = apply_bps(amount, MDR_BPS[method])
        tax = apply_bps(fee, GST_BPS)

        oid = f"order_{seed % 1000:03d}{i:05d}"
        pid = f"pay_{seed % 1000:03d}{i:05d}"
        receipt = f"RCPT-{2600 + i}"

        orders.append(
            OrderRow(
                order_id=oid,
                order_receipt=receipt,
                amount=amount,
                order_date=created,
                channel=Channel.WEBSITE,
            )
        )
        rows.append(
            ReconRow(
                entity_id=pid,
                type=EntityType.PAYMENT,
                debit=0,
                credit=amount,
                amount=amount,
                fee=fee,
                tax=tax,
                settled=True,
                settled_at=created + timedelta(days=2),
                created_at=created,
                settlement_id=None,  # assigned below
                settlement_utr=None,
                payment_id=pid,
                order_id=oid,
                order_receipt=receipt,
                method=method,
                card_network="visa" if "card" in method else None,
                currency="USD" if method == "intl_card" else "INR",
                description=f"Payment for {receipt}",
            )
        )

    # ---- refunds ----------------------------------------------------------
    # Refunds are initiated against a payment but deducted from a settlement
    # 5-7 working days later, so they land in a batch that has nothing to do
    # with the original sale. This is the single most common reason a naive
    # amount match fails.
    n_refunds = max(1, n_payments // 25)
    for pay in rng.sample([r for r in rows if r.type == EntityType.PAYMENT], n_refunds):
        lag = rng.randrange(5, 8)
        partial = rng.random() < 0.4
        amt = apply_bps(pay.amount, rng.randrange(3000, 7000)) if partial else pay.amount
        rows.append(
            ReconRow(
                entity_id=f"rfnd_{pay.entity_id[4:]}",
                type=EntityType.REFUND,
                debit=amt,
                credit=0,
                amount=amt,
                fee=0,
                tax=0,
                settled=True,
                settled_at=(pay.settled_at or start) + timedelta(days=lag),
                created_at=(pay.settled_at or start) + timedelta(days=1),
                settlement_id=None,
                settlement_utr=None,
                payment_id=pay.payment_id,
                order_id=pay.order_id,
                order_receipt=pay.order_receipt,
                method=pay.method,
                description=f"{'Partial refund' if partial else 'Refund'} for {pay.order_receipt}",
            )
        )
    injected.append(f"late_refund x{n_refunds}")

    # ---- chargebacks ------------------------------------------------------
    n_cb = max(1, n_payments // 120)
    for pay in rng.sample([r for r in rows if r.type == EntityType.PAYMENT], n_cb):
        rows.append(
            ReconRow(
                entity_id=f"adj_{pay.entity_id[4:]}",
                type=EntityType.ADJUSTMENT,
                debit=pay.amount,
                credit=0,
                amount=pay.amount,
                fee=0,
                tax=0,
                settled=True,
                settled_at=(pay.settled_at or start) + timedelta(days=rng.randrange(14, 26)),
                created_at=(pay.settled_at or start) + timedelta(days=13),
                settlement_id=None,
                settlement_utr=None,
                payment_id=pay.payment_id,
                order_id=pay.order_id,
                order_receipt=pay.order_receipt,
                method=pay.method,
                description=f"ADJ-CHGBK-{rng.randrange(1000, 9999)}",
            )
        )
    injected.append(f"chargeback_reversal x{n_cb}")

    # ---- group into settlement batches -----------------------------------
    # Razorpay settles UPI and card rails on separate cycles, so a merchant
    # typically sees two credits land on the same day. That collision is what
    # makes date-based matching ambiguous, and it is why Layer 1 needs a solver
    # rather than a dictionary.
    def rail(method: str) -> str:
        return "upi" if method == "upi" else "cards"

    by_batch: dict[tuple[date, str], list[int]] = {}
    for idx, row in enumerate(rows):
        if row.settled_at is not None:
            by_batch.setdefault((row.settled_at, rail(row.method)), []).append(idx)

    settlements: dict[str, list[int]] = {}
    utrs: dict[str, str] = {}
    for n, key in enumerate(sorted(by_batch)):
        sid = f"setl_{seed % 1000:03d}{n:04d}"
        settlements[sid] = by_batch[key]
        utrs[sid] = _utr(rng)
        for idx in by_batch[key]:
            rows[idx] = replace(rows[idx], settlement_id=sid, settlement_utr=utrs[sid])

    # ---- bank lines -------------------------------------------------------
    bank: list[BankLine] = []
    truth: dict[str, str] = {}
    # Some batches have an amount held back as a rolling reserve, so the bank
    # credit is genuinely less than the rows sum to. Nothing is wrong with the
    # data and nothing can reconcile it -- a human has to go and find the reserve
    # advice. These exist to prove the verifier refuses arithmetic it cannot
    # close, even when the identifier is perfectly good.
    #
    # Defect counts scale with the number of batches rather than being fixed.
    # Fixed counts were a modelling error: they made the file *easier* the larger
    # it got, which is the opposite of how real settlement data behaves.
    n_holds = max(1, len(settlements) // 30) if len(settlements) > 3 else 0
    held = set(rng.sample(sorted(settlements), n_holds)) if n_holds else set()

    for n, (sid, idxs) in enumerate(sorted(settlements.items())):
        net = sum(rows[i].net for i in idxs)
        if net <= 0:
            continue  # a fully-refunded batch produces no credit
        if sid in held:
            net -= rupees(f"{rng.randrange(500, 9000)}.{rng.randrange(0, 100):02d}")
        sdate = rows[idxs[0]].settled_at
        assert sdate is not None
        line_id = f"bank_{n:04d}"
        if sid in held:
            injected.append("reserve_hold")
        bank.append(
            BankLine(
                line_id=line_id,
                value_date=sdate,
                narration=f"NEFT/RZRPY/{utrs[sid]}/SETTLEMENT",
                amount=net,
                utr=utrs[sid],
            )
        )
        truth[line_id] = sid

    # ---- defect injection -------------------------------------------------
    bank, extra = _inject_defects(rng, bank, truth)
    injected.extend(extra)

    # ---- order-ledger defects --------------------------------------------
    # The merchant's own ledger is not clean either. Orders go missing when a
    # sale is created outside the shop system, and captured amounts drift from
    # order values on partial captures. Both are real, both are found only by
    # exploding the settlement down to order level.
    orders, order_notes = _damage_orders(rng, orders)
    injected.extend(order_notes)

    # ---- tax invoices and GSTR-2B ----------------------------------------
    invoices, gstr2b, tax_defects = _build_tax(rng, rows)
    injected.extend(tax_defects)

    return Dataset(
        recon_rows=tuple(rows),
        bank_lines=tuple(bank),
        orders=tuple(orders),
        tax_invoices=tuple(invoices),
        gstr2b=tuple(gstr2b),
        seed=seed,
        injected=tuple(injected),
        truth=truth,
    )


def _inject_defects(
    rng: random.Random, bank: list[BankLine], truth: dict[str, str]
) -> tuple[list[BankLine], list[str]]:
    """Damage the bank statement the way a real bank feed damages it."""
    notes: list[str] = []
    if len(bank) < 6:
        return bank, notes

    # Narration truncated at 35 chars by the bank's own field limit, taking the
    # UTR with it. Layer 0 cannot match these; Layer 1 has to earn them.
    for line in rng.sample(bank, max(1, len(bank) // 6)):
        i = bank.index(line)
        bank[i] = replace(line, narration=line.narration[:35], utr=None)
    notes.append(f"narration_truncated x{max(1, len(bank) // 6)}")

    # Settlements sharing a UTR, because a bank feed copied one narration onto
    # another line. Exact matching must refuse to pick between them.
    n_dupes = max(1, len(bank) // 40)
    dupes = 0
    for a, b in zip(rng.sample(range(len(bank)), n_dupes),
                    rng.sample(range(len(bank)), n_dupes), strict=True):
        if a != b and bank[a].utr and bank[b].utr:
            bank[b] = replace(bank[b], utr=bank[a].utr,
                              narration=f"NEFT/RZRPY/{bank[a].utr}/SETTLEMENT")
            dupes += 1
    if dupes:
        notes.append(f"duplicate_utr x{dupes}")

    # The bank clubs same-day NEFT credits into one line and drops both UTRs.
    # Nothing short of arithmetic can take these apart, which is what Layer 1's
    # subset-sum is for.
    target = max(1, len(bank) // 25)
    clubbed = 0
    i = 0
    while i < len(bank) - 1 and clubbed < target:
        first, second = bank[i], bank[i + 1]
        if (first.value_date == second.value_date and first.utr and second.utr
                and first.line_id in truth and second.line_id in truth):
            bank[i] = BankLine(
                line_id=first.line_id,
                value_date=first.value_date,
                narration="NEFT/RZRPY/CONSOLIDATED CREDIT",
                amount=first.amount + second.amount,
                utr=None,
            )
            truth[first.line_id] = "+".join(
                sorted([truth[first.line_id], truth[second.line_id]])
            )
            del truth[second.line_id]
            bank.pop(i + 1)
            clubbed += 1
            i += 1  # skip past the merged line
            continue
        i += 1
    if clubbed:
        notes.append(f"clubbed_credit x{clubbed}")

    return bank, notes


def _damage_orders(
    rng: random.Random, orders: list[OrderRow]
) -> tuple[list[OrderRow], list[str]]:
    """Introduce the two order-side defects that actually occur."""
    notes: list[str] = []
    if len(orders) < 20:
        return orders, notes

    # Partial capture: the order says one thing, the captured payment another.
    n_mismatch = max(1, len(orders) // 200)
    for o in rng.sample(orders, n_mismatch):
        i = orders.index(o)
        orders[i] = replace(o, amount=o.amount - rupees(f"{rng.randrange(10, 400)}.00"))
    notes.append(f"order_amount_mismatch x{n_mismatch}")

    # Order absent from the merchant ledger entirely.
    n_missing = max(1, len(orders) // 250)
    for o in rng.sample(orders, n_missing):
        orders.remove(o)
    notes.append(f"order_missing x{n_missing}")

    return orders, notes


def _build_tax(
    rng: random.Random, rows: list[ReconRow]
) -> tuple[list[TaxInvoice], list[Gstr2bEntry], list[str]]:
    """Monthly Razorpay tax invoices, and the GSTR-2B the government shows.

    The interesting case is when these two disagree. That gap is the merchant's
    input tax credit quietly failing to exist.
    """
    notes: list[str] = []
    by_period: dict[str, list[ReconRow]] = {}
    for row in rows:
        if row.settled_at and row.fee:
            by_period.setdefault(row.settled_at.strftime("%Y-%m"), []).append(row)

    invoices: list[TaxInvoice] = []
    gstr2b: list[Gstr2bEntry] = []

    for n, (period, prows) in enumerate(sorted(by_period.items())):
        taxable = sum(r.fee for r in prows)
        # Razorpay rounds GST on the monthly aggregate; the per-row taxes were
        # each rounded individually. Those two numbers differ by a few paisa,
        # and that drift is a genuine reconciliation exception, not a bug.
        gst = apply_bps(taxable, GST_BPS)
        inv = TaxInvoice(
            invoice_no=f"RZP/2627/{1200 + n}",
            period=period,
            taxable_value=taxable,
            gst=gst,
            gstin=SUPPLIER_GSTIN,
        )
        invoices.append(inv)

        roll = rng.random()
        if roll < 0.25:
            # Supplier filed late: the invoice is simply absent from 2B, so the
            # credit cannot be claimed this period.
            notes.append(f"itc_not_in_2b {period}")
            continue
        if roll < 0.45:
            # Filed, but for a different taxable value.
            drift = rng.randrange(100, 900)
            gstr2b.append(
                Gstr2bEntry(inv.invoice_no, period, taxable - drift,
                            apply_bps(taxable - drift, GST_BPS), SUPPLIER_GSTIN)
            )
            notes.append(f"itc_amount_mismatch {period}")
            continue
        gstr2b.append(Gstr2bEntry(inv.invoice_no, period, taxable, gst, SUPPLIER_GSTIN))

    return invoices, gstr2b, notes
