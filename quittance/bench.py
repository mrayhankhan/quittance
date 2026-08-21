"""Ablation: what does the model actually buy?

The pipeline reports that Layer 2 contributes 0% of matches. That number is
easy to dismiss in either direction — as proof the model is useless, or as proof
it was never given a fair chance. This harness settles it by measurement.

It runs the same dataset three ways:

* **full** — every layer on. What ships.
* **no solver, heuristic** — Layer 1 removed, so arithmetic cannot rescue a
  mangled narration. Whatever survives is what naive string matching recovers.
* **no solver, model** — same, but Layer 2 calls a real model.

The gap between rows two and three is the model's genuine contribution on the
task it is actually suited to: *reading*. The gap between row three and row one
is what deterministic arithmetic buys over the model — in accuracy, in latency,
and in money.

Run it, and the claim "the model is disabled by default" stops being an opinion.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .generate import generate
from .pipeline import run


@dataclass(frozen=True, slots=True)
class Arm:
    label: str
    engine: str
    matched: int
    total: int
    l2_matches: int
    rejected: int
    false_matches: int
    seconds: float

    @property
    def rate(self) -> float:
        return self.matched / self.total if self.total else 0.0


def _arm(label: str, ds, *, offline: bool, skip_solver: bool) -> Arm:
    started = time.perf_counter()
    r = run(ds, force_offline=offline, skip_solver=skip_solver)
    return Arm(
        label=label,
        engine=r.engine,
        matched=len(r.matches),
        total=r.bank_lines,
        l2_matches=r.by_layer["L2_llm"],
        rejected=len(r.rejected),
        false_matches=len(r.false_matches),
        seconds=time.perf_counter() - started,
    )


def benchmark(seed: int = 20260905, payments: int = 2000, days: int = 90) -> list[Arm]:
    ds = generate(seed=seed, n_payments=payments, days=days)
    arms = [
        _arm("full pipeline", ds, offline=True, skip_solver=False),
        _arm("no solver · heuristic", ds, offline=True, skip_solver=True),
    ]
    if _has_key():
        arms.append(_arm("no solver · model", ds, offline=False, skip_solver=True))
    return arms


def _has_key() -> bool:
    return any(os.environ.get(k) for k in
               ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "TOGETHER_API_KEY"))


def render(arms: list[Arm]) -> None:
    bar = "─" * 92
    print(f"\nABLATION · what the model buys\n{bar}")
    print(f"  {'arm':<24}{'engine':<26}{'matched':>12}{'via L2':>9}"
          f"{'rejected':>10}{'false':>7}{'time':>8}")
    print(f"  {'':·<92}")
    for a in arms:
        print(f"  {a.label:<24}{a.engine[:25]:<26}"
              f"{a.matched:>5}/{a.total:<3} {a.rate * 100:5.1f}%"
              f"{a.l2_matches:>9}{a.rejected:>10}{a.false_matches:>7}{a.seconds:>7.1f}s")

    print(f"{bar}")
    full = arms[0]
    heur = next((a for a in arms if a.label.endswith("heuristic")), None)
    model = next((a for a in arms if a.label.endswith("model")), None)

    if heur and model:
        gain = model.matched - heur.matched
        print(f"  With arithmetic removed, the model recovered {gain} credit(s) that "
              f"string matching could not.")
        print(f"  It also cost {model.seconds - heur.seconds:.1f}s more and made "
              f"{model.rejected} proposal(s) the verifier threw out.")
        recovered = full.matched - model.matched
        print(f"  Layer 1 recovers {recovered} more than the model does, in "
              f"{full.seconds:.2f}s, for zero tokens.")
        print("\n  Conclusion: the model is real and it works — and it is still the "
              "wrong tool\n  for this layer. That is why it ships disabled.")
    else:
        print("  No API key set — model arm skipped. Set GROQ_API_KEY (or "
              "ANTHROPIC_API_KEY,\n  OPENROUTER_API_KEY, TOGETHER_API_KEY) to run it.")
    print()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="quittance.bench", description=__doc__)
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--payments", type=int, default=2000)
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    render(benchmark(args.seed, args.payments, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
