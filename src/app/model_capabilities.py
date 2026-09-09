"""The Claude model this bot runs on, and what that model's API accepts.

One place to edit when Anthropic ships a new model. The default id and the
per-model request quirks live together because they change together — bumping
the id on its own is how you earn a hard 400 on every call:

- Opus 4.7 dropped ``temperature``, and every model released after it kept it
  dropped. Sending it is a 400, not a warning.
- Opus 5 turned thinking on by *default*. That puts a ``thinking`` block ahead
  of the text block in the response, and spends part of ``max_tokens`` before
  a single visible token is written — so a 128-token cap that used to return a
  sentence now returns nothing at all.

Each capability is an allowlist of the versions known to accept the feature,
so a model id this file has never heard of (anything released after it) falls
back to the conservative request shape: a slightly duller call, never a 400.
That is the direction a stale file should fail in.
"""

import re

# The model every Claude call uses unless LLM_MODEL says otherwise.
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Cheapest current model. Only ever asked to say "hi" — it exists to prove an
# API key is live, so capability doesn't matter and price does.
TOKEN_VALIDATION_MODEL = "claude-haiku-4-5"

_MODEL_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[-.](\d+))?")

# Last version in each family whose API still accepts `temperature`.
_LAST_TEMPERATURE_VERSION = {"opus": (4, 6), "sonnet": (4, 6), "haiku": (4, 5)}

# First version in each family that accepts `output_config.effort`. Sonnet 4.5
# and Haiku 4.5 error on it; Opus 4.5 was the first to take it.
_FIRST_EFFORT_VERSION = {"opus": (4, 5), "sonnet": (4, 6), "fable": (5, 0), "mythos": (5, 0)}

# First version in each family where *omitting* `thinking` still thinks. Before
# these, no thinking unless you asked for it.
_FIRST_THINKING_BY_DEFAULT = {"opus": (5, 0), "sonnet": (5, 0), "fable": (5, 0), "mythos": (5, 0)}


def parse_model(model: str | None) -> tuple[str, tuple[int, int]] | None:
    """('opus', (4, 5)) for 'claude-opus-4-5-20251101'. None if unrecognised.

    A bare id with no minor ('claude-opus-5') reads as (5, 0), and any date
    suffix is ignored, so both id styles Anthropic ships compare cleanly.
    """
    match = _MODEL_RE.search((model or "").lower())
    if not match:
        return None
    family, major, minor = match.groups()
    return family, (int(major), int(minor or 0))


def supports_temperature(model: str | None) -> bool:
    """False for Opus 4.7+, Sonnet 5+, and anything unrecognised."""
    parsed = parse_model(model)
    if parsed is None:
        return False
    family, version = parsed
    ceiling = _LAST_TEMPERATURE_VERSION.get(family)
    return ceiling is not None and version <= ceiling


def supports_effort(model: str | None) -> bool:
    """True where `output_config.effort` is accepted."""
    parsed = parse_model(model)
    if parsed is None:
        return False
    family, version = parsed
    floor = _FIRST_EFFORT_VERSION.get(family)
    return floor is not None and version >= floor


def thinks_by_default(model: str | None) -> bool:
    """True where a request that never mentions `thinking` still thinks.

    Unrecognised ids answer True: reserving output budget that goes unused
    costs nothing (output is billed on tokens written, not on the cap), while
    failing to reserve it truncates the answer.
    """
    parsed = parse_model(model)
    if parsed is None:
        return True
    family, version = parsed
    floor = _FIRST_THINKING_BY_DEFAULT.get(family)
    return floor is not None and version >= floor


def output_budget(model: str | None, visible_tokens: int, thinking_headroom: int) -> int:
    """A `max_tokens` covering the visible answer *and* the thinking before it.

    `max_tokens` caps both, so on a thinking-by-default model the old
    visible-only numbers leave the answer to be cut off mid-sentence — or, on
    the short calls, never started.
    """
    if thinks_by_default(model):
        return visible_tokens + thinking_headroom
    return visible_tokens
