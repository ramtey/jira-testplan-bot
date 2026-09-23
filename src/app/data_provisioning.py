"""Whether a case that needs unusual data says how anyone is to get it.

A case can need an input shape ordinary data does not carry: a malformed value
in a field the code parses as a number, a field populated while its sibling is
empty, a formatting variant. The plan's job is not only to name the shape but
to say which *kind* of ask it is, because they are not the same ask:

* a real record — someone searches the data for one (a fixture ask);
* a stub — someone mocks the payload (an Engineering ask);
* unreachable — no record can deliver the shape to the code under test,
  because a selector upstream discards the records that carry it.

**Why this exists (SK-2325, 2026-09-23).** Three cases asked Engineering to
"supply a property" for shapes live provider data cannot produce. Establishing
that took mining ~86 real parcels: malformed numeric values do exist, but only
on ~2010-era listings that the latest-list-date selector systematically
excludes, so none can reach an import; comma-formatted acreage appears in no
parcel; and the assessment supplies lot square footage and acreage together or
not at all (85 both, 0 mixed). Written as a data hunt, each one sends QA looking
for something that cannot exist and makes the request read as a failure to look
hard enough.

**Scope.** This is a shape check with no data access, so it can only catch a
case that *defers the work to someone else* and leaves the route unstated —
"ask Engineering for a property that…". It deliberately does NOT flag the
ordinary "use a property that has monthly HOA dues": a regex cannot tell a
routine pick from an exotic shape, and a check that fires on every case teaches
the reader to skim past it. Distinguishing those is the generator's job, under
the DATA SHAPE rule in the system prompt; this pass catches the residue.

Like ``citation_integrity``, it flags and never moves a case: section counts are
the progress-key fingerprint, and a case whose data ask is vague is still a case
QA can read and act on once the ask is fixed.
"""

from __future__ import annotations

import re

#: Deferring the data work to another team or person. This is the signature of
#: the failure — not "find a record", which is ordinary, but "have someone else
#: produce one", which is an ask the reader cannot evaluate without the route.
_TEAMS = r"""(?:engineering|eng|dev(?:eloper)?s?|dev\s+team|development|backend|
               data\s+team|ops|devops|product)"""

_PRODUCE = r"""(?:suppl(?:y|ies|ied)|provid(?:e|es|ed)|creat(?:e|es|ed)|
                seed(?:s|ed)?|set\s?up|sets?\s?up|generat(?:e|es|ed)|
                prepar(?:e|es|ed)|craft(?:s|ed)?)"""

_DEFERRED_ASK_RE = re.compile(
    rf"""
    # "ask Engineering …", "request from the dev team …"
    (?:\b(?:ask|request|have|get|coordinate\s+with)\s+
       (?:from\s+|with\s+)?(?:the\s+)?{_TEAMS}\b)
    |
    # "Engineering to supply …", "a developer has seeded …"
    (?:\b{_TEAMS}\b[^.\n]{{0,24}}?\b{_PRODUCE}\b)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PROVISIONING_ROUTES = ("real_record", "real_record_unconfirmed", "stub", "unreachable")

#: Surfaces a human drives against a running environment. An `unreachable`
#: shape cannot be produced there by definition.
_LIVE_SURFACES = ("web_ui", "mobile")

# Same sections as every other grounding guard: `regression_checklist` is bare
# strings and carries no metadata to inspect.
_CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "needs_spec_cases")


def _case_text(case: dict) -> str:
    """Steps, preconditions and test data as one blob — the fields where a
    request for data is written."""
    parts: list[str] = []
    for key in ("preconditions", "test_data", "expected"):
        value = case.get(key)
        if isinstance(value, str):
            parts.append(value)
    steps = case.get("steps")
    if isinstance(steps, list):
        parts.extend(s for s in steps if isinstance(s, str))
    return "\n".join(parts)


def inspect_provisioning(case: dict) -> list[str]:
    """Reasons this case's data ask cannot be acted on as written.

    Empty list means nothing about the case's *shape* contradicts it — not that
    the shape it asks for exists, which only real data can answer.
    """
    if not isinstance(case, dict):
        return []

    shape = case.get("data_shape")
    reasons: list[str] = []

    if not isinstance(shape, dict) or not shape:
        if _DEFERRED_ASK_RE.search(_case_text(case)):
            reasons.append(
                "asks someone else to supply data but names no provisioning "
                "route, so the reader cannot tell whether a real record can "
                "produce this shape or whether it needs a stubbed payload"
            )
        return reasons

    route = shape.get("provisioning")
    if route not in _PROVISIONING_ROUTES:
        reasons.append(
            f"declares a data shape with no usable provisioning route "
            f"({route!r}); expected one of {', '.join(_PROVISIONING_ROUTES)}"
        )
    elif route == "real_record_unconfirmed" and not str(shape.get("obtain") or "").strip():
        reasons.append(
            "says a real record is unconfirmed but gives no way to look for "
            "one and no fallback for when none turns up"
        )
    elif route == "unreachable" and case.get("surface") in _LIVE_SURFACES:
        reasons.append(
            "declares the shape unreachable in live data yet is labelled a "
            f"{case.get('surface')} case — an unreachable shape cannot be "
            "produced through the running app, so this belongs to Engineering "
            "with a stubbed payload"
        )

    if not str(shape.get("shape") or "").strip():
        reasons.append("declares a data shape without saying what the shape is")

    return reasons


def flag_unactionable_data_asks(test_plan) -> list[dict]:
    """Mark every case whose data ask cannot be acted on, in place.

    Sets ``data_ask_unactionable`` and a human-readable
    ``data_ask_unactionable_reason``. Returns the cases it marked. Nothing moves
    between sections and no other field is touched.
    """
    flagged: list[dict] = []
    for section in _CASE_SECTIONS:
        for case in getattr(test_plan, section, None) or []:
            if not isinstance(case, dict):
                continue
            reasons = inspect_provisioning(case)
            if not reasons:
                continue
            case["data_ask_unactionable"] = True
            case["data_ask_unactionable_reason"] = "; ".join(reasons)
            flagged.append(case)
    return flagged
