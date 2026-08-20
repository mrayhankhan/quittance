"""Tests.

The suite is weighted towards the two properties that actually matter:
money arithmetic is exact, and the pipeline never produces a wrong match.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import date

import pytest

from tieout.generate import generate
from tieout.matching import layer0_exact, settlement_nets, subset_sum
from tieout.money import apply_bps, fmt, inr, rupees, within
from tieout.pipeline import run
from tieout.schema import BankLine, Layer
from tieout.tax import ItcStatus, reconcile_itc
from tieout.verify import verify

# ---------------------------------------------------------------- money ----


@pytest.mark.parametrize(
    "text,expected",
    [("942.31", 94231), ("0.01", 1), ("1,00,000.00", 10_000_000), ("-12.50", -1250), ("₹5", 500)],
)
def test_rupees_parses(text, expected):
    assert rupees(text) == expected


def test_rupees_rejects_sub_paisa():
    # Silently truncating here is how a settlement file quietly loses money.
    with pytest.raises(ValueError):
        rupees("10.001")


def test_no_float_drift():
    """The canonical float failure, which integer paisa simply does not have."""
    assert rupees("0.10") + rupees("0.20") == rupees("0.30")


def test_apply_bps_rounds_half_up():
    # 2% of 1050 paisa is 21.0 exactly; 2% of 1025 is 20.5 -> 21, not 20.
    assert apply_bps(1050, 200) == 21
    assert apply_bps(1025, 200) == 21
    assert apply_bps(-1025, 200) == -21


def test_gst_on_mdr_chain():
    amount = rupees("1000.00")
    fee = apply_bps(amount, 200)
    tax = apply_bps(fee, 1800)
    assert fee == rupees("20.00")
    assert tax == rupees("3.60")
    assert amount - fee - tax == rupees("976.40")


def test_formatting():
    assert fmt(94231) == "942.31"


@pytest.mark.parametrize(
    "paisa,expected",
    [
        (9_423_117, "₹94,231.17"),
        (100, "₹1.00"),
        (5_000_000, "₹50,000.00"),
        (12_345_678_901, "₹12,34,56,789.01"),  # lakh/crore grouping, not thousands
        (-250, "-₹2.50"),
    ],
)
def test_indian_digit_grouping(paisa, expected):
    assert inr(paisa) == expected


def test_within_defaults_to_exact():
    assert within(100, 100)
    assert not within(100, 101)
    assert within(100, 101, tolerance_paisa=1)


# ------------------------------------------------------------ subset sum ----


def test_subset_sum_finds_unique_solution():
    items = [("a", 600), ("b", 400), ("c", 150)]
    assert subset_sum(1000, items) == ["a", "b"]


def test_subset_sum_refuses_ambiguity():
    """Two valid decompositions must yield no match, not an arbitrary one.

    600+400 and 600+400 via the second 400 are distinct subsets summing to the
    same target. Picking either is a coin flip.
    """
    items = [("a", 600), ("b", 400), ("c", 400)]
    assert subset_sum(1000, items) is None


def test_subset_sum_no_solution():
    assert subset_sum(999, [("a", 600), ("b", 400)]) is None
    assert subset_sum(0, [("a", 1)]) is None


# --------------------------------------------------------------- layer 0 ----


def test_layer0_never_claims_a_settlement_twice():
    """Two bank lines sharing a corrupted UTR must not both reconcile to it.

    The generator injects exactly this: a bank feed that copies one line's UTR
    onto another. The correct outcome is one match and one unmatched row, never
    the same settlement paid out twice.
    """
    for seed in (7, 42, 20260905):
        ds = generate(seed=seed, n_payments=150)
        matches, _ = layer0_exact(ds)
        claimed = [m.settlement_id for m in matches]
        assert len(claimed) == len(set(claimed)), f"settlement double-claimed at seed {seed}"


def test_layer0_refuses_when_amount_disagrees():
    """A good identifier never overrides bad arithmetic."""
    ds = generate(seed=11, n_payments=150)
    nets = settlement_nets(ds.recon_rows)
    matches, _ = layer0_exact(ds)
    for m in matches:
        assert nets[m.settlement_id] == m.amount


# --------------------------------------------------------------- layer 3 ----


def test_verifier_rejects_arithmetic_mismatch():
    from tieout.schema import Match

    ds = generate(seed=3, n_payments=100)
    line = ds.bank_lines[0]
    sid = next(iter(settlement_nets(ds.recon_rows)))
    bogus = Match(
        bank_line_id=line.line_id,
        settlement_id=sid,
        amount=line.amount + 1,  # off by one paisa
        layer=Layer.LLM,
        rule="test",
        confidence=0.99,
    )
    accepted, rejected, exceptions = verify(ds, [bogus], set())
    if settlement_nets(ds.recon_rows)[sid] != line.amount:
        assert not accepted
        assert rejected
        assert "arithmetic mismatch" in exceptions[0].detail


def test_verifier_rejects_low_confidence():
    from tieout.schema import Match

    ds = generate(seed=3, n_payments=100)
    line = ds.bank_lines[0]
    timid = Match(line.line_id, "setl_x", line.amount, Layer.LLM, "test", 0.10)
    accepted, rejected, _ = verify(ds, [timid], set())
    assert not accepted and rejected


def test_verifier_blocks_double_spend():
    """One settlement cannot reconcile two different bank lines."""
    from tieout.schema import Match

    ds = generate(seed=5, n_payments=100)
    nets = settlement_nets(ds.recon_rows)
    sid, net = next(iter(nets.items()))
    line = BankLine("bank_fake", date(2026, 7, 3), "x", net, None)
    ds = dc_replace(ds, bank_lines=ds.bank_lines + (line,))
    m = Match("bank_fake", sid, net, Layer.LLM, "test", 0.9)
    accepted, rejected, _ = verify(ds, [m], claimed={sid})
    assert not accepted and rejected


# ------------------------------------------------------------------ tax ----


def test_itc_flags_missing_2b_entry():
    ds = generate(seed=20260905, n_payments=300)
    lines = reconcile_itc(ds)
    assert lines
    for line in lines:
        if line.status is ItcStatus.NOT_IN_2B:
            assert line.at_risk == line.invoiced_gst
            assert line.gstr2b_gst is None


def test_itc_at_risk_is_zero_when_claimable():
    ds = generate(seed=4, n_payments=200)
    for line in reconcile_itc(ds):
        if line.status is ItcStatus.CLAIMABLE:
            assert line.at_risk == 0


# ------------------------------------------------------------- pipeline ----


@pytest.mark.parametrize("seed", [1, 42, 1234, 20260905, 99991])
def test_never_produces_a_false_match(seed):
    """The load-bearing invariant of the whole project.

    Across every seed, the pipeline may leave rows unmatched -- that is fine
    and expected -- but it must never claim a match that the answer key
    disagrees with.
    """
    report = run(generate(seed=seed, n_payments=250), force_offline=True)
    assert report.false_matches == [], report.false_matches


def test_deterministic_across_runs():
    a = run(generate(seed=77, n_payments=200), force_offline=True)
    b = run(generate(seed=77, n_payments=200), force_offline=True)
    assert a.match_rate == b.match_rate
    assert a.matched_amount == b.matched_amount
    assert len(a.exceptions) == len(b.exceptions)


def test_most_work_happens_without_a_model():
    """If this ever fails, the architecture's premise is wrong."""
    report = run(generate(seed=20260905, n_payments=500), force_offline=True)
    deterministic = report.by_layer[Layer.EXACT.value] + report.by_layer[Layer.SOLVER.value]
    assert deterministic / report.bank_lines >= 0.90


def test_money_conservation():
    """Matched plus unexplained accounts for every bank line."""
    report = run(generate(seed=20260905, n_payments=400), force_offline=True)
    assert len(report.matches) + len({e.ref for e in report.exceptions
                                      if e.ref.startswith("bank_")}) == report.bank_lines
