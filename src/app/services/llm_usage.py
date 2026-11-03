"""Token and cost metering for one plan generation.

A plan generation makes many Claude calls (the plan itself, critics,
exploration, grounding checks), all through ``ClaudeClient._post_messages``.
Each response's ``usage`` is added to the meter that is active in the current
async context, so the run record carries the whole generation's tokens and
cost instead of the zero it used to default to.

``begin()`` starts a fresh meter and must be called at the top of each
generation: the queue watcher runs generations one after another in a single
task, so a meter left over from the previous ticket would otherwise keep
growing. Calls made while no meter is active are simply not counted.

Prices are USD per million tokens, Anthropic first-party rates (checked
2026-11-03). Cache writes bill at 1.25x input, cache reads at 0.1x input, and
fast mode at 2x both. A model missing from the table still has its tokens
counted; its cost is left out and the meter says so (``unpriced_models``)
rather than reporting a cost that looks complete.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from decimal import Decimal

logger = logging.getLogger(__name__)

# (input, output) USD per million tokens. Longest prefix wins, so dated or
# suffixed ids resolve to their family entry.
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-mythos-5": (Decimal("10"), Decimal("50")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-4": (Decimal("3"), Decimal("15")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-haiku-4": (Decimal("1"), Decimal("5")),
}

CACHE_WRITE_MULTIPLIER = Decimal("1.25")
CACHE_READ_MULTIPLIER = Decimal("0.1")
FAST_MODE_MULTIPLIER = Decimal("2")
_MTOK = Decimal(1_000_000)

_meter: ContextVar[dict | None] = ContextVar("llm_usage_meter", default=None)


def _new_meter() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "calls": 0,
        "cost_usd": Decimal("0"),
        "unpriced_models": [],
    }


def begin() -> dict:
    """Start a fresh meter for this generation and return it."""
    meter = _new_meter()
    _meter.set(meter)
    return meter


def current() -> dict | None:
    return _meter.get()


def price_for(model: str | None) -> tuple[Decimal, Decimal] | None:
    if not model:
        return None
    best = None
    for prefix, price in PRICES_PER_MTOK.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def cost_of(model: str | None, usage: dict, *, fast: bool = False) -> Decimal | None:
    """USD cost of one response's usage, or None when the model is unpriced."""
    price = price_for(model)
    if price is None:
        return None
    in_price, out_price = price
    if fast:
        in_price *= FAST_MODE_MULTIPLIER
        out_price *= FAST_MODE_MULTIPLIER
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    total = (
        inp * in_price
        + cw * in_price * CACHE_WRITE_MULTIPLIER
        + cr * in_price * CACHE_READ_MULTIPLIER
        + out * out_price
    )
    return total / _MTOK


def record(model: str | None, usage: dict | None) -> None:
    """Add one Claude response's usage to the active meter, if any."""
    meter = _meter.get()
    if meter is None or not usage:
        return
    meter["calls"] += 1
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            meter[key] += value
    cost = cost_of(model, usage, fast=usage.get("speed") == "fast")
    if cost is None:
        if model not in meter["unpriced_models"]:
            meter["unpriced_models"].append(model)
            logger.warning(
                "llm_usage: no price for model %r; its tokens are counted but "
                "its cost is left out of the run's cost_usd",
                model,
            )
        return
    meter["cost_usd"] += cost


def prompt_tokens(meter: dict) -> int:
    """All input tokens the generation sent, cached or not."""
    return (
        meter["input_tokens"]
        + meter["cache_creation_input_tokens"]
        + meter["cache_read_input_tokens"]
    )
