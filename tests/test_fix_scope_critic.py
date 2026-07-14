"""Tests for the post-generation fix-scope critic — SK-2373-style reporter drift.

The critic reads each test case (title, steps, expected, cited ACs)
alongside a snapshot of what the merged PR actually did (title, body,
commits, files, diffs) and returns a supported/unsupported verdict.
Unsupported cases are badged in place with
``needs_manual_verification=True`` and gain a ``grounding_warnings`` entry
so the frontend renders them under the existing "Ungrounded UI ref" badge.

Regression coverage is anchored on the real-world SK-2373 example: an
edge-case titled "Verify Freddie Mac default rate is NOT auto-applied…"
cited against real ACs, but the merged PR body explicitly says "Tooltip
copy only (no change to the displayed balance or any FRED behavior)".
The critic must catch that reporter-drift.
"""

from src.app.fix_scope_critic import (
    apply_scope_verdicts,
    build_case_scope_inputs,
    build_fix_scope_summary,
    build_scope_critic_user_message,
    parse_scope_verdicts,
)
from src.app.models import TestPlan


# ─── fixtures ─────────────────────────────────────────────────────────────────


_SK2373_PR_BODY = (
    "Refresh the Loan Information tooltip to say where the estimated balance "
    "came from and add a disclaimer. Tooltip copy only (no change to the "
    "displayed balance or any FRED behavior).\n\n"
    "Deferred to a follow-up with Megan: whether balance-only should display "
    "the raw recorded balance vs a labeled estimate, and whether to retire "
    "the FRED fallback."
)


def _sk2373_plan() -> TestPlan:
    """Fixture based on the real SK-2373 v1 plan that triggered this bug.

    - happy_path[0] (HP-0): tooltip source disclosure — matches what the PR changed.
    - happy_path[1] (HP-1): partial-data disclaimer — matches the copy the PR added.
    - edge_cases[0] (EC-0): "Freddie Mac default rate is NOT auto-applied" —
      derived from the reporter's diagnostic aside; the PR did NOT change FRED
      behaviour. This is the case the critic must catch.
    - edge_cases[1] (EC-1): tooltip math consistency — matches the exact bug the
      PR targeted (removing fabricated amortization narrative from the copy).
    """
    return TestPlan(
        happy_path=[
            {
                "title": "Tooltip displays FNF data source when all loan info found",
                "steps": [
                    "Enter an address whose loan record has both principal and rate in FNF.",
                    "Open the Loan Information tooltip.",
                    "Confirm the tooltip cites FNF as the data source.",
                ],
                "expected": "Tooltip includes 'Real-time data successfully imported from Fidelity National Financial.'",
                "priority": "high",
                "covers_acs": ["SK-2373-AC1", "SK-2373-AC3"],
            },
            {
                "title": "Tooltip explains partial data scenario when only loan balance found",
                "steps": [
                    "Enter an address whose FNF record has balance only (no rate).",
                    "Open the Loan Information tooltip.",
                    "Confirm the tooltip discloses that the balance is an estimate.",
                ],
                "expected": "Tooltip contains the balance-only disclaimer copy.",
                "priority": "high",
                "covers_acs": ["SK-2373-AC1", "SK-2373-AC3", "SK-2373-AC6"],
            },
        ],
        edge_cases=[
            {
                "title": "Verify Freddie Mac default rate is NOT auto-applied when only loan balance is found",
                "category": "error_handling",
                "steps": [
                    "Enter an address whose FNF record has balance only.",
                    "Open the loan calculation surface.",
                    "Confirm the 3.11% FRED default is NOT auto-applied to the interest rate field.",
                ],
                "expected": "Interest rate remains empty or user-editable; no 3.11% default is written.",
                "priority": "critical",
                "covers_acs": ["SK-2373-AC1", "SK-2373-AC3", "SK-2373-AC6"],
            },
            {
                "title": "Tooltip math consistency — displayed balance matches calculation shown",
                "category": "boundary",
                "steps": [
                    "Enter an address whose FNF loan info is complete.",
                    "Open the Loan Information tooltip.",
                    "Confirm the tooltip no longer narrates a fabricated amortization breakdown.",
                ],
                "expected": "Tooltip copy matches the balance shown on the field; no invented math.",
                "priority": "high",
                "covers_acs": ["SK-2373-AC1"],
            },
        ],
        regression_checklist=[],
        integration_tests=[],
    )


def _sk2373_dev_infos() -> list[dict]:
    """Merged PR context — the shape ``build_fix_scope_summary`` consumes."""
    return [
        {
            "ticket_key": "SK-2373",
            "development_info": {
                "pull_requests": [
                    {
                        "title": "Refresh Loan Information tooltip copy",
                        "status": "MERGED",
                        "github_description": _SK2373_PR_BODY,
                        "files_changed": [
                            {
                                "filename": "apps/web/src/components/LoanInfoTooltip.tsx",
                                "status": "modified",
                                "additions": 18,
                                "deletions": 6,
                                "patch": (
                                    "@@ -12,7 +12,7 @@\n"
                                    "-      <p>Amortization derived from principal × rate.</p>\n"
                                    "+      <p>Real-time data successfully imported from Fidelity National Financial.</p>\n"
                                ),
                            },
                            {
                                "filename": "apps/web/src/components/__tests__/LoanInfoTooltip.test.tsx",
                                "status": "added",
                                "additions": 42,
                                "deletions": 0,
                                "patch": (
                                    "@@ -0,0 +1,42 @@\n"
                                    "+describe('tooltip copy', () => {\n"
                                    "+  it('discloses FNF source when all loan info is present', () => {})\n"
                                    "+  it('discloses partial-data disclaimer when only balance is present', () => {})\n"
                                    "+})\n"
                                ),
                            },
                        ],
                    }
                ],
                "commits": [
                    {"message": "Refresh Loan Information tooltip copy (no FRED behaviour change)"}
                ],
            },
        }
    ]


# ─── build_fix_scope_summary ──────────────────────────────────────────────────


def test_build_fix_scope_summary_surfaces_pr_body_verbatim():
    """The 'no change to FRED behavior' phrasing is the whole signal — it
    MUST appear in the summary the critic sees."""
    summary = build_fix_scope_summary(_sk2373_dev_infos())
    assert "SK-2373" in summary
    assert "Tooltip copy only" in summary
    assert "no change to the displayed balance or any FRED behavior" in summary
    # Deferred-to-follow-up signal also matters.
    assert "Deferred to a follow-up" in summary


def test_build_fix_scope_summary_marks_test_files():
    """Test files are labeled so the critic can distinguish 'PR added tests
    for this behaviour' from 'PR added runtime code for this behaviour'."""
    summary = build_fix_scope_summary(_sk2373_dev_infos())
    # The runtime component line is unmarked, the test file line carries [TEST].
    for line in summary.splitlines():
        if "LoanInfoTooltip.test.tsx" in line:
            assert "[TEST]" in line
            break
    else:
        raise AssertionError("Expected a line for LoanInfoTooltip.test.tsx in summary")


def test_build_fix_scope_summary_includes_diff_patches():
    summary = build_fix_scope_summary(_sk2373_dev_infos())
    assert "Real-time data successfully imported from Fidelity National Financial." in summary
    # Verify the diff hunk marker is preserved.
    assert "@@ -12,7 +12,7 @@" in summary


def test_build_fix_scope_summary_returns_empty_when_no_signal():
    """No PR body, no commits, no files → nothing for the critic to reason
    against; caller should skip the critic."""
    assert build_fix_scope_summary([]) == ""
    assert build_fix_scope_summary([{"ticket_key": "SK-1", "development_info": None}]) == ""
    assert (
        build_fix_scope_summary([{"ticket_key": "SK-1", "development_info": {}}])
        == ""
    )
    # A PR with only a title is also empty of scope signal.
    empty_pr = [
        {
            "ticket_key": "SK-1",
            "development_info": {"pull_requests": [{"title": "just a title"}]},
        }
    ]
    assert build_fix_scope_summary(empty_pr) == ""


def test_build_fix_scope_summary_truncates_long_pr_body():
    long_body = "x" * 5000
    summary = build_fix_scope_summary([
        {
            "ticket_key": "SK-1",
            "development_info": {
                "pull_requests": [{"title": "p", "github_description": long_body}]
            },
        }
    ])
    # Body is capped well below the raw length.
    body_line_len = max(
        (len(line) for line in summary.splitlines() if line.startswith("x")),
        default=0,
    )
    assert 0 < body_line_len <= 2001  # _MAX_PR_BODY_CHARS + trailing ellipsis


# ─── build_case_scope_inputs ──────────────────────────────────────────────────


def test_build_case_scope_inputs_includes_every_cited_case():
    """Every case in the SK-2373 plan cites at least one AC and none are
    pre-badged, so all four should be handed to the critic."""
    plan = _sk2373_plan()
    inputs = build_case_scope_inputs(plan)
    ids = {c["case_id"] for c in inputs}
    assert ids == {"happy_path:0", "happy_path:1", "edge_cases:0", "edge_cases:1"}


def test_build_case_scope_inputs_skips_already_badged_cases():
    """Grounding critic (or the LLM) may have already flagged some cases —
    don't spend budget re-checking them."""
    plan = _sk2373_plan()
    plan.happy_path[0]["needs_manual_verification"] = True
    inputs = build_case_scope_inputs(plan)
    ids = {c["case_id"] for c in inputs}
    assert "happy_path:0" not in ids
    assert "happy_path:1" in ids


def test_build_case_scope_inputs_skips_cases_with_no_cited_ac():
    """A case with no covers_acs has nothing to attach a warning to. Skip
    (mirrors the AC-grounding critic's convention)."""
    plan = TestPlan(
        happy_path=[{"title": "no ac", "steps": ["x"], "covers_acs": []}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    assert build_case_scope_inputs(plan) == []


def test_build_case_scope_inputs_surfaces_steps_and_expected():
    """The critic must see the actual claim, not just the title — steps and
    expected are the substantive assertion."""
    plan = _sk2373_plan()
    inputs = build_case_scope_inputs(plan)
    ec_0 = next(c for c in inputs if c["case_id"] == "edge_cases:0")
    assert any("3.11%" in s for s in ec_0["steps"])
    assert "no 3.11% default" in ec_0["expected"]
    assert "SK-2373-AC1" in ec_0["covers_acs"]


# ─── build_scope_critic_user_message ──────────────────────────────────────────


def test_build_scope_critic_user_message_includes_pr_scope_and_cases():
    plan = _sk2373_plan()
    inputs = build_case_scope_inputs(plan)
    scope = build_fix_scope_summary(_sk2373_dev_infos())
    message = build_scope_critic_user_message(inputs, scope)

    assert "MERGED PR SCOPE" in message
    assert "TEST CASES TO VERIFY" in message
    # The scope statement makes it into the critic's context.
    assert "no change to the displayed balance or any FRED behavior" in message
    # The at-risk case surfaces title + steps + cited ACs.
    assert "Verify Freddie Mac default rate is NOT auto-applied" in message
    assert "3.11%" in message
    assert "SK-2373-AC1" in message


# ─── apply_scope_verdicts (SK-2373 regression) ────────────────────────────────


def test_apply_scope_verdicts_badges_sk2373_reporter_drift():
    """Regression: SK-2373 tc-edge_cases-0 must be badged when the critic
    marks it unsupported — the PR body explicitly says 'no change to FRED
    behavior' and the case asserts FRED default is not applied.

    The three grounded cases (HP-0, HP-1, EC-1) must NOT be badged — they
    exercise behaviour the PR actually changed (tooltip copy).
    """
    plan = _sk2373_plan()
    verdicts = {
        "happy_path:0": {"verdict": "supported", "reason": "PR added the FNF-source line to the tooltip."},
        "happy_path:1": {"verdict": "supported", "reason": "PR added the balance-only disclaimer copy."},
        "edge_cases:0": {
            "verdict": "unsupported",
            "reason": "PR body says 'tooltip copy only, no change to FRED behavior' — this test asserts FRED default is not applied.",
        },
        "edge_cases:1": {"verdict": "supported", "reason": "PR replaced the fabricated amortization narrative in the tooltip."},
    }

    added = apply_scope_verdicts(plan, verdicts)

    # EC-0 is badged, the three supported cases are left clean.
    assert plan.edge_cases[0].get("needs_manual_verification") is True
    assert not plan.happy_path[0].get("needs_manual_verification")
    assert not plan.happy_path[1].get("needs_manual_verification")
    assert not plan.edge_cases[1].get("needs_manual_verification")

    # A matching grounding_warnings entry was appended for EC-0.
    assert len(added) == 1
    assert added[0]["ac_id"] == "SK-2373-AC1"
    assert added[0]["missing_element"] == (
        "Verify Freddie Mac default rate is NOT auto-applied when only loan balance is found"
    )
    # Explanation is prefixed so operators can tell this came from the scope pass.
    assert added[0]["explanation"].startswith("Fix-scope critic: ")
    assert "no change to FRED behavior" in added[0]["explanation"]

    assert plan.grounding_warnings == added


def test_apply_scope_verdicts_preserves_existing_grounding_warnings():
    """If prior passes (LLM-emitted or AC-grounding critic) added warnings,
    the scope critic must append rather than clobber."""
    plan = _sk2373_plan()
    existing = [{
        "ac_id": "SK-2373-AC7",
        "missing_element": "some other element",
        "explanation": "already flagged upstream",
    }]
    plan.grounding_warnings = list(existing)

    apply_scope_verdicts(plan, {
        "edge_cases:0": {"verdict": "unsupported", "reason": "out of scope"},
    })

    assert plan.grounding_warnings[0] == existing[0]
    assert plan.grounding_warnings[-1]["missing_element"] == (
        "Verify Freddie Mac default rate is NOT auto-applied when only loan balance is found"
    )


def test_apply_scope_verdicts_supported_is_noop():
    plan = _sk2373_plan()
    verdicts = {
        "happy_path:0": {"verdict": "supported", "reason": "fine"},
        "edge_cases:0": {"verdict": "supported", "reason": "fine"},
    }
    added = apply_scope_verdicts(plan, verdicts)
    assert added == []
    assert not plan.happy_path[0].get("needs_manual_verification")
    assert not plan.edge_cases[0].get("needs_manual_verification")
    assert not plan.grounding_warnings


def test_apply_scope_verdicts_empty_is_noop():
    plan = _sk2373_plan()
    assert apply_scope_verdicts(plan, {}) == []
    assert not plan.grounding_warnings


def test_apply_scope_verdicts_skips_case_with_no_cited_ac():
    """Same convention as the AC-grounding critic: can't attach a warning
    without an AC ID."""
    plan = TestPlan(
        happy_path=[{"title": "no ac", "steps": ["x"], "covers_acs": []}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    added = apply_scope_verdicts(plan, {
        "happy_path:0": {"verdict": "unsupported", "reason": "no anchor"},
    })
    assert added == []
    assert not plan.happy_path[0].get("needs_manual_verification")


# ─── parse_scope_verdicts ─────────────────────────────────────────────────────


def test_parse_scope_verdicts_accepts_tool_input_wrapper():
    raw = {"verdicts": [
        {"case_id": "happy_path:0", "verdict": "supported", "reason": "ok"},
        {"case_id": "edge_cases:0", "verdict": "unsupported", "reason": "out of scope"},
    ]}
    parsed = parse_scope_verdicts(raw)
    assert parsed == {
        "happy_path:0": {"verdict": "supported", "reason": "ok"},
        "edge_cases:0": {"verdict": "unsupported", "reason": "out of scope"},
    }


def test_parse_scope_verdicts_accepts_bare_list():
    raw = [
        {"case_id": "happy_path:0", "verdict": "supported", "reason": "ok"},
    ]
    parsed = parse_scope_verdicts(raw)
    assert parsed == {"happy_path:0": {"verdict": "supported", "reason": "ok"}}


def test_parse_scope_verdicts_drops_malformed_entries():
    """A misbehaving critic should degrade gracefully — drop bad entries,
    keep the good ones, never raise."""
    raw = {"verdicts": [
        {"case_id": "happy_path:0", "verdict": "supported", "reason": "ok"},
        {"case_id": "", "verdict": "supported", "reason": "blank id"},
        {"case_id": "x", "verdict": "maybe", "reason": "bad verdict"},
        "not a dict at all",
        {"case_id": "edge_cases:0", "verdict": "unsupported"},  # missing reason ok
    ]}
    parsed = parse_scope_verdicts(raw)
    assert set(parsed.keys()) == {"happy_path:0", "edge_cases:0"}
    assert parsed["edge_cases:0"]["reason"] == ""


def test_parse_scope_verdicts_on_junk_returns_empty():
    assert parse_scope_verdicts(None) == {}
    assert parse_scope_verdicts("nope") == {}
    assert parse_scope_verdicts({"verdicts": "not a list"}) == {}
