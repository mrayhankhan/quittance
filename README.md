# Quittance

_**quittance** (n.) — release or discharge from a debt or obligation, once it
is proven paid; the document that certifies it. 14th century, via Old French
*quitance*. This is the receipt the settlement never gives you._

Three-way settlement reconciliation for Indian merchants, built so that a
language model can propose a match but never close the books.

**Razorpay AI Buildathon 2026 · Track 4 — AI Finance Controller**

```bash
make install
make demo        # runs offline, no API key needed
make test
```

---

## The problem

A merchant makes 500 sales. Razorpay does not credit 500 amounts. It credits
**one** bank line at T+2 — say ₹94,231.17 — being those payments minus MDR,
minus 18% GST on that MDR, minus refunds that landed in this window rather than
the one they belong to, minus chargebacks from three weeks ago.

Finance then ties one bank credit back to hundreds of orders. In a spreadsheet.
Every day.

And underneath that sits the part the settlement report cannot answer at all:
the GST embedded in those fees is **input tax credit**, but only if the fee is
booked, Razorpay's tax invoice exists, that invoice appears in GSTR-2B, and the
GSTR-3B claim matches. Miss it and the credit is not deferred — it is gone. On
₹10 crore of GMV that is roughly ₹3.6 lakh a year evaporating quietly.

## What it does

Reconciles three sources — **bank statement ↔ Razorpay settlement report ↔
merchant order ledger** — then reconciles the tax line on top, and reports what
it could _not_ explain.

## Results

`make demo` — 524 recon rows, 41 bank credits, seed `20260905`:

```
Match rate by layer
  L0  exact identifier      no AI    32   78.0%
  L1  constrained solver    no AI     8   19.5%
  L2  model, verified by L3    AI     0    0.0%
                                     40   97.6%  total

Correctness
  false matches                          0   [PASS]
  proposals rejected by verifier         1
  exceptions raised                      2
```

Every number above is reproducible: the generator ships with its seed. Clone
the repo, run `make demo`, get this.

**97.6% of bank credits reconciled. Zero false matches. 100% of the matches
were made without a language model.**

That last figure is not a disappointment. It is the finding.

## Architecture

| Layer          | Does                                                                                       | AI? |
| -------------- | ------------------------------------------------------------------------------------------ | --- |
| **0** Exact    | Joins on `settlement_utr`, refuses ambiguous UTRs, refuses when the amount disagrees       | no  |
| **1** Solver   | Amount+date uniqueness, then subset-sum for credits the bank clubbed together              | no  |
| **2** Model    | Reads mangled narration, nominates a settlement, cites evidence. **Never does arithmetic** | yes |
| **3** Verifier | Recomputes every proposal to the paisa. Rejects anything that does not close               | no  |
| **Tax**        | Booked fees → Razorpay tax invoice → GSTR-2B → claimable / at-risk verdict                 | no  |

### Why the model is confined to Layer 2

By the time a row reaches Layer 2, exact matching and arithmetic have both
declined it. What remains is a _reading_ problem — `NEFT/RZRPY/CONSOLIDATED
CREDIT`, `ADJ-CHGBK-8821` — which is genuinely what a model is good at.

Adding 312 numbers is not. So it doesn't.

### Why Layer 3 exists

In the demo run, Layer 2 nominated a settlement for `bank_0027` and it was
**right about which settlement it was** — the narration prefix really did match
that UTR. Layer 3 rejected it anyway, because the bank credit was ₹2,500 short
of what those rows sum to.

There was a rolling reserve hold on that batch. The model could not have known;
nothing in the narration says so. The arithmetic did know.

That is the whole design in one row: the model may propose, the verifier
decides, and a human gets an exception that names the exact shortfall.

## Design decisions worth arguing about

**All money is `int` paisa. No floats anywhere.** Reconciliation is an equality
test and floats do not have exact equality. `0.1 + 0.2 != 0.3` is a curiosity in
most programs and a wrong journal entry here.

**Zero tolerance by default.** `within()` takes an explicit `tolerance_paisa`
that defaults to `0`. Every caller that loosens it has to say so at the call
site, which stops sloppy matching creeping in.

**Subset-sum refuses ambiguity.** A clubbed credit of ₹1,000 against
settlements of ₹600, ₹400 and ₹400 has two exact decompositions. Returning the
first one the search reaches is a coin flip dressed as a reconciliation, so the
solver keeps searching for a second solution and returns nothing if it finds
one. It also carries a hard node budget — subset-sum is NP-hard, and a search
that never returns is worth less than an honest exception.

**A good identifier never overrides bad arithmetic.** Layer 0 will decline a
perfect UTR match if the money doesn't agree.

**Optimised for zero false matches, not for match rate.** An unmatched row
costs an analyst four minutes. A wrong match corrupts a ledger and surfaces in
an audit six months later. A long, honest exception list is the correct output.

## Tests

```
34 passed
```

The load-bearing one runs the full pipeline across five seeds and asserts the
answer key never disagrees with a claimed match:

```python
@pytest.mark.parametrize("seed", [1, 42, 1234, 20260905, 99991])
def test_never_produces_a_false_match(seed):
    report = run(generate(seed=seed, n_payments=250), force_offline=True)
    assert report.false_matches == []
```

`test_most_work_happens_without_a_model` asserts the deterministic layers carry
≥90%. If that ever fails, the premise of this architecture is wrong and I'd
want to know.

## Synthetic data

No merchant is going to hand a student their settlement file, so the generator
builds one — and deliberately breaks it in the ways real files break:

`late_refund` · `chargeback_reversal` · `reserve_hold` · `narration_truncated`
· `duplicate_utr` · `clubbed_credit` · `itc_not_in_2b` · `itc_amount_mismatch`
· GST rounding drift between per-row and monthly-aggregate rounding

Row shape follows Razorpay's `GET /v1/settlements/recon/combined` — `entity_id`,
`type`, `debit`, `credit`, `fee`, `tax`, `settlement_id`, `settlement_utr`,
`payment_id`, `order_id`, `method`, `settled_at`.

The generator also emits an **answer key** (`Dataset.truth`) that the pipeline
never reads. It exists so the metrics harness can tell a correct match from a
confident wrong one — which is the number this whole project is built around.

## Layer 2 without a key

`make demo` runs `HeuristicClient`, a deterministic offline stand-in, so a
clean clone reproduces the published numbers with no network and no spend. Set
`ANTHROPIC_API_KEY` and run `make demo-llm` for the real client. The report
prints which engine ran, because a benchmark that silently swaps engines is not
a benchmark.

## Layout

```
quittance/
  money.py      paisa arithmetic, half-up bps rounding, Indian digit grouping
  schema.py     domain model, exception taxonomy
  generate.py   seeded generator with defect injection
  matching.py   Layer 0 + Layer 1 (subset-sum lives here)
  llm.py        Layer 2 — offline and Anthropic clients
  verify.py     Layer 3 — the gate
  tax.py        ITC reconciliation against GSTR-2B
  pipeline.py   orchestration and metrics
  cli.py        terminal report
```

## Not built

Scope was 16 days; these are deliberate omissions, not oversights.

- **Cross-channel reconciliation.** A real D2C seller runs Razorpay _and_
  Amazon _and_ Flipkart, each with its own deduction structure and its own
  194-O/TCS treatment. That is the version merchants actually need — and it is
  one Razorpay is structurally barred from building, since it means ingesting
  competitors' settlement data.
- **Live API ingestion.** The schema matches the real endpoint; the transport
  is not wired.
- **Journal-entry export** to Tally/Zoho.

## Known limits

- The GSTR-2B side is synthetic. Real 2B has invoice-level granularity and
  amendment history this model flattens.
- The offline Layer 2 client only recovers truncated UTR prefixes. A real model
  handles narration variety it cannot.
- Settlement batching is modelled as UPI/cards split daily. Real cycles vary by
  merchant contract.
