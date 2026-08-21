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
import time
import urllib.error
import urllib.request
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


class OpenAICompatClient:
    """Any OpenAI-compatible chat endpoint, over stdlib urllib.

    Groq, OpenRouter, Together, and local vLLM all speak this shape. Written
    against ``urllib`` rather than a vendor SDK so the package keeps zero
    runtime dependencies -- a judge can clone and run without installing
    anything, which matters more here than SDK ergonomics.
    """

    #: Free tiers are metered per minute. Groq allows 30 rpm, so pace at just
    #: under that rather than sprinting into a wall of 429s.
    MIN_INTERVAL = 2.1
    MAX_RETRIES = 4

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self.name = f"{base_url.split('//')[-1].split('/')[0]}:{model}"
        self.calls = 0
        self.errors = 0
        self.rate_limited = 0
        self.last_error: str | None = None
        self._last_call = 0.0

    def _send(self, req) -> dict | None:
        """One request, paced and retried. Returns ``None`` on give-up."""
        for attempt in range(self.MAX_RETRIES):
            gap = time.monotonic() - self._last_call
            if gap < self.MIN_INTERVAL:
                time.sleep(self.MIN_INTERVAL - gap)
            self._last_call = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self.rate_limited += 1
                    retry_after = exc.headers.get("retry-after")
                    wait = float(retry_after) if retry_after else 2 ** (attempt + 1)
                    time.sleep(min(wait, 30))
                    continue
                self.errors += 1
                self.last_error = f"HTTP {exc.code}"
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None

        self.errors += 1
        self.last_error = "gave up after repeated 429s"
        return None

    def propose(self, line: BankLine, candidates: list[tuple[str, int, str]]) -> list[dict]:

        prompt = PROMPT.format(
            line_id=line.line_id,
            date=line.value_date,
            amount=line.amount,
            narration=line.narration,
            candidates="\n".join(f"  {sid}  {net}  {d}" for sid, net, d in candidates)
            or "  (none)",
        )
        body = json.dumps({
            "model": self._model,
            "temperature": 0,
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            self._url, data=body,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                # Required. Without an explicit UA, urllib sends
                # "Python-urllib/3.x" and Cloudflare returns 403 error 1010.
                # This cost a benchmark run that silently reported zero
                # matches as though the model had simply found nothing.
                "User-Agent": "quittance/0.1 (settlement reconciliation)",
            },
        )
        payload = self._send(req)
        if payload is None:
            return []

        self.calls += 1
        message = payload.get("choices", [{}])[0].get("message", {})
        # Reasoning models put the answer in `content` and the scratchpad in
        # `reasoning`. Read only `content`; the scratchpad is not an answer.
        text = message.get("content") or ""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            return json.loads(match.group(0)).get("candidates", [])
        except json.JSONDecodeError:
            return []


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
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            return json.loads(match.group(0)).get("candidates", [])
        except json.JSONDecodeError:
            return []


#: Provider precedence. First key present wins. Deliberately provider-agnostic:
#: the architecture's claim is about *where* a model belongs, not whose.
PROVIDERS = (
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    ("TOGETHER_API_KEY", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
)


def build_client(utr_index: dict[str, str], force_offline: bool = False) -> LLMClient:
    """Pick an engine. The report always names which one ran."""
    if force_offline:
        return HeuristicClient(utr_index)

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicClient()
        except (ImportError, RuntimeError, ValueError):
            pass

    for env, base, model in PROVIDERS:
        key = os.environ.get(env)
        if key:
            return OpenAICompatClient(base, key, os.environ.get("QUITTANCE_MODEL", model))

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
