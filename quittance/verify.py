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
from .schema import Dataset, Exception_, ExceptionCode, Match

#: Confidence below which a proposal is not even arithmetic-checked. A model
#: that is unsure is telling you to send the row to a human.
MIN_CONFIDENCE = 0.55


def verify(
    ds: Dataset, proposals: list[Match], claimed: set[str]
) -> tuple[list[Match], list[Match], list[Exception_]]:
    """Split proposals into accepted, rejected, and the exceptions they became.

    Returns ``(accepted, rejected, exceptions)``.
    """
    nets = settlement_nets(ds.recon_rows)
    amounts = {line.line_id: line.amount for line in ds.bank_lines}

    accepted: list[Match] = []
    rejected: list[Match] = []
    exceptions: list[Exception_] = []
    taken = set(claimed)

    for p in proposals:
        reason = _reject_reason(p, nets, amounts, taken)
        if reason is None:
            accepted.append(replace(p, verified=True))
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
    p: Match, nets: dict[str, int], amounts: dict[str, int], taken: set[str]
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

    return None
