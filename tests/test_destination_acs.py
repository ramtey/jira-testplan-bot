"""SK-2627: an AC that says where a control takes the user must become a case
that asserts the destination.

The ticket's email CTAs both linked to the bare app root. QA signed off because
the copy matched; nobody followed the link. Three things had to line up for the
generator to miss it, and each is pinned here:

1. ADF -> text dropped list depth, so "Clicking this will ... land them
   directly inside the Forms file" arrived as a sibling of the button it
   describes, not its child.
2. The AC heading was "UX Acceptance Criteria" followed by a prose lead-in,
   so the extractor found zero ACs and the coverage check had nothing to
   compare against.
3. Nothing distinguished a case that proves the user CAN reach the file
   (sign in, open it) from one that proves the BUTTON lands them there.

The ADF below mirrors SK-2627's real description tree — the same nesting,
plain-paragraph heading and lead-in — with the email body text replaced.
"""

from __future__ import annotations

from src.app.adf_parser import extract_text_from_adf
from src.app.description_analyzer import (
    extract_ac_destination,
    extract_acceptance_criteria,
)
from src.app.llm_client import _format_ac_line
from src.app.models import TestPlan
from src.app.services.test_plan_generator import compute_ac_coverage

CHECK_IT_OUT_DEST = (
    "Clicking this will open up a new tab in their browser and land them "
    "directly inside the Forms file"
)
SIGN_UP_DEST = (
    "Clicking this will open up a new tab in their browser and land them in "
    "the account creation flow"
)


def _p(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]} if text else {"type": "paragraph"}


def _signature():
    """The real signature paragraph: hardBreak-separated lines and a URL, which
    the extractor sees as non-bullet lines in the middle of the AC block."""
    return {
        "type": "listItem",
        "content": [
            _p("Signature"),
            _ul({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Cheers!"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "SkySlope Forms Team"},
                        {"type": "hardBreak"},
                        {"type": "inlineCard", "attrs": {"url": "https://forms.skyslope.com"}},
                    ],
                }],
            }),
        ],
    }


def _li(text, *children):
    content = [_p(text)]
    if children:
        content.append(_ul(*children))
    return {"type": "listItem", "content": content}


def _li_paras(*paras):
    """A list item holding several paragraphs, like the email body bullet."""
    return {"type": "listItem", "content": [_p(t) for t in paras]}


def _ul(*items):
    return {
        "type": "bulletList",
        "content": [i if isinstance(i, dict) else _li(i) for i in items],
    }


SK2627_ADF = {
    "type": "doc",
    "content": [
        _ul("Forms user", "Non-Forms user", "Figma Link", "Loom walkthrough"),
        {"type": "rule"},
        _p("User Story "),
        _p("As an: Agent"),
        _p("I want: the person I share a file with to receive an email notification"),
        _p("So that: they know they can access my file now"),
        {"type": "rule"},
        _p("UX Acceptance Criteria"),
        _p("When I click “Save” in the share modal and I’ve added a new person, I see…"),
        _ul(
            "Success toast: Changes saved. Invitation sent",
            "An email is sent to the person’s email",
        ),
        _p(""),
        _p("Forms user email"),
        _ul(
            "Email header: A file has been shared with you",
            _li(
                "Body",
                _li_paras("Hi [FName],", "[Sender] has shared the [File Name] file with you."),
                _li("Primary button “Check It Out”", CHECK_IT_OUT_DEST),
            ),
            _signature(),
            "Existing email footer",
        ),
        _p(""),
        _p("Non-Forms user email"),
        _ul(
            "Email header: You’ve been invited to SkySlope Forms",
            _li(
                "Body",
                "Hi [FName],",
                _li(
                    "Primary button “Sign Up For Forms”",
                    SIGN_UP_DEST,
                    "Once they’ve onboarded, they should not see an empty state "
                    "on their dashboard, but this file that has been shared with them",
                ),
            ),
            _signature(),
            "Existing email footer",
        ),
        {"type": "rule"},
        _p("Accessibility"),
        _ul("Use all existing accessibility patterns today; not introducing anything new"),
    ],
}


def _acs() -> list[str]:
    return extract_acceptance_criteria(extract_text_from_adf(SK2627_ADF))


def _ac_containing(acs: list[str], needle: str) -> tuple[int, str]:
    hits = [(i, a) for i, a in enumerate(acs, 1) if needle in a]
    assert len(hits) == 1, f"expected exactly one AC containing {needle!r}, got {hits}"
    return hits[0]


# ─── what the generator receives ─────────────────────────────────────────────


def test_adf_text_keeps_the_destination_bullet_nested_under_its_button():
    text = extract_text_from_adf(SK2627_ADF)
    lines = text.splitlines()

    def indent_of_marker_before(needle: str) -> int:
        idx = next(i for i, line in enumerate(lines) if needle in line)
        marker = next(
            lines[j] for j in range(idx - 1, -1, -1) if lines[j].strip() == "•"
        )
        return len(marker) - len(marker.lstrip())

    button = indent_of_marker_before("Primary button “Check It Out”")
    child = indent_of_marker_before("land them directly inside the Forms file")
    assert child > button


def test_ux_acceptance_criteria_heading_with_lead_in_yields_acs():
    acs = _acs()
    assert acs, "SK-2627 extracted zero ACs — the coverage check is disabled"
    assert any("Success toast" in a for a in acs)
    assert any("Use all existing accessibility patterns" in a for a in acs)


def test_destination_bullets_stay_attached_to_their_button():
    acs = _acs()
    _, check = _ac_containing(acs, "land them directly inside the Forms file")
    _, sign_up = _ac_containing(acs, "land them in the account creation flow")
    assert "Check It Out" in check
    assert "Sign Up For Forms" in sign_up


# ─── destination detection ───────────────────────────────────────────────────


def test_destination_is_detected_for_both_buttons():
    acs = _acs()
    _, check = _ac_containing(acs, "Forms file")
    _, sign_up = _ac_containing(acs, "account creation flow")
    assert "directly inside the Forms file" in (extract_ac_destination(check) or "")
    assert "account creation flow" in (extract_ac_destination(sign_up) or "")


def test_common_navigation_phrasings_are_destinations():
    for text in (
        "Clicking the link takes them to the billing page",
        "Submitting redirects to /dashboard",
        "The CTA opens the file in a new tab",
        "Tapping Done navigates to the summary screen",
        "User lands on the onboarding checklist",
    ):
        assert extract_ac_destination(text), text


def test_non_navigation_acs_are_not_destinations():
    for text in (
        "Success toast: Changes saved. Invitation sent",
        "An email is sent to the person’s email",
        "Email header: A file has been shared with you",
        "Primary button “Check It Out”",
        "Use all existing accessibility patterns today; not introducing anything new",
    ):
        assert extract_ac_destination(text) is None, text


def test_prompt_marks_both_destination_acs():
    acs = _acs()
    for needle in ("Forms file", "account creation flow"):
        i, text = _ac_containing(acs, needle)
        line = _format_ac_line(f"SK-2627-AC{i}", text)
        assert "DESTINATION AC" in line
        assert "assertion_type" in line


# ─── coverage: access does not satisfy a destination ─────────────────────────


def _plan(cases: list[dict]) -> TestPlan:
    return TestPlan(happy_path=cases, edge_cases=[], regression_checklist=[])


def _case(title: str, covers: list[str], assertion_type: str | None) -> dict:
    case = {
        "title": title,
        "priority": "high",
        "steps": ["Open the invitation email", "Click the primary button"],
        "expected": "…",
        "covers_acs": covers,
    }
    if assertion_type:
        case["assertion_type"] = assertion_type
    return case


def _destination_ids() -> list[str]:
    acs = _acs()
    return [
        f"SK-2627-AC{_ac_containing(acs, n)[0]}"
        for n in ("Forms file", "account creation flow")
    ]


def _coverage(cases):
    return compute_ac_coverage(
        _plan(cases), [{"ticket_key": "SK-2627", "acceptance_criteria": _acs()}]
    )["tickets"]["SK-2627"]


def test_an_access_case_does_not_cover_a_destination_ac():
    """What QA actually did: signed in as the recipient and opened the file."""
    ids = _destination_ids()
    cov = _coverage([_case("Recipient can open the shared file", ids, "access")])
    flagged = {e["id"] for e in cov["under_covered"]}
    assert flagged == set(ids)
    for entry in cov["under_covered"]:
        assert entry["missing_destination"]


def test_a_case_tagged_destination_covers_it():
    ids = _destination_ids()
    cov = _coverage(
        [
            _case("Check It Out lands inside the shared file", [ids[0]], "destination"),
            _case("Sign Up For Forms lands in account creation", [ids[1]], "destination"),
        ]
    )
    assert not {e["id"] for e in cov["under_covered"]} & set(ids)
