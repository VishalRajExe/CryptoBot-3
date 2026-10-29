"""Shadow-mode AI research adapter.

Importing this package never imports a model SDK and never touches the trading
API. AI output is evidence to be scored, not an input to execution.
"""

from cryptobot.ai.shadow import (
    BIASES,
    MODE,
    PROMPT_VERSION,
    REGIMES,
    AssessmentProvider,
    MarketContext,
    NullProvider,
    ProviderResponse,
    ShadowAnalyst,
    ShadowAssessment,
    StaticProvider,
    build_context_from_bars,
    build_prompt,
    parse_assessment,
)

__all__ = [
    "BIASES",
    "MODE",
    "PROMPT_VERSION",
    "REGIMES",
    "AssessmentProvider",
    "MarketContext",
    "NullProvider",
    "ProviderResponse",
    "ShadowAnalyst",
    "ShadowAssessment",
    "StaticProvider",
    "build_context_from_bars",
    "build_prompt",
    "parse_assessment",
]
