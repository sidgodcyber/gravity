"""Price table, USD per 1M tokens (input, output).

Verified 2026-07-05 against Anthropic's current pricing. Cache math for
Anthropic models: cache reads bill at 0.1x input, 5-minute cache writes at
1.25x input. Update this table when prices change; /budget only estimates.
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus": (5.0, 25.0),        # prefix fallback for older opus
    "claude-sonnet-5": (3.0, 15.0),    # intro $2/$10 through 2026-08-31; sticker used
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (1.0, 5.0),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25

# Conservative stand-in when a PAID model has no table entry: the governor
# must never see $0 for real spend, so assume opus-level pricing.
DEFAULT_PAID_PRICE = (5.0, 25.0)


def lookup(model: str) -> tuple[float, float] | None:
    """Longest-prefix match after stripping any provider prefix."""
    name = model.rsplit("/", 1)[-1].lower()
    best = None
    for key, price in PRICES.items():
        if name.startswith(key) and (best is None or len(key) > best[0]):
            best = (len(key), price)
    return best[1] if best else None


def estimate(model: str, tokens_in: int, tokens_out: int,
             cache_read: int = 0, cache_write: int = 0,
             assume_paid: bool = False) -> float:
    price = lookup(model)
    if price is None:
        if not assume_paid:
            return 0.0
        price = DEFAULT_PAID_PRICE
    p_in, p_out = price
    return (
        tokens_in * p_in
        + tokens_out * p_out
        + cache_read * p_in * CACHE_READ_MULT
        + cache_write * p_in * CACHE_WRITE_MULT
    ) / 1_000_000
