"""Layer 3: the gate.

Every proposal from Layer 2 passes through here before it can become a match.
The verifier does not ask the model to justify itself and it does not weigh
confidence -- it recomputes the arithmetic and compares to the paisa. A
proposal either reconciles or it does not.

This is the layer that makes the rest of the design safe. Because it exists,
Layer 2 is allowed to be creative: the worst a wrong hypothesis can do is get
rejected into the exception queue, where a human was going to look anyway.
"""

from __future__ import annotations

from dataclasses import replace

from .matching import settlement_nets
from .schema import Dataset, Exception_, ExceptionCode, Layer, Match

#: Confidence below which a proposal is not even arithmetic-checked. A model
#: that is unsure is telling you to send the row to a human.
MIN_CONFIDENCE = 0.55

#: Days a model-proposed settlement may sit from the bank credit's value date.
#:
#: Defence in depth, not the primary control. A settlement dated six weeks from
#: the credit is implausible whatever the arithmetic says, so this rejects an
#: obviously bad nomination early and cheaply.
#:
#: It is explicitly *not* what stops prompt injection. The adversarial eval
#: showed an attacker simply picks a colliding settlement inside the window.
#: What stops injection is that Layer 2 cannot post at all -- see
#: ``requires_review`` below and DEBUG.md incident 9.
MAX_DATE_DRIFT_DAYS = 4


def verify(
    ds: Dataset, proposals: list[Match], claimed: set[str]
) -> tuple[list[Match], list[Match], list[Exception_]]:
    """Split proposals into accepted, rejected, and the exceptions they became.

    Returns ``(accepted, rejected, exceptions)``.
    """
    nets = settlement_nets(ds.recon_rows)
    amounts = {line.line_id: line.amount for line in ds.bank_lines}
    value_dates = {line.line_id: line.value_date for line in ds.bank_lines}
    settled: dict[str, object] = {}
    for row in ds.recon_rows:
        if row.settlement_id and row.settled_at and row.settlement_id not in settled:
            settled[row.settlement_id] = row.settled_at

    accepted: list[Match] = []
    rejected: list[Match] = []
    exceptions: list[Exception_] = []
    taken = set(claimed)

    for p in proposals:
        reason = _reject_reason(p, nets, amounts, taken, value_dates, settled)
        if reason is None:
            # A model proposal that reconciles is still only a *proposal*. Bank
            # narration is attacker-controllable, and an adversarial eval showed
            # a merchant who can steer order amounts can engineer a settlement
            # whose net equals the target credit, then inject a narration
            # pointing the model at it. Arithmetic closes and the ledger is
            # corrupted. No amount of checking the sum fixes that, because the
            # sum is what the attacker forged.
            #
            # So Layer 2 cannot post. It queues for a human. This makes the
            # project's central claim structural rather than conditional:
            # the model may propose, and it may never close the books.
            accepted.append(replace(p, verified=True,
                                    requires_review=p.layer is Layer.LLM))
            for sid in p.settlement_id.split("+"):
                taken.add(sid)
        else:
            rejected.append(p)
            exceptions.append(
                Exception_(
                    ref=p.bank_line_id,
                    code=ExceptionCode.UNEXPLAINED,
                    detail=f"proposal rejected by verifier: {reason}",
                    amount=amounts.get(p.bank_line_id, 0),
                    layer=p.layer,
                )
            )

    return accepted, rejected, exceptions


def _reject_reason(
    p: Match,
    nets: dict[str, int],
    amounts: dict[str, int],
    taken: set[str],
    value_dates: dict | None = None,
    settled: dict | None = None,
) -> str | None:
    if p.confidence < MIN_CONFIDENCE:
        return f"confidence {p.confidence:.2f} below {MIN_CONFIDENCE}"

    sids = p.settlement_id.split("+")
    for sid in sids:
        if sid not in nets:
            return f"unknown settlement {sid}"
        if sid in taken:
            return f"settlement {sid} already reconciled"

    expected = sum(nets[sid] for sid in sids)
    actual = amounts.get(p.bank_line_id)
    if actual is None:
        return f"unknown bank line {p.bank_line_id}"
    if expected != actual:
        return f"arithmetic mismatch: settlements sum to {expected}, bank line is {actual}"

    # Second, independent corroboration -- required only of the model layer,
    # because only the model layer reads attacker-controllable text. Layers 0
    # and 1 already carry an identifier or a solved constraint as evidence.
    if p.layer is Layer.LLM and value_dates and settled:
        credit_date = value_dates.get(p.bank_line_id)
        for sid in sids:
            sdate = settled.get(sid)
            if credit_date is None or sdate is None:
                return f"cannot corroborate {sid} on date"
            drift = abs((sdate - credit_date).days)
            if drift > MAX_DATE_DRIFT_DAYS:
                return (
                    f"temporally implausible: {sid} settled {drift} days from the "
                    f"credit (limit {MAX_DATE_DRIFT_DAYS})"
                )

    return None
