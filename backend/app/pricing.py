"""Per-model token prices, USD per 1M tokens (input, output).

UNVERIFIED: these are my best recollection, not read from Anthropic's pricing page. Check
https://www.anthropic.com/pricing and edit before relying on any dollar figure. Phase 5 (credits =
2x API cost) will read the same table, so keep it the single source of truth."""
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """None when the model has no price entry (so reports show 'unknown' instead of a wrong number)."""
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000
