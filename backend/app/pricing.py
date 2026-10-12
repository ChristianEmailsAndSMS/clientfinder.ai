"""Per-model token prices, USD per 1M tokens, and the customer markup.

Source: Anthropic's model table as shipped in the `claude-api` skill (cached 2026-10-06). Prices change: re-check
https://www.anthropic.com/pricing before launch and quarterly, and edit THIS file only. Every customer charge is computed
from it, so a wrong number here means a wrong invoice.

Tuple = (input, output) or (input, output, long_input, long_output) where the long rate card applies when the prompt is
longer than LONG_PROMPT_TOKENS (Haiku 5.5 charges more for prompts over 100K tokens)."""
from decimal import ROUND_CEILING, Decimal

LONG_PROMPT_TOKENS = 100_000

PRICES: dict[str, tuple[float, ...]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.50, 2.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),        # old dated id still found in older .env files
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
}

# What a customer is charged = our cost x this. One place to change it.
CREDIT_MARKUP = 1.5

# Prices are USD per MILLION tokens, which is exactly micro-USD per TOKEN. So all money below is exact integer-ish
# micro-USD arithmetic in Decimal: no floating-point drift, and a charge is the real markup, never a rounding accident.
_MARKUP = Decimal(str(CREDIT_MARKUP))


def rates(model: str | None, input_tokens: int) -> tuple[Decimal, Decimal] | None:
    p = PRICES.get(model or "")
    if p is None:
        return None
    i, o = (p[2], p[3]) if (len(p) == 4 and input_tokens > LONG_PROMPT_TOKENS) else (p[0], p[1])
    return Decimal(str(i)), Decimal(str(o))


def cost_micro_exact(model: str | None, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Our real cost of one call in micro-USD (exact). Output tokens include thinking tokens (that is what usage.output_tokens
    reports). None when the model has no price entry."""
    r = rates(model, input_tokens)
    return None if r is None else input_tokens * r[0] + output_tokens * r[1]


def cost_micro(model: str | None, input_tokens: int, output_tokens: int) -> int | None:
    """Our cost, rounded UP to a whole micro-USD (never understates what we spent)."""
    c = cost_micro_exact(model, input_tokens, output_tokens)
    return None if c is None else int(c.to_integral_value(ROUND_CEILING))


def charge_micro(model: str | None, input_tokens: int, output_tokens: int) -> int | None:
    """What the customer pays: our exact cost x CREDIT_MARKUP, rounded UP to a whole micro-USD. None for an unpriced model:
    callers must refuse to run it for a paying user rather than guess."""
    c = cost_micro_exact(model, input_tokens, output_tokens)
    return None if c is None else int((c * _MARKUP).to_integral_value(ROUND_CEILING))


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    c = cost_micro(model, input_tokens, output_tokens)
    return None if c is None else c / 1_000_000


def charge_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    c = charge_micro(model, input_tokens, output_tokens)
    return None if c is None else c / 1_000_000
