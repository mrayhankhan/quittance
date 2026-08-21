"""The third leg: settlement rows against the merchant's own order ledger.

Bank-to-settlement matching tells you the money arrived. It does not tell you
*which orders it was for*, and that is the question finance actually has to
answer to close a month. Revenue posts per order; the bank posts per batch.

So this layer explodes each settlement back down to order level and asks three
questions of every payment row:

* is there an order for it at all?
* does the order's value agree with what was captured, to the paisa?
* has any order been paid for twice?

Like Layers 0 and 1, this is entirely deterministic. Matching an ``order_id`` to
an ``order_id`` is a dictionary lookup, and no amount of model will make it more
correct.
"""

from __future__ import annotations

from dataclasses import dataclass

from .money import inr
from .schema import Dataset, EntityType, Exception_, ExceptionCode


@dataclass(frozen=True, slots=True)
class OrderRecon:
    """Coverage of the settlement file against the order ledger."""

    payment_rows: int
    tied: int
    exceptions: tuple[Exception_, ...]

    @property
    def coverage(self) -> float:
        return self.tied / self.payment_rows if self.payment_rows else 0.0


def reconcile_orders(ds: Dataset) -> OrderRecon:
    """Tie every settled payment row to an order in the merchant's ledger."""
    by_id = {o.order_id: o for o in ds.orders}
    seen: dict[str, str] = {}

    tied = 0
    exceptions: list[Exception_] = []

    for row in ds.recon_rows:
        if row.type is not EntityType.PAYMENT:
            continue  # refunds and adjustments tie to their parent payment
        order = by_id.get(row.order_id or "")
        if order is None:
            exceptions.append(
                Exception_(
                    ref=row.entity_id,
                    code=ExceptionCode.MISSING_ORDER,
                    detail=(
                        f"settlement row references order {row.order_id!r}, which is "
                        f"absent from the merchant ledger"
                    ),
                    amount=row.amount,
                )
            )
            continue

        if order.amount != row.amount:
            delta = row.amount - order.amount
            exceptions.append(
                Exception_(
                    ref=row.entity_id,
                    code=ExceptionCode.AMOUNT_MISMATCH,
                    detail=(
                        f"order {order.order_id} is {inr(order.amount)} but "
                        f"{inr(row.amount)} was captured "
                        f"({'over' if delta > 0 else 'under'} by {inr(abs(delta))})"
                    ),
                    amount=abs(delta),
                )
            )
            continue

        prior = seen.get(order.order_id)
        if prior is not None:
            exceptions.append(
                Exception_(
                    ref=row.entity_id,
                    code=ExceptionCode.DUPLICATE_PAYMENT,
                    detail=(
                        f"order {order.order_id} is already settled by {prior}; "
                        f"the customer may have been charged twice"
                    ),
                    amount=row.amount,
                )
            )
            continue

        seen[order.order_id] = row.entity_id
        tied += 1

    total = sum(1 for r in ds.recon_rows if r.type is EntityType.PAYMENT)
    return OrderRecon(payment_rows=total, tied=tied, exceptions=tuple(exceptions))
