"""Orchestration and measurement.

The report this produces is the deliverable. It is deliberately unflattering:
it counts what was left unmatched, what the verifier threw out, and -- the
number that matters most -- how many matches were *wrong*.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import llm, matching, orders, tax, verify
from .schema import Dataset, Exception_, ExceptionCode, Layer, Match


@dataclass(slots=True)
class Report:
    dataset_seed: int
    bank_lines: int
    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    rejected: list[Match] = field(default_factory=list)
    itc: list[tax.ItcLine] = field(default_factory=list)
    orders: orders.OrderRecon | None = None
    engine: str = ""
    elapsed_ms: float = 0.0
    false_matches: list[tuple[str, str, str]] = field(default_factory=list)
    """``(bank_line_id, claimed_settlements, true_settlements)``."""

    # -- headline numbers --------------------------------------------------

    @property
    def by_layer(self) -> dict[str, int]:
        counts = {l.value: 0 for l in Layer}
        for m in self.matches:
            counts[m.layer.value] += 1
        return counts

    @property
    def match_rate(self) -> float:
        return len(self.matches) / self.bank_lines if self.bank_lines else 0.0

    @property
    def matched_amount(self) -> int:
        return sum(m.amount for m in self.matches)

    @property
    def unexplained_amount(self) -> int:
        return sum(e.amount for e in self.exceptions)

    @property
    def false_match_rate(self) -> float:
        return len(self.false_matches) / len(self.matches) if self.matches else 0.0

    @property
    def itc_at_risk(self) -> int:
        return sum(line.at_risk for line in self.itc)


def run(ds: Dataset, force_offline: bool = False) -> Report:
    """Execute all four layers plus the tax reconciliation."""
    started = time.perf_counter()
    report = Report(dataset_seed=ds.seed, bank_lines=len(ds.bank_lines))

    # -- Layer 0 ------------------------------------------------------------
    l0, unresolved = matching.layer0_exact(ds)
    report.matches.extend(l0)
    claimed = {sid for m in l0 for sid in m.settlement_id.split("+")}

    # -- Layer 1 ------------------------------------------------------------
    l1, unresolved = matching.layer1_solver(ds, unresolved, claimed)
    report.matches.extend(l1)
    claimed |= {sid for m in l1 for sid in m.settlement_id.split("+")}

    # -- Layer 2 (proposals only) -------------------------------------------
    utr_index = {
        r.settlement_utr: r.settlement_id
        for r in ds.recon_rows
        if r.settlement_utr and r.settlement_id
    }
    client = llm.build_client(utr_index, force_offline=force_offline)
    report.engine = client.name

    nets = matching.settlement_nets(ds.recon_rows)
    dates = {}
    for r in ds.recon_rows:
        if r.settlement_id and r.settlement_id not in dates and r.settled_at:
            dates[r.settlement_id] = r.settled_at
    candidates = [
        (sid, net, str(dates.get(sid, "")))
        for sid, net in nets.items()
        if sid not in claimed
    ]
    proposals = llm.layer2_propose(client, unresolved, candidates)

    # -- Layer 3 (the gate) --------------------------------------------------
    accepted, rejected, verif_exceptions = verify.verify(ds, proposals, claimed)
    report.matches.extend(accepted)
    report.rejected.extend(rejected)
    report.exceptions.extend(verif_exceptions)
    matched_lines = {m.bank_line_id for m in report.matches}
    # A line whose proposal the verifier rejected already has its exception.
    # Raising a second one for the same rupees would double-count the money.
    already_flagged = {e.ref for e in report.exceptions}

    for line in unresolved:
        if line.line_id not in matched_lines and line.line_id not in already_flagged:
            report.exceptions.append(
                Exception_(
                    ref=line.line_id,
                    code=_classify(line),
                    detail=f"no settlement reconciles to {line.amount} paisa on {line.value_date}",
                    amount=line.amount,
                )
            )

    # -- order leg: settlement rows down to the merchant ledger ---------------
    report.orders = orders.reconcile_orders(ds)
    report.exceptions.extend(report.orders.exceptions)

    # -- tax layer -----------------------------------------------------------
    report.itc = tax.reconcile_itc(ds)
    for line in report.itc:
        code = line.exception_code
        if code and code is not ExceptionCode.ROUNDING_DRIFT:
            report.exceptions.append(
                Exception_(ref=line.invoice_no, code=code, detail=line.detail, amount=line.at_risk)
            )

    # -- scoring against the answer key --------------------------------------
    for m in report.matches:
        claimed_set = frozenset(m.settlement_id.split("+"))
        true_set = frozenset(ds.truth.get(m.bank_line_id, "").split("+"))
        if claimed_set != true_set:
            report.false_matches.append(
                (m.bank_line_id, "+".join(sorted(claimed_set)), "+".join(sorted(true_set)))
            )

    report.elapsed_ms = (time.perf_counter() - started) * 1000
    return report


def _classify(line) -> ExceptionCode:
    n = line.narration.upper()
    if "CONSOLIDATED" in n:
        return ExceptionCode.DUPLICATE_UTR
    if line.utr is None:
        return ExceptionCode.NARRATION_TRUNCATED
    return ExceptionCode.UNEXPLAINED
