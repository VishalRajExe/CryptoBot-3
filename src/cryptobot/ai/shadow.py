"""Shadow-mode AI analyst.

Phase 9, deliberately last and deliberately toothless. The design rules are
structural, not procedural:

* This module imports **nothing** from ``execution``, ``sizing``, or ``risk``.
  There is no way for an assessment to become an order.
* It holds no broker credentials and cannot reach the trading API.
* Every call is bounded by a wall-clock limit and a cost limit, and every failure
  - timeout, overspend, malformed JSON - is recorded and swallowed.
* The default provider is :class:`NullProvider`, which refuses to run. Enabling a
  real provider is an explicit configuration act, and even then the output is
  only ever stored and later scored.

The value of shadow mode is the *record*: model, prompt hash, structured
assessment, and the realised outcome. Only that record can tell us whether the
model adds anything over the deterministic baseline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from cryptobot.core.contracts import normalize_symbol, to_decimal
from cryptobot.core.errors import CryptobotError, MissingDependency
from cryptobot.core.logging import get_logger
from cryptobot.core.timeframes import duration as timeframe_duration
from cryptobot.journal.store import Journal

log = get_logger(__name__)
UTC = timezone.utc

PROMPT_VERSION = "shadow-v1"
MODE = "shadow"

REGIMES = frozenset({"trending_up", "trending_down", "ranging", "high_volatility", "unknown"})
BIASES = frozenset({"bullish", "bearish", "neutral"})

MAX_OUTPUT_CHARS = 4_000
DEFAULT_MAX_LATENCY_MS = 10_000
DEFAULT_MAX_COST_USD = Decimal("0.02")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AssessmentError(CryptobotError):
    """The provider failed or returned something unusable."""


@dataclass(frozen=True)
class MarketContext:
    """Everything the model is allowed to see: closed bars only."""

    symbol: str
    timeframe: str
    as_of: datetime
    last_price: Decimal
    indicators: Mapping[str, Decimal] = field(default_factory=dict)
    returns: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "last_price": format(to_decimal(self.last_price, "last_price"), "f"),
            "indicators": {k: format(v, "f") for k, v in sorted(self.indicators.items())},
            "returns": {k: format(v, "f") for k, v in sorted(self.returns.items())},
        }


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0


class AssessmentProvider(Protocol):
    name: str

    def assess(self, prompt: str, context: MarketContext) -> ProviderResponse: ...


class NullProvider:
    """Default: no model is configured, so nothing is sent anywhere."""

    name = "null"

    def assess(self, prompt: str, context: MarketContext) -> ProviderResponse:
        raise MissingDependency(
            "no AI provider configured; shadow assessments are disabled. "
            "This is the intended default and does not affect trading."
        )


class StaticProvider:
    """Deterministic provider for tests and for exercising the record path."""

    name = "static"

    def __init__(self, payload: str | None = None, *, latency_ms: int = 5) -> None:
        self._payload = payload
        self._latency_ms = latency_ms

    def assess(self, prompt: str, context: MarketContext) -> ProviderResponse:
        text = self._payload
        if text is None:
            text = json.dumps(
                {
                    "regime": "ranging",
                    "bias": "neutral",
                    "confidence": 0.5,
                    "horizon_bars": 12,
                    "rationale": "static provider: no view expressed",
                    "key_risks": ["static fixture, not a model output"],
                }
            )
        return ProviderResponse(
            text=text,
            model="static",
            tokens_in=len(prompt) // 4,
            tokens_out=len(text) // 4,
            cost_usd=Decimal("0"),
            latency_ms=self._latency_ms,
        )


@dataclass(frozen=True)
class ShadowAssessment:
    assessment_id: str
    symbol: str
    timeframe: str
    bar_ts: datetime
    created_at: datetime
    provider: str
    model: str
    prompt_hash: str
    regime: str
    bias: str
    confidence: Decimal
    horizon_bars: int
    rationale: str
    key_risks: tuple[str, ...]
    latency_ms: int
    cost_usd: Decimal
    raw_text: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "ts": self.created_at.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_ts": self.bar_ts.isoformat(),
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "mode": MODE,
            "latency_ms": self.latency_ms,
            "cost_usd": format(self.cost_usd, "f"),
            "payload": {
                "prompt_version": PROMPT_VERSION,
                "regime": self.regime,
                "bias": self.bias,
                "confidence": format(self.confidence, "f"),
                "horizon_bars": self.horizon_bars,
                "rationale": self.rationale,
                "key_risks": list(self.key_risks),
                "raw": self.raw_text[:MAX_OUTPUT_CHARS],
            },
        }

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.regime}/{self.bias} "
            f"conf={self.confidence} horizon={self.horizon_bars}b "
            f"({self.provider}:{self.model}, {self.latency_ms}ms, ${self.cost_usd})"
        )


class ShadowAnalyst:
    """Runs assessments and records them. Never raises, never trades."""

    def __init__(
        self,
        journal: Journal,
        provider: AssessmentProvider | None = None,
        *,
        enabled: bool = False,
        max_latency_ms: int = DEFAULT_MAX_LATENCY_MS,
        max_cost_usd: Decimal = DEFAULT_MAX_COST_USD,
        clock: Callable[[], datetime] = _utcnow,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._journal = journal
        self._provider = provider or NullProvider()
        self._enabled = enabled
        self._max_latency_ms = int(max_latency_ms)
        self._max_cost = to_decimal(max_cost_usd, "max_cost_usd")
        self._clock = clock
        self._timer = timer
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and not isinstance(self._provider, NullProvider)

    @property
    def provider_name(self) -> str:
        return self._provider.name

    # -- public API ---------------------------------------------------------

    def assess(self, context: MarketContext) -> ShadowAssessment | None:
        if not self.enabled:
            return None
        prompt = build_prompt(context)
        started = self._timer()
        try:
            response = self._provider.assess(prompt, context)
        except MissingDependency:
            self.last_error = "provider unavailable"
            return None
        except Exception as exc:  # noqa: BLE001 - an AI failure is never fatal
            self._fail("provider_error", str(exc))
            return None

        elapsed_ms = int((self._timer() - started) * 1000)
        latency = max(elapsed_ms, response.latency_ms)

        if latency > self._max_latency_ms:
            self._fail("latency_limit", f"{latency}ms > {self._max_latency_ms}ms")
            return None
        if response.cost_usd > self._max_cost:
            self._fail("cost_limit", f"${response.cost_usd} > ${self._max_cost}")
            return None
        if len(response.text) > MAX_OUTPUT_CHARS:
            self._fail("oversized_response", f"{len(response.text)} chars")
            return None

        parsed = parse_assessment(response.text)
        if parsed is None:
            self._fail("malformed_response", response.text[:200])
            return None

        assessment = ShadowAssessment(
            assessment_id=f"ai-{uuid.uuid4().hex[:16]}",
            symbol=context.symbol,
            timeframe=context.timeframe,
            bar_ts=context.as_of,
            created_at=self._clock(),
            provider=self._provider.name,
            model=response.model,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32],
            regime=parsed["regime"],
            bias=parsed["bias"],
            confidence=parsed["confidence"],
            horizon_bars=parsed["horizon_bars"],
            rationale=parsed["rationale"],
            key_risks=parsed["key_risks"],
            latency_ms=latency,
            cost_usd=response.cost_usd,
            raw_text=response.text,
        )
        try:
            self._journal.save_ai_assessment(assessment.to_payload())
        except Exception as exc:  # noqa: BLE001
            self._fail("persistence_error", str(exc))
            return None
        return assessment

    def evaluate_outcomes(
        self,
        price_lookup: Callable[[str, datetime], Decimal | None],
        *,
        limit: int = 100,
    ) -> dict[str, int]:
        """Score stored assessments against what the price actually did.

        ``price_lookup(symbol, ts)`` returns the close at (or after) ``ts``.
        """
        counters = {"evaluated": 0, "correct": 0, "incorrect": 0, "skipped": 0}
        for row in self._journal.unevaluated_assessments(limit=limit):
            payload = json.loads(row.get("payload_json") or "{}")
            horizon = int(payload.get("horizon_bars") or 0)
            if horizon <= 0:
                counters["skipped"] += 1
                continue
            start_ts = datetime.fromisoformat(row["bar_ts"])
            end_ts = start_ts + timeframe_duration(row["timeframe"]) * horizon
            start = price_lookup(row["symbol"], start_ts)
            end = price_lookup(row["symbol"], end_ts)
            if start is None or end is None or start <= 0:
                counters["skipped"] += 1
                continue
            change = (end - start) / start
            bias = str(payload.get("bias", "neutral"))
            realised = "bullish" if change > 0 else "bearish" if change < 0 else "neutral"
            correct = bias == realised or bias == "neutral"
            self._journal.mark_assessment_evaluated(
                row["assessment_id"],
                {
                    "start_price": format(start, "f"),
                    "end_price": format(end, "f"),
                    "change_pct": format(change * 100, ".4f"),
                    "realised_bias": realised,
                    "correct": correct,
                },
            )
            counters["evaluated"] += 1
            counters["correct" if correct else "incorrect"] += 1
        return counters

    # -- internals ----------------------------------------------------------

    def _fail(self, kind: str, detail: str) -> None:
        self.last_error = f"{kind}: {detail[:160]}"
        log.warning(
            "shadow assessment skipped",
            extra={"context": {"kind": kind, "detail": detail[:160]}},
        )
        try:
            self._journal.record_notification("ai", f"skipped:{kind}", detail[:200])
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# prompt + parsing
# ---------------------------------------------------------------------------


def build_prompt(context: MarketContext) -> str:
    payload = json.dumps(context.to_payload(), indent=2, sort_keys=True)
    return (
        f"You are an offline market analyst providing a SHADOW assessment.\n"
        f"Your output is recorded and later scored. It cannot place orders and is "
        f"never shown to an execution path.\n\n"
        f"Prompt version: {PROMPT_VERSION}\n"
        f"Closed-bar context (no future data):\n{payload}\n\n"
        f"Return ONE JSON object and nothing else, with exactly these keys:\n"
        f'  "regime": one of {sorted(REGIMES)}\n'
        f'  "bias": one of {sorted(BIASES)}\n'
        f'  "confidence": number between 0 and 1\n'
        f'  "horizon_bars": integer >= 1\n'
        f'  "rationale": string, <= 400 characters\n'
        f'  "key_risks": array of <= 3 short strings\n'
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Accept a bare object or one wrapped in prose / a code fence."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", candidate).strip()
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def parse_assessment(text: str) -> dict[str, Any] | None:
    """Strict validation. Anything unexpected is rejected, not coerced."""
    payload = extract_json(text)
    if payload is None:
        return None
    regime = str(payload.get("regime", "")).strip().lower()
    bias = str(payload.get("bias", "")).strip().lower()
    if regime not in REGIMES or bias not in BIASES:
        return None
    try:
        confidence = Decimal(str(payload.get("confidence"))).quantize(Decimal("0.01"))
        horizon = int(payload.get("horizon_bars"))
    except Exception:  # noqa: BLE001
        return None
    if not (Decimal("0") <= confidence <= Decimal("1")):
        return None
    if not 1 <= horizon <= 1000:
        return None
    rationale = str(payload.get("rationale", "")).strip()
    if not rationale:
        return None
    risks_raw = payload.get("key_risks", [])
    if not isinstance(risks_raw, Sequence) or isinstance(risks_raw, (str, bytes)):
        risks_raw = []
    key_risks = tuple(str(item)[:120] for item in list(risks_raw)[:3])
    return {
        "regime": regime,
        "bias": bias,
        "confidence": confidence,
        "horizon_bars": horizon,
        "rationale": rationale[:400],
        "key_risks": key_risks,
    }


def build_context_from_bars(
    bars: Sequence[Any],
    *,
    indicators: Mapping[str, Decimal] | None = None,
    windows: Sequence[int] = (1, 6, 24),
) -> MarketContext:
    """Derive a context from closed bars, reusing the indicators already computed."""
    if not bars:
        raise AssessmentError("cannot build a context without bars")
    last = bars[-1]
    returns: dict[str, Decimal] = {}
    for window in windows:
        if len(bars) > window:
            previous = bars[-1 - window].close
            if previous > 0:
                returns[f"ret_{window}b_pct"] = (
                    (last.close - previous) / previous * Decimal("100")
                )
    return MarketContext(
        symbol=last.symbol,
        timeframe=last.timeframe,
        as_of=last.closes_at,
        last_price=last.close,
        indicators=dict(indicators or {}),
        returns=returns,
    )


def outcome_window_end(context: MarketContext, horizon_bars: int) -> datetime:
    return context.as_of + timeframe_duration(context.timeframe) * horizon_bars
