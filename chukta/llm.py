"""Layer 2: the narrow slice where a language model earns its place.

By the time a row reaches this module, exact matching and the solver have both
declined it. What is left is almost always a *reading* problem rather than a
counting problem -- a mangled bank narration, an adjustment code, a
free-text description that a human would glance at and understand.

Two constraints define this layer:

* **The model never performs arithmetic.** It nominates candidate settlements
  and explains why. Layer 3 does the sums. A model that is good at reading
  ``ADJ-CHGBK-8821`` is not thereby good at adding 312 numbers, and asking it
  to do both is how you get a confidently wrong ledger.

* **It runs offline by default.** ``HeuristicClient`` is a deterministic
  string-matching fallback used when no API key is present, so ``make demo``
  works on a clean clone. The pipeline reports which client ran, because a
  benchmark that silently changes engine is not a benchmark.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

from .schema import BankLine, Layer, Match

MODEL = "claude-sonnet-5"

PROMPT = """You reconcile Indian payment-gateway settlements.

A bank credit could not be matched to a settlement by exact identifier or by \
arithmetic. Read the narration and nominate which settlement it is likely to be.

Rules you must follow:
- Do NOT compute or verify sums. A separate deterministic checker does that.
- Nominate only from the candidate list given.
- If nothing is a plausible read, return an empty candidates list.
- Cite the exact substring of the narration that drove your answer.

Bank line:
  id:        {line_id}
  date:      {date}
  amount:    {amount} paisa
  narration: {narration}

Candidate settlements (id, net paisa, date):
{candidates}

Return JSON only:
{{"candidates": [{{"settlement_id": "...", "confidence": 0.0-1.0, \
"evidence": "exact substring from the narration"}}]}}
"""


class LLMClient(Protocol):
    name: str

    def propose(self, line: BankLine, candidates: list[tuple[str, int, str]]) -> list[dict]: ...


class HeuristicClient:
    """Offline stand-in. Deterministic, no network, no key.

    Recovers the common real case: a truncated narration that still carries
    enough of the UTR prefix to identify the settlement unambiguously.
    """

    name = "heuristic-offline"

    def __init__(self, utr_index: dict[str, str]) -> None:
        self._utr_index = utr_index

    def propose(self, line: BankLine, candidates: list[tuple[str, int, str]]) -> list[dict]:
        allowed = {sid for sid, _, _ in candidates}
        out: list[dict] = []

        for token in re.findall(r"[0-9a-z]{6,}", line.narration.lower()):
            for utr, sid in self._utr_index.items():
                if sid in allowed and utr.lower().startswith(token) and len(token) >= 8:
                    out.append({
                        "settlement_id": sid,
                        "confidence": 0.75,
                        "evidence": f"narration prefix {token!r} matches UTR {utr}",
                    })
        return out[:1]


class AnthropicClient:
    """Real client. Used when ANTHROPIC_API_KEY is set."""

    name = f"anthropic:{MODEL}"

    def __init__(self) -> None:
        from anthropic import Anthropic  # imported lazily; optional dependency

        self._client = Anthropic()

    def propose(self, line: BankLine, candidates: list[tuple[str, int, str]]) -> list[dict]:
        prompt = PROMPT.format(
            line_id=line.line_id,
            date=line.value_date,
            amount=line.amount,
            narration=line.narration,
            candidates="\n".join(f"  {sid}  {net}  {d}" for sid, net, d in candidates) or "  (none)",
        )
        resp = self._client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return []
        try:
            return json.loads(match.group(0)).get("candidates", [])
        except json.JSONDecodeError:
            return []


def build_client(utr_index: dict[str, str], force_offline: bool = False) -> LLMClient:
    if force_offline or not os.environ.get("ANTHROPIC_API_KEY"):
        return HeuristicClient(utr_index)
    try:
        return AnthropicClient()
    except Exception:
        return HeuristicClient(utr_index)


def layer2_propose(
    client: LLMClient,
    unresolved: list[BankLine],
    candidates: list[tuple[str, int, str]],
) -> list[Match]:
    """Turn model nominations into unverified proposals.

    Nothing here is a match yet. Everything returned goes to Layer 3.
    """
    proposals: list[Match] = []
    for line in unresolved:
        for c in client.propose(line, candidates):
            sid = c.get("settlement_id")
            if not sid:
                continue
            proposals.append(
                Match(
                    bank_line_id=line.line_id,
                    settlement_id=sid,
                    amount=line.amount,
                    layer=Layer.LLM,
                    rule=f"llm_nomination[{client.name}]",
                    confidence=float(c.get("confidence", 0.0)),
                    evidence=(str(c.get("evidence", "")),),
                )
            )
    return proposals
