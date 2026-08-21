"""Command line entry point: ``python -m quittance``."""

from __future__ import annotations

import argparse
from collections import Counter

from .generate import generate
from .money import inr
from .pipeline import Report, run
from .schema import Layer

BAR = "─" * 74


def _head(title: str) -> None:
    print(f"\n{title}\n{BAR}")


def render(report: Report) -> None:
    _head("QUITTANCE · settlement reconciliation")
    print(f"  seed {report.dataset_seed}   bank lines {report.bank_lines}   "
          f"engine {report.engine}   {report.elapsed_ms:.0f} ms")

    _head("Match rate by layer")
    labels = {
        Layer.EXACT.value: "L0  exact identifier      no AI",
        Layer.SOLVER.value: "L1  constrained solver    no AI",
        Layer.LLM.value: "L2  model, verified by L3    AI",
    }
    counts = report.by_layer
    for key, label in labels.items():
        n = counts[key]
        pct = n / report.bank_lines * 100 if report.bank_lines else 0
        blocks = "█" * round(pct / 2.5)
        print(f"  {label}  {n:>4}  {pct:5.1f}%  {blocks}")
    print(f"  {'':<32}  {len(report.matches):>4}  {report.match_rate * 100:5.1f}%  total")

    _head("Money")
    print(f"  matched                {inr(report.matched_amount):>18}")
    print(f"  unexplained            {inr(report.unexplained_amount):>18}")
    print(f"  ITC at risk            {inr(report.itc_at_risk):>18}")

    _head("Correctness")
    fm = len(report.false_matches)
    verdict = "PASS" if fm == 0 else "FAIL"
    print(f"  false matches                       {fm:>4}   [{verdict}]")
    print(f"  proposals rejected by verifier      {len(report.rejected):>4}")
    print(f"  exceptions raised                   {len(report.exceptions):>4}")
    for line_id, claimed, truth in report.false_matches[:5]:
        print(f"    ! {line_id}: claimed {claimed}, actually {truth}")

    if report.rejected:
        _head("Verifier rejections")
        for m in report.rejected[:5]:
            print(f"  {m.bank_line_id}  {m.rule}  conf {m.confidence:.2f}")
            for e in m.evidence:
                print(f"      evidence: {e}")

    _head("Exceptions by type")
    for code, n in Counter(e.code.value for e in report.exceptions).most_common():
        total = sum(e.amount for e in report.exceptions if e.code.value == code)
        print(f"  {code:<24} {n:>4}   {inr(total):>16}")

    _head("Input tax credit")
    for line in report.itc:
        flag = "  " if line.status.value == "claimable" else "! "
        print(f"  {flag}{line.period}  {line.invoice_no:<16} {line.status.value:<16}"
              f" at risk {inr(line.at_risk):>12}")
        print(f"       {line.detail}")

    print()


def main() -> int:
    ap = argparse.ArgumentParser(prog="quittance", description=__doc__)
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--payments", type=int, default=500)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--offline", action="store_true",
                    help="force the deterministic Layer 2 fallback")
    ap.add_argument("--report", metavar="PATH", nargs="?", const="out/report.html",
                    help="write the exception queue to a self-contained HTML file")
    ap.add_argument("--open", action="store_true",
                    help="open the report in a browser after writing it")
    args = ap.parse_args()

    ds = generate(seed=args.seed, n_payments=args.payments, days=args.days)
    report = run(ds, force_offline=args.offline)
    render(report)

    if args.report:
        from .report import write_report

        path = write_report(report, args.report)
        print(f"  exception queue written to {path}\n")
        if args.open:
            import webbrowser

            webbrowser.open(f"file://{path}")

    return 1 if report.false_matches else 0


if __name__ == "__main__":
    raise SystemExit(main())
