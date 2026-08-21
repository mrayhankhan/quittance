# Build log

Running notes on what broke. Kept as I went, not reconstructed afterwards.

---

## 1. The test suite caught a double-spend in Layer 0

**Symptom.** `test_layer0_never_claims_a_settlement_twice` failed on seed 7.
Two bank lines were both reconciling to the same settlement.

**What I assumed first.** That the duplicate-UTR defect injector was buggy and
producing a UTR collision the real world wouldn't.

**Why that was wrong.** The injector was fine — it does exactly what a bank
feed does when it copies a narration onto the wrong line. The bug was mine.
Layer 0 indexed `settlement_utr -> settlement_id` and asserted the *settlement*
side was unambiguous. It never checked the *bank* side. Two lines carrying the
same UTR each looked up the same settlement, and each got a match, so the same
money reconciled twice and the books balanced on a lie.

**Fix.** Layer 0 now tracks claimed settlements and declines a second claim.

**Structural change.** The claim check is not local to Layer 0. Layer 3 enforces
the same invariant independently for anything the model proposes, so the
property holds even if a future layer forgets it. One invariant, two enforcement
points, because this is the class of bug that silently balances.

---

## 2. `slots=True` broke the verifier's accept path

**Symptom.** `AttributeError: 'Dataset' object has no attribute '__dict__'`.

**Root cause.** I'd written `Match(**{**p.__dict__, "verified": True})` to copy
a match with one field changed. Every dataclass here uses `slots=True`, so
there is no `__dict__`.

**Why it hid.** It only fires when the verifier *accepts* a proposal. Early on
the offline Layer 2 client never produced an acceptable one, so the accept path
had literally never executed. The tests passed while a crash sat on the main
path.

**Fix.** `dataclasses.replace(p, verified=True)`.

**Lesson.** Green tests meant the branch was untested, not that it worked. I now
assert on rejected *and* accepted counts rather than just "not accepted".

---

## 3. Subset-sum was returning a coin flip

**Symptom.** Not a crash — a design review of my own code. The solver returned
the first exact subset the DFS reached.

**Why that's wrong.** A clubbed credit of ₹1,000 against settlements of ₹600,
₹400 and ₹400 has two valid decompositions. Returning either is a guess, and a
guess that reconciles is worse than no match: it looks correct, posts to the
ledger, and surfaces in an audit six months later.

**Fix.** The search no longer stops at the first solution. It continues until it
finds a second, and returns `None` if one exists. Ambiguity is an exception, not
a tiebreak.

**Cost.** Roughly 2× search on solvable inputs. Worth it.

---

## 4. Double-counted exception money

**Symptom.** Unexplained total was ₹4,72,540.96 across "2 exceptions" for what
was visibly one problem row.

**Cause.** A bank line whose LLM proposal the verifier rejected got an exception
from `verify()`, then fell through to the unmatched-lines loop and got a second
one. The rupees were counted twice.

**Fix.** The unmatched loop skips refs already flagged.

**Structural change.** Added `test_money_conservation`, which asserts matches
plus distinct exception refs equals the bank-line count. Double counting can no
longer pass silently.

---

## 5. Two defect injectors were dead code

**Symptom.** `clubbed_credit` never appeared in the injected list.

**Cause.** It requires two bank lines on the same value date, but I was grouping
settlements one-per-day, so no two ever collided. The injector was unreachable —
and so, therefore, was the subset-sum path it was written to exercise. I had a
tested solver that production data could never reach.

**Fix.** Settlements now batch by `(date, rail)` with UPI and cards on separate
cycles, which is how Razorpay actually settles. Same-day pairs exist, the
injector fires, and Layer 1's subset-sum resolves one real clubbed credit in the
demo run.

**Lesson.** A unit test proving the solver works is not evidence the solver is
ever used. I now check the rule histogram (`Counter(m.rule for m in matches)`)
after every generator change to confirm each code path is actually reached.

---

## 6. My synthetic data got *easier* the bigger it got

**Symptom.** Building the exception queue, I wanted a fuller screen, so I ran
the pipeline at three scales to pick a good demo size:

```
  500 payments /  21d -> match  97.6%   exceptions 2
 2000 payments /  90d -> match  99.4%   exceptions 3
 5000 payments / 120d -> match  99.6%   exceptions 2
```

The match rate went *up* with volume and the exception count stayed flat.

**Why that's wrong.** Real settlement files do not get cleaner as they grow.
Defect rates are roughly proportional to throughput — more settlements means
more truncated narrations, more clubbed credits, more reserve holds. A 5,000-row
file should be *harder* than a 500-row one, not easier.

**Root cause.** Three of my defect injectors were hard-coded to a fixed count:
exactly one duplicate UTR, exactly one clubbed credit, exactly one reserve hold
— the clubbed-credit loop even had a literal `break` after the first hit. Only
`narration_truncated` scaled, because it happened to be written as
`len(bank) // 6`. So as the dataset grew, a constant number of defects got
diluted across an ever-larger denominator.

**Why it mattered more than it looked.** I was about to publish 99.6% as a
headline number. It would have been a real measurement of a fake problem, and
the first judge to run `--payments 5000` and see it climb would have correctly
concluded the data was rigged. It also meant `subset_sum` — the most
interesting code in the repo — was being exercised exactly once no matter how
much data I threw at it.

**Fix.** Every injector is now proportional: reserve holds at
`len(settlements) // 30`, duplicate UTRs at `len(bank) // 40`, clubbed credits
at `len(bank) // 25`. Hold amounts are randomised rather than a fixed ₹2,500.

Match rate now sits at ~96.5% and stays there across all three scales, and
subset-sum fires 1 / 6 / 8 times respectively.

**Structural change.** `test_difficulty_does_not_decay_with_volume` runs all
three sizes and asserts the match rate never exceeds 99% while false matches
stay at zero. The generator can no longer quietly flatter the matcher.

**Lesson.** I had been checking that each defect *existed*. I had not checked
that it existed *at the right rate*. For synthetic data, the distribution is
part of the contract, not a detail of the fixture — and a benchmark you author
yourself will drift toward flattering you unless something asserts otherwise.

---

## 7. The headline claim was not true

**Symptom.** None. Everything passed. I only found this because I went looking
for weaknesses before recording the video, and grepped for where the order
ledger was consumed:

```
$ grep -c "ds.orders" quittance/matching.py quittance/pipeline.py quittance/verify.py
0
0
0
```

**What was wrong.** The README's first line called this a *three-way*
reconciliation — bank statement, settlement report, and merchant order ledger.
The generator dutifully built an order ledger and attached `order_id` to every
row. Nothing ever read it. The pipeline did bank-to-settlement plus tax: a
two-way match with a good story.

**Why it survived so long.** Forty tests passed, because every one of them
tested what the code *did* rather than what the README *claimed*. There was no
test asserting the third source was consumed at all, so an entirely absent
feature looked exactly like a working one. The dataclass existed, the field was
populated, the docs described it — every signal except execution said it was
there.

**Fix.** Built `orders.py`: explodes each settlement to order level and asks
three questions per payment row — does an order exist, does its value agree to
the paisa, and has any order settled twice. It found 18 real defects in the
existing dataset that had been invisible: 8 settlement rows pointing at orders
absent from the ledger, and 10 partial captures where the order value and the
captured amount disagreed.

**Structural change.** `test_order_leg_actually_runs` asserts the leg executed
at all. Claims in the README that correspond to pipeline stages now have a test
that the stage runs, not merely that it returns sensible values when called.

**Lesson.** The dangerous defect is not the failing test, it is the capability
that is documented, plausible, and never invoked. I had been verifying outputs;
I had not verified that every input source was read. For anything that claims to
reconcile N sources, assert N.

---

## 8. A benchmark that was measuring a 403

**Symptom.** First run of the ablation with a real API key:

```
no solver · model   api.groq.com:openai/gpt-o   110/172  64.0%   via L2 0   rejected 0
```

Zero matches via Layer 2 and zero rejections. I nearly wrote this up as "the
model contributed nothing" — it agreed with my thesis, which is exactly why it
should have been suspicious.

**What tipped me off.** Zero *rejections* was the tell. If the model had
answered badly, the verifier would have thrown proposals out and the rejected
count would be non-zero. Zero of both means nothing reached the verifier at
all — the model was never really asked.

**Root cause.** Two bugs stacked.

First: `urllib` sends `User-Agent: Python-urllib/3.13` by default, and
Cloudflare answers that with `403 error 1010`. Every request was being blocked
before it reached Groq.

Second, and much worse: my exception handler caught `URLError` and returned
`[]` — the same value as "the model considered this line and had no candidate."
A transport failure and a negative result were indistinguishable. **The
benchmark was reporting a network error as a scientific finding.**

**Fix.** An explicit `User-Agent`, and error counters (`calls`, `errors`,
`rate_limited`, `last_error`) surfaced on the `Report` so a failed call can
never masquerade as an empty one.

Rerunning immediately exposed a third problem the counters made visible:
`calls: 3, errors: 59, last: HTTP 429`. Free tiers meter per minute. Added
2.1s pacing and exponential backoff honouring `retry-after`.

Final clean run: **62 calls, 0 errors, 48 matches via Layer 2** — and the
honest conclusion flipped. The model is substantially better than the heuristic
at reading mangled narration (48 vs 30). It is still worse than arithmetic.

**Structural change.** Any client that can fail over a network now counts its
failures, and the count is printed next to the result. A benchmark that cannot
distinguish "no answer" from "no connection" is not a benchmark.

**Lesson.** The dangerous measurement is the one that confirms what you already
believe. I had a thesis — *the model is the wrong tool here* — and the first
run agreed with it for entirely the wrong reason. If I had shipped that number,
I would have argued the right conclusion from fabricated evidence, and the
first judge to set an API key would have got a different answer than my README.
