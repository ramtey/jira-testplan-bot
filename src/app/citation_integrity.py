"""Whether a case's `expected_source` can be what the case claims it is.

`expected_verified: true` is a promise to whoever reads the plan. The schema
says so in as many words: setting it "with a file path you did not read is the
most damaging thing you can emit: it tells the reviewer to stop checking."
The promise is only worth anything if the citation attached to it could
plausibly be a reading of the implementation.

Measured across the 34 replayed plans on 2026-09-16, **101 of 362 verified
citations could not be** — 50 cited line 1, 50 named a file with no line at
all, and 5 named several sources where the schema asks for "the single
primary `<file>:<line>`". None of that was visible to a tester, who saw
"Expected verified against: …" and a path.

This module is a shape check and nothing more. It has no network and no
repository access, so it can say a citation is *unconfirmed*, never that it is
wrong: line 1 of a six-line config file is a legitimate reading, and only
opening the file at the right commit can tell those apart. That check is the
groundedness scorer's job. Saying "unconfirmed" where we mean "unchecked" is
the whole discipline here — the same rule the code-grounding critic states,
that only "searched and found nothing" is evidence.

**It deliberately does not touch `expected_verified`.** Flipping it to False
would meet `_is_ungrounded`'s second condition, and on the 40 measured cases
that also carry `needs_manual_verification` that quarantines them out of their
section into `needs_spec_cases`. Section counts are the progress-key
fingerprint, so that would orphan QA's existing marks on every plan already in
flight — a large, silent cost for a check that by construction cannot prove
anything is wrong. Flag, keep the case where it is, and let the reader decide:
the same shape as `run_regression_grounding_critic`, which narrows a line's
text and never removes the line.
"""

from __future__ import annotations

import re

#: `<path>:<line>` or `<path>:<line>-<line>`, the shape the schema asks for.
_LOCATOR_RE = re.compile(r"^(?P<path>\S+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")

#: Separators that mean the model cited more than one place.
_SEPARATORS = re.compile(r"\s*(?:,|;|\band\b)\s*", re.IGNORECASE)

#: A fragment naming a file rather than prose — a slash or a dotted extension.
_LOOKS_LIKE_PATH = re.compile(r"[/\\]|\.\w{1,5}(?::|$|\s)")

# Sections whose cases carry grounding metadata at all. `regression_checklist`
# is bare strings, so there is nothing here to inspect — the same shape
# asymmetry that exempts it from every other grounding guard.
_CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "needs_spec_cases")


#: An aside is a parenthetical with whitespace before it. A parenthesis welded
#: to the path is part of the path — Expo route groups (`app/(calculation)/x.tsx`)
#: are all over this codebase, and stripping those left a fragment with no
#: colon, which the checker then reported as "cites a file but no line number".
#: A false flag inside the guard against false flags.
_ASIDE_RE = re.compile(r"(?:^|\s)\([^)]*\)")


def _without_parentheticals(text: str) -> str:
    """Drop `(…)` asides, which routinely contain commas and the word "and"."""
    return _ASIDE_RE.sub(" ", text)


def inspect_citation(expected_source: str | None) -> list[str]:
    """Reasons this citation cannot be read as a single verified location.

    Empty list means the citation is well-formed — which is not the same as
    correct, only that nothing about its *shape* contradicts the claim.
    """
    raw = (expected_source or "").strip()
    if not raw:
        return ["claims a verified expected result but cites no source"]

    core = _without_parentheticals(raw).strip()
    fragments = [f for f in _SEPARATORS.split(core) if f.strip()]
    paths = [f for f in fragments if _LOOKS_LIKE_PATH.search(f)]

    reasons: list[str] = []
    if len(paths) > 1:
        reasons.append(
            f"cites {len(paths)} sources; a verified expected result rests on "
            f"the single primary <file>:<line>"
        )

    primary = (paths[0] if paths else fragments[0] if fragments else core).strip()
    match = _LOCATOR_RE.match(primary)
    if not match:
        reasons.append("cites a file but no line number")
        return reasons

    if int(match.group("start")) == 1:
        # Not proof of anything: some files are short, and some behaviour does
        # live at the top. But line 1 is the import region in most source
        # files, and it is what a fabricated citation defaults to.
        reasons.append(
            "cites line 1, which in most source files is the import region "
            "rather than the behaviour under test"
        )
    return reasons


def flag_unconfirmed_citations(test_plan) -> list[dict]:
    """Mark every verified case whose citation cannot be what it claims.

    Sets ``expected_source_unconfirmed`` and a human-readable
    ``expected_source_unconfirmed_reason`` on the case, in place. Returns the
    cases it marked. Nothing moves between sections, and ``expected_verified``
    is left exactly as the generator set it.
    """
    flagged: list[dict] = []
    for section in _CASE_SECTIONS:
        for case in getattr(test_plan, section, None) or []:
            if not isinstance(case, dict):
                continue
            if case.get("expected_verified") is not True:
                # An honest "I assumed this" needs no citation and gets no
                # complaint. Only the promise is audited.
                continue
            reasons = inspect_citation(case.get("expected_source"))
            if not reasons:
                continue
            case["expected_source_unconfirmed"] = True
            case["expected_source_unconfirmed_reason"] = "; ".join(reasons)
            flagged.append(case)
    return flagged
