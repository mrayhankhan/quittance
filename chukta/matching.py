"""Layers 0 and 1: everything that reconciles without a model.

Between them these two layers resolve the large majority of a real settlement
file. That is the whole argument of this project -- not that language models
are useless here, but that pointing one at a problem an index lookup already
solves is a failure of engineering judgment, and an expensive one at paisa
scale.
"""

from __future__ import annotations

from collections import defaultdict

from .schema import BankLine, Dataset, Layer, Match, ReconRow

# A bank line and a settlement must agree to the paisa. There is no tolerance
# here on purpose: the fee and tax arithmetic is exact, so any gap is a real
# discrepancy that deserves a human, not a fudge factor.
EXACT = 0


def settlement_nets(rows: tuple[ReconRow, ...]) -> dict[str, int]:
    """Net bank credit each settlement batch should produce."""
    nets: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.settlement_id:
            nets[row.settlement_id] += row.net
    return dict(nets)


# --------------------------------------------------------------------------
# Layer 0 -- exact identifier match
# --------------------------------------------------------------------------


def layer0_exact(ds: Dataset) -> tuple[list[Match], list[BankLine]]:
    """Join bank lines to settlements on UTR.

    Refuses to match when a UTR maps to more than one settlement. An ambiguous
    identifier is not a weak match to be broken by a tiebreak -- it is an
    unmatched row, and saying so is the correct output.
    """
    nets = settlement_nets(ds.recon_rows)

    by_utr: dict[str, list[str]] = defaultdict(list)
    for row in ds.recon_rows:
        if row.settlement_utr and row.settlement_id:
            if row.settlement_id not in by_utr[row.settlement_utr]:
                by_utr[row.settlement_utr].append(row.settlement_id)

    matches: list[Match] = []
    unresolved: list[BankLine] = []
    # A settlement pays out once. Two bank lines carrying the same UTR -- which
    # happens when a bank feed corrupts a narration -- must not both claim it,
    # or the same money is reconciled twice and the books balance on a lie.
    claimed: set[str] = set()

    for line in ds.bank_lines:
        if not line.utr:
            unresolved.append(line)
            continue

        candidates = by_utr.get(line.utr, [])
        if len(candidates) != 1:
            unresolved.append(line)
            continue

        sid = candidates[0]
        if sid in claimed:
            unresolved.append(line)
            continue

        if nets.get(sid) != line.amount:
            # The UTR agrees but the money does not. Never let an identifier
            # override arithmetic -- hand it downstream.
            unresolved.append(line)
            continue

        matches.append(
            Match(
                bank_line_id=line.line_id,
                settlement_id=sid,
                amount=line.amount,
                layer=Layer.EXACT,
                rule="utr_unique_and_amount_exact",
                confidence=1.0,
                evidence=(f"utr={line.utr}", f"net={nets[sid]}"),
            )
        )
        claimed.add(sid)

    return matches, unresolved


# --------------------------------------------------------------------------
# Layer 1 -- constrained solver
# --------------------------------------------------------------------------


def layer1_solver(
    ds: Dataset,
    unresolved: list[BankLine],
    claimed: set[str],
) -> tuple[list[Match], list[BankLine]]:
    """Resolve lines whose identifier is missing or ambiguous, by arithmetic.

    Three strategies, tried in order of how much they assume:

    1. amount and value date both agree, and exactly one settlement fits
    2. amount alone agrees, and exactly one settlement fits
    3. the credit is a *club* of several settlements the bank batched into one
       NEFT -- solved as a subset-sum over unclaimed settlement nets

    Uniqueness is required throughout. Two candidates means no match.
    """
    nets = {k: v for k, v in settlement_nets(ds.recon_rows).items() if k not in claimed}

    dates: dict[str, object] = {}
    for row in ds.recon_rows:
        if row.settlement_id and row.settlement_id not in dates and row.settled_at:
            dates[row.settlement_id] = row.settled_at

    matches: list[Match] = []
    still: list[BankLine] = []

    for line in unresolved:
        # -- strategy 1: amount + date -------------------------------------
        hits = [
            sid
            for sid, net in nets.items()
            if net == line.amount and dates.get(sid) == line.value_date
        ]
        if len(hits) == 1:
            sid = hits[0]
            matches.append(_match(line, sid, Layer.SOLVER, "amount_and_date_unique", 0.98,
                                  (f"net={nets[sid]}", f"date={line.value_date}")))
            del nets[sid]
            continue

        # -- strategy 2: amount alone --------------------------------------
        hits = [sid for sid, net in nets.items() if net == line.amount]
        if len(hits) == 1:
            sid = hits[0]
            matches.append(_match(line, sid, Layer.SOLVER, "amount_unique", 0.92,
                                  (f"net={nets[sid]}",)))
            del nets[sid]
            continue

        # -- strategy 3: subset-sum over clubbed settlements ---------------
        window = [
            (sid, net)
            for sid, net in nets.items()
            if _near(dates.get(sid), line.value_date)
        ]
        subset = subset_sum(line.amount, window)
        if subset and len(subset) > 1:
            matches.append(
                _match(
                    line,
                    "+".join(subset),
                    Layer.SOLVER,
                    "subset_sum_clubbed_credit",
                    0.90,
                    tuple(f"{sid}={nets[sid]}" for sid in subset),
                )
            )
            for sid in subset:
                del nets[sid]
            continue

        still.append(line)

    return matches, still


def _match(line: BankLine, sid: str, layer: Layer, rule: str,
           conf: float, evidence: tuple[str, ...]) -> Match:
    return Match(
        bank_line_id=line.line_id,
        settlement_id=sid,
        amount=line.amount,
        layer=layer,
        rule=rule,
        confidence=conf,
        evidence=evidence,
    )


def _near(a: object, b: object, days: int = 3) -> bool:
    if a is None or b is None:
        return False
    try:
        return abs((a - b).days) <= days  # type: ignore[operator]
    except TypeError:
        return False


# --------------------------------------------------------------------------
# Subset-sum
# --------------------------------------------------------------------------

#: Search-node ceiling. Subset-sum is NP-hard and a settlement window can hold
#: dozens of candidates, so an unbounded search will happily run until the heat
#: death of the universe on a bad batch. Exceeding the budget returns ``None``,
#: which routes the line to the exception queue -- a slow correct answer is
#: worth more than a fast wrong one, but an answer that never arrives is worth
#: nothing at all.
MAX_NODES = 250_000


def subset_sum(target: int, items: list[tuple[str, int]]) -> list[str] | None:
    """Find the *unique* subset of ``items`` summing exactly to ``target``.

    Uniqueness is the whole point, and it is the part most implementations get
    wrong. A clubbed bank credit of ₹1,000 against settlements of ₹600, ₹400,
    ₹400 and ₹250 has two exact decompositions. Returning the first one the
    search happens to reach is a coin flip dressed up as a reconciliation --
    and a wrong match here corrupts a ledger far more expensively than an
    unmatched row costs an analyst.

    So the search does not stop at the first hit. It keeps going until it finds
    a second, and if one exists it returns ``None`` and lets the row become an
    exception.

    Depth-first with three prunes: descending sort so large values fail fast, a
    suffix-sum bound so hopeless branches die early, and a hard node budget so
    a pathological batch degrades to an exception instead of hanging.
    """
    if target <= 0 or not items:
        return None

    pool = sorted(((v, k) for k, v in items if v > 0), reverse=True)
    values = [v for v, _ in pool]
    keys = [k for _, k in pool]

    n = len(values)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + values[i]

    budget = MAX_NODES
    chosen: list[int] = []
    solutions: list[list[int]] = []

    def walk(i: int, remaining: int) -> None:
        nonlocal budget
        if len(solutions) > 1 or budget <= 0:
            return
        if remaining == 0:
            solutions.append(list(chosen))
            return
        budget -= 1
        if i >= n or remaining < 0 or suffix[i] < remaining:
            return
        if values[i] <= remaining:
            chosen.append(i)
            walk(i + 1, remaining - values[i])
            chosen.pop()
        walk(i + 1, remaining)

    walk(0, target)

    if budget <= 0:
        return None  # search exhausted; refuse rather than guess
    if len(solutions) != 1:
        return None  # zero solutions, or ambiguous
    return sorted(keys[i] for i in solutions[0])
