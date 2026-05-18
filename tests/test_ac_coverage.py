"""Tests for acceptance-criteria extraction and multi-ticket coverage validation."""

from src.app.adf_parser import extract_text_from_adf
from src.app.description_analyzer import extract_acceptance_criteria
from src.app.main import _compute_ac_coverage
from src.app.models import TestPlan as _TestPlan


def _adf_doc(content):
    return {"type": "doc", "content": content}


def _heading(text, level=2):
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}


def _para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _bullets(*texts_or_nested):
    """Make a bulletList. Each arg is either a string (simple bullet) or a
    tuple (text, [nested_bullets...]) to express sub-bullets."""
    items = []
    for arg in texts_or_nested:
        if isinstance(arg, tuple):
            text, nested = arg
            items.append({
                "type": "listItem",
                "content": [_para(text), _bullets(*nested)],
            })
        else:
            items.append({"type": "listItem", "content": [_para(arg)]})
    return {"type": "bulletList", "content": items}


# ─── extract_acceptance_criteria ──────────────────────────────────────────────


def test_extract_returns_empty_when_no_description():
    assert extract_acceptance_criteria(None) == []
    assert extract_acceptance_criteria("") == []


def test_extract_returns_empty_when_no_ac_section():
    text = """## User Story
As an agent I want X.

## Implementation Notes
Mirror seller net sheet."""
    assert extract_acceptance_criteria(text) == []


def test_extract_pulls_bullets_after_h2_heading():
    text = """## User Story
Blah.

## Acceptance Criteria
* First criterion
* Second criterion
* Third criterion

## Notes
Other stuff."""
    acs = extract_acceptance_criteria(text)
    assert acs == ["First criterion", "Second criterion", "Third criterion"]


def test_extract_handles_numbered_lists():
    text = """**Acceptance Criteria:**
1. Feature works on login
2. Feature works on signup

## Notes"""
    assert extract_acceptance_criteria(text) == [
        "Feature works on login",
        "Feature works on signup",
    ]


def test_extract_handles_dash_bullets_and_bold_heading():
    text = """**Acceptance Criteria**
- Login works
- Signup works
- Logout works

**Other**"""
    assert extract_acceptance_criteria(text) == [
        "Login works",
        "Signup works",
        "Logout works",
    ]


def test_extract_pulls_indented_sub_bullets_as_siblings():
    """Indented sub-bullets become their own AC entries; QA tests each separately."""
    text = """## Acceptance Criteria

* Data should match the mobile app

    * All loan, down payment, interest fields present

* Calculation logic verified

## Notes"""
    acs = extract_acceptance_criteria(text)
    assert "Data should match the mobile app" in acs
    assert "All loan, down payment, interest fields present" in acs
    assert "Calculation logic verified" in acs


def test_extract_skips_stale_og_ac_sections():
    text = """## OG AC
* old button should be red
* old button should be blue

## Acceptance Criteria
* button should be green
* button should be 200px wide

## Notes"""
    acs = extract_acceptance_criteria(text)
    assert acs == ["button should be green", "button should be 200px wide"]


def test_extract_stops_at_next_section_heading():
    text = """## Acceptance Criteria
* First
* Second

## Implementation Notes
* Do not test this bullet"""
    assert extract_acceptance_criteria(text) == ["First", "Second"]


def test_extract_ignores_smartlink_url_between_ac_and_next_section():
    """Real-world regression: Jira ADF parser drops `##` markers, so the section
    after the AC list arrives as a plain title-case line. A Figma smartlink URL
    sits in between. The old extractor captured both as ACs."""
    text = """User Story

As an agent, I want a calculator.

Acceptance Criteria

* Banner appears on buyer files
* Clicking button opens calculator
* New badge shows until engagement
* Forms data passes over

https://www.figma.com/proto/abc/Buyer-Home-Estimate

Implementation Notes

Should mirror seller net sheet."""
    acs = extract_acceptance_criteria(text)
    assert acs == [
        "Banner appears on buyer files",
        "Clicking button opens calculator",
        "New badge shows until engagement",
        "Forms data passes over",
    ]


def test_extract_stops_at_unmarked_section_heading():
    """ADF-stripped section titles (no `##`, no `**`) must still terminate the AC block."""
    text = """Acceptance Criteria

- Login works
- Logout works

Implementation Notes

Do this and that."""
    assert extract_acceptance_criteria(text) == ["Login works", "Logout works"]


def test_extract_skips_url_only_lines_inside_ac_block():
    """A bare URL line between bullets is not an AC and not a section heading —
    it should be ignored, not captured as an AC."""
    text = """Acceptance Criteria

- First AC
https://example.com/figma
- Second AC"""
    assert extract_acceptance_criteria(text) == ["First AC", "Second AC"]


def test_extract_works_on_real_adf_output():
    """End-to-end: build an ADF doc the way Jira actually returns it, run it
    through extract_text_from_adf, then through the extractor.

    Regression: the previous extractor required bullet + text on the same line,
    but ADF emits the bullet on its own line and the inner paragraph on the
    next line. This caused production to return 0 ACs even though unit tests
    that used markdown-shaped input passed."""
    adf = _adf_doc([
        _heading("User Story"),
        _para("As an agent, I want X."),
        _heading("Acceptance Criteria"),
        _bullets(
            "Banner appears on buyer files",
            "Clicking button opens calculator",
            '"New" badge until engagement',
            "Forms info pre-fills",
        ),
        _heading("Implementation Notes"),
        _para("Mirror seller net sheet."),
    ])
    text = extract_text_from_adf(adf)
    acs = extract_acceptance_criteria(text)
    assert acs == [
        "Banner appears on buyer files",
        "Clicking button opens calculator",
        '"New" badge until engagement',
        "Forms info pre-fills",
    ]


def test_extract_handles_adf_nested_sub_bullets():
    """SK-2141 pattern: indented sub-bullet inside a parent listItem. ADF
    serializes both at the same level — we want both pulled out as ACs."""
    adf = _adf_doc([
        _heading("Acceptance Criteria"),
        _bullets(
            ("Data should match mobile app", ["All fields present"]),
            "Calculation logic verified",
            ("Save shows toast", ["Save to forms file works"]),
            "Preview opens PDF",
        ),
        _heading("Implementation Notes"),
    ])
    acs = extract_acceptance_criteria(extract_text_from_adf(adf))
    assert "Data should match mobile app" in acs
    assert "All fields present" in acs
    assert "Save to forms file works" in acs
    assert "Preview opens PDF" in acs
    assert len(acs) == 6  # 4 top-level + 2 sub-bullets


def test_extract_skips_smartlink_paragraph_between_ac_and_next_heading_in_adf():
    """A bare URL paragraph (inlineCard/smartlink) sits between the bullet
    list and the next heading. Must not become an AC, must not abort early."""
    adf = _adf_doc([
        _heading("Acceptance Criteria"),
        _bullets("First", "Second"),
        {"type": "paragraph", "content": [
            {"type": "inlineCard", "attrs": {"url": "https://www.figma.com/proto/abc"}}
        ]},
        _heading("Implementation Notes"),
    ])
    assert extract_acceptance_criteria(extract_text_from_adf(adf)) == ["First", "Second"]


def test_extract_deduplicates_repeated_bullets():
    text = """## Acceptance Criteria
* Same thing
* Same thing
* Other thing"""
    assert extract_acceptance_criteria(text) == ["Same thing", "Other thing"]


# ─── _compute_ac_coverage ─────────────────────────────────────────────────────


def test_coverage_flags_uncovered_acs_across_tickets():
    tickets = [
        {"ticket_key": "SK-1", "acceptance_criteria": ["a", "b", "c"]},
        {"ticket_key": "SK-2", "acceptance_criteria": ["x", "y"]},
    ]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-1-AC1", "SK-2-AC2"]}],
        edge_cases=[{"title": "e", "covers_acs": ["SK-1-AC2"]}],
        integration_tests=[],
        regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 2
    assert cov["tickets"]["SK-1"]["covered"] == ["SK-1-AC1", "SK-1-AC2"]
    assert [u["id"] for u in cov["tickets"]["SK-1"]["uncovered"]] == ["SK-1-AC3"]
    assert [u["id"] for u in cov["tickets"]["SK-2"]["uncovered"]] == ["SK-2-AC1"]


def test_coverage_handles_missing_covers_acs_field():
    """Cases without `covers_acs` must not crash; they just don't count."""
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a"]}]
    plan = _TestPlan(
        happy_path=[{"title": "t"}],
        edge_cases=[],
        integration_tests=None,
        regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 1
    assert cov["tickets"]["SK-1"]["covered"] == []
    assert cov["tickets"]["SK-1"]["total"] == 1


def test_coverage_reports_full_coverage_as_empty_uncovered():
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a", "b"]}]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-1-AC1", "SK-1-AC2"]}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 0
    assert cov["tickets"]["SK-1"]["uncovered"] == []


def test_coverage_returns_empty_tickets_when_no_acs_supplied():
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": []}]
    plan = _TestPlan(
        happy_path=[], edge_cases=[], integration_tests=[], regression_checklist=[]
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 0
    assert cov["tickets"]["SK-1"] == {
        "covered": [],
        "uncovered": [],
        "superseded": [],
        "total": 0,
    }


def test_coverage_drops_invalid_ids_from_cases_and_reports_them():
    """LLM invents AC9 when only 2 ACs exist — the fake ID must be stripped
    from the case so the UI doesn't show a bogus tag, and surfaced in
    `invalid_ids` so the user knows the model hallucinated."""
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a", "b"]}]
    case = {"title": "t", "covers_acs": ["SK-1-AC1", "SK-1-AC9", "SK-2-AC1"]}
    plan = _TestPlan(
        happy_path=[case], edge_cases=[], integration_tests=[], regression_checklist=[]
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert case["covers_acs"] == ["SK-1-AC1"]
    assert cov["invalid_ids"] == ["SK-1-AC9", "SK-2-AC1"]
    assert cov["tickets"]["SK-1"]["covered"] == ["SK-1-AC1"]
    assert [u["id"] for u in cov["tickets"]["SK-1"]["uncovered"]] == ["SK-1-AC2"]


def test_coverage_handles_empty_invalid_ids_when_all_tags_valid():
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a"]}]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-1-AC1"]}],
        edge_cases=[], integration_tests=[], regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["invalid_ids"] == []


def test_coverage_strips_whitespace_and_keeps_unique_invalid_ids():
    """Whitespace-padded IDs must still match. Duplicates of an invalid ID
    appear once in the report."""
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a"]}]
    plan = _TestPlan(
        happy_path=[
            {"title": "t1", "covers_acs": [" SK-1-AC1 ", "SK-1-AC99"]},
            {"title": "t2", "covers_acs": ["SK-1-AC99", "garbage"]},
        ],
        edge_cases=[], integration_tests=[], regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["tickets"]["SK-1"]["covered"] == ["SK-1-AC1"]
    assert cov["invalid_ids"] == ["SK-1-AC99", "garbage"]


def test_coverage_handles_non_list_covers_acs_gracefully():
    """A pathologically malformed `covers_acs` (string instead of list) should
    not crash; just be ignored."""
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a"]}]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": "SK-1-AC1"}],  # wrong type
        edge_cases=[], integration_tests=[], regression_checklist=[],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 1
    assert cov["invalid_ids"] == []


# ─── superseded_acs (newer ticket wins on conflicts) ──────────────────────────


def test_supersede_excludes_loser_from_uncovered_and_surfaces_pair():
    """When the LLM marks SK-2138-AC3 as superseded by SK-2194-AC1, the older
    AC must be excluded from "uncovered" (it's overridden, not missed) and
    surfaced in the top-level `superseded_acs` list."""
    tickets = [
        {"ticket_key": "SK-2138", "acceptance_criteria": ["modal stays open"]},
        {"ticket_key": "SK-2194", "acceptance_criteria": ["modal closes after Save"]},
    ]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-2194-AC1"]}],
        edge_cases=[], integration_tests=[], regression_checklist=[],
        superseded_acs=[
            {
                "loser_id": "SK-2138-AC1",
                "winner_id": "SK-2194-AC1",
                "reason": "Modal close behaviour reversed in SK-2194.",
            }
        ],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["uncovered_total"] == 0
    assert cov["tickets"]["SK-2138"]["uncovered"] == []
    assert cov["tickets"]["SK-2138"]["superseded"] == [
        {"id": "SK-2138-AC1", "text": "modal stays open", "winner_id": "SK-2194-AC1"}
    ]
    # `total` excludes the superseded AC so X/Y reflects what was actually expected.
    assert cov["tickets"]["SK-2138"]["total"] == 0
    assert cov["superseded_acs"] == [{
        "loser_id": "SK-2138-AC1",
        "loser_text": "modal stays open",
        "loser_ticket": "SK-2138",
        "winner_id": "SK-2194-AC1",
        "winner_text": "modal closes after Save",
        "winner_ticket": "SK-2194",
        "reason": "Modal close behaviour reversed in SK-2194.",
    }]


def test_supersede_strips_loser_id_from_case_covers_acs():
    """If the LLM tags a test case with the loser ID, it must be stripped — the
    newer AC is the source of truth and the test verifies that one only."""
    tickets = [
        {"ticket_key": "SK-2138", "acceptance_criteria": ["old behaviour"]},
        {"ticket_key": "SK-2194", "acceptance_criteria": ["new behaviour"]},
    ]
    case = {"title": "t", "covers_acs": ["SK-2138-AC1", "SK-2194-AC1"]}
    plan = _TestPlan(
        happy_path=[case], edge_cases=[], integration_tests=[], regression_checklist=[],
        superseded_acs=[
            {"loser_id": "SK-2138-AC1", "winner_id": "SK-2194-AC1", "reason": "x"}
        ],
    )
    _compute_ac_coverage(plan, tickets)
    assert case["covers_acs"] == ["SK-2194-AC1"]


def test_supersede_rejects_backwards_recency():
    """If the LLM gets recency backwards (winner is older than loser), the
    entry must be discarded — better to show the AC as uncovered than to
    silently honour a wrong override."""
    tickets = [
        {"ticket_key": "SK-2138", "acceptance_criteria": ["a"]},
        {"ticket_key": "SK-2194", "acceptance_criteria": ["b"]},
    ]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-2138-AC1"]}],
        edge_cases=[], integration_tests=[], regression_checklist=[],
        superseded_acs=[
            # SK-2194 is newer but the LLM marked it as the loser — reject.
            {"loser_id": "SK-2194-AC1", "winner_id": "SK-2138-AC1", "reason": "x"}
        ],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["superseded_acs"] == []
    assert [u["id"] for u in cov["tickets"]["SK-2194"]["uncovered"]] == ["SK-2194-AC1"]


def test_supersede_rejects_invalid_ac_ids():
    """A supersede entry referencing an AC ID that doesn't exist must be dropped."""
    tickets = [
        {"ticket_key": "SK-1", "acceptance_criteria": ["a"]},
        {"ticket_key": "SK-2", "acceptance_criteria": ["b"]},
    ]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-2-AC1", "SK-1-AC1"]}],
        edge_cases=[], integration_tests=[], regression_checklist=[],
        superseded_acs=[
            {"loser_id": "SK-1-AC9", "winner_id": "SK-2-AC1", "reason": "x"},  # AC9 doesn't exist
            {"loser_id": "SK-1-AC1", "winner_id": "SK-9-AC1", "reason": "x"},  # SK-9 doesn't exist
        ],
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["superseded_acs"] == []
    # SK-1-AC1 is still tagged as covered since neither supersede applied.
    assert cov["tickets"]["SK-1"]["covered"] == ["SK-1-AC1"]


def test_supersede_absent_field_keeps_existing_behaviour():
    """Single-ticket plans (and any plan without superseded_acs) must behave
    exactly as before — the field is optional."""
    tickets = [{"ticket_key": "SK-1", "acceptance_criteria": ["a", "b"]}]
    plan = _TestPlan(
        happy_path=[{"title": "t", "covers_acs": ["SK-1-AC1"]}],
        edge_cases=[], integration_tests=[], regression_checklist=[],
        # superseded_acs defaults to None
    )
    cov = _compute_ac_coverage(plan, tickets)
    assert cov["superseded_acs"] == []
    assert cov["tickets"]["SK-1"]["superseded"] == []
    assert cov["tickets"]["SK-1"]["total"] == 2
    assert [u["id"] for u in cov["tickets"]["SK-1"]["uncovered"]] == ["SK-1-AC2"]
