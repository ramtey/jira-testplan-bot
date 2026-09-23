"""The Jira comment must name every case by the id the database stores.

Why this file exists
--------------------
Two problems met on SK-2325's plan 536.

The first: a case flagged ``covered_by_unit_test`` was lifted out of the
checklist *and* out of the id space. All four of that plan's integration cases
were flagged, the section rendered as 0 of 0, two of them were verified live
with real evidence, and ``mark-passed.sh`` answered "'integration_tests:0' is
out of range — integration_tests has 0 case(s), valid indexes 0..-1". There was
nothing to write to.

The second: the displayed grouping has never matched storage. The comment
presents happy_path, edge [error_handling], edge [boundary], integration
[Cross-project] and integration as five visible groups; storage has four
sections, and ``edge_cases`` interleaves the two edge categories by plan
position — so the second boundary case on screen is ``edge_cases:3``, not
``edge_cases:1``. Anyone mapping from the visible labels marked the wrong case,
plausibly and silently.

Both are answered by printing the canonical id beside every case. That makes the
comment a third producer of case ids, after ``progress_key.case_index`` and the
UI — and a producer that drifts is exactly how SK-2642 and SK-2327 happened. So
this runs the *real* renderer (``frontend/src/utils/markdown.js``, via node)
through the *real* ADF conversion and the *real* adoption parser, and pins that
what comes out the far end is what ``progress_key`` started with.

Skipped when node is unavailable. Everything it covers that can be pinned in
pure Python is also pinned in ``test_plan_adoption.py`` against a hand-written
mirror of the renderer; this is the test that keeps the mirror honest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.app.jira_client import (
    TEST_PLAN_MARKER,
    _wrap_body_in_expand,
    markdown_to_adf,
)
from src.app.services.plan_adoption import parse_plan_from_adf
from src.app.services.progress_key import build_progress_key, case_index

MARKDOWN_JS = (
    Path(__file__).resolve().parents[1] / "frontend" / "src" / "utils" / "markdown.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not MARKDOWN_JS.exists(),
    reason="needs node and frontend/src/utils/markdown.js",
)


def _render(plan: dict, tmp_path: Path) -> str:
    """The Jira comment text the browser would post for `plan`."""
    script = tmp_path / "render.mjs"
    script.write_text(
        f"import {{ formatTestPlanAsJira }} from {json.dumps(str(MARKDOWN_JS))}\n"
        "process.stdout.write(formatTestPlanAsJira(JSON.parse(process.argv[2])))\n"
    )
    return subprocess.run(
        ["node", str(script), json.dumps(plan)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _adopt(plan: dict, tmp_path: Path) -> dict:
    """`plan` rendered, posted and read back — the whole round trip."""
    text = _render(plan, tmp_path)
    adf = _wrap_body_in_expand(markdown_to_adf(f"{TEST_PLAN_MARKER}\n\n{text}"))
    return parse_plan_from_adf(adf, ticket_key="SK-2325")


# The SK-2325 shape, reduced: covered cases in two different sections, both edge
# categories interleaved (the mapping trap), and a regression checklist.
PLAN = {
    "happy_path": [
        {"title": "Saves a property", "priority": "critical"},
        {
            "title": "Zero and false are retained on save",
            "covered_by_unit_test": True,
            "unit_test_ref": "src/save.test.ts > keeps zero",
        },
    ],
    "edge_cases": [
        {"title": "Rejects a malformed token", "category": "error_handling"},
        {"title": "Max length is 255", "category": "boundary"},
        {"title": "Empty payload 400s", "category": "error_handling"},
        {"title": "Min length is 1", "category": "boundary"},
    ],
    "integration_tests": [
        {
            "title": "POST /api/spoke/property returns the native payload",
            "covered_by_unit_test": True,
            "unit_test_ref": "spoke.test.ts > native",
        },
        {"title": "400 validation on the same route", "covered_by_unit_test": True},
    ],
    "regression_checklist": ["Existing search still works", "Login unaffected"],
}


def test_the_posted_comment_derives_the_same_progress_key(tmp_path):
    """The property that matters. Render, post, read back — same key."""
    adopted = _adopt(PLAN, tmp_path)
    assert build_progress_key(["SK-2325"], json.dumps(adopted)) == build_progress_key(
        ["SK-2325"], json.dumps(PLAN)
    )


def test_every_case_id_in_the_comment_exists_in_the_plan(tmp_path):
    """An id printed for a tester to copy must name a real case. A comment that
    offers `edge_cases:4` for a plan with four edge cases sends the runner to
    write a row nothing renders."""
    text = _render(PLAN, tmp_path)
    valid = set(case_index(json.dumps(PLAN)))
    printed = {
        token.strip("[]`")
        for token in text.replace("\n", " ").split()
        if ":" in token and token.strip("[]`").split(":")[0] in {
            "happy_path",
            "edge_cases",
            "integration_tests",
            "regression_checklist",
            "covered_by_unit_test",
        }
    }
    assert printed, "the comment printed no case ids at all"
    assert printed <= valid, f"comment names ids no plan case has: {printed - valid}"
    assert printed == valid, f"comment never names: {valid - printed}"


def test_the_second_boundary_case_is_edge_cases_3(tmp_path):
    """The mapping trap, stated as the test it is. The comment shows the edge
    categories interleaved; the case a reader would call "boundary 2" is
    `edge_cases:3`, and the comment has to say so in the same line as the title."""
    text = _render(PLAN, tmp_path)
    line = next(l for l in text.splitlines() if "Min length is 1" in l)
    assert "[edge_cases:3]" in line


def test_a_covered_case_is_addressable_in_the_comment(tmp_path):
    """SK-2325's whole complaint: there was no index to write to. There is one
    now, and it is printed next to the case it names."""
    text = _render(PLAN, tmp_path)
    line = next(l for l in text.splitlines() if "returns the native payload" in l)
    assert "covered_by_unit_test:1" in line
    assert "integration_tests" in line, "the section it was lifted from is lost"


def test_the_covered_list_is_posted_even_though_nobody_asked_for_it(tmp_path):
    """It used to be behind an off-by-default toggle. A comment that omits these
    cases gives the UAT runner no id to record against when one is verified live
    anyway — which is how the evidence on SK-2325 ended up with nowhere to go."""
    assert "ALREADY COVERED BY UNIT TESTS" in _render(PLAN, tmp_path)


def test_an_underscore_in_an_id_survives_the_trip_through_jira(tmp_path):
    """Jira's markdown-to-ADF conversion treats a lone underscore in plain
    paragraph text as an emphasis delimiter and drops it, so a bare
    `regression_checklist:0` posts as `regressionchecklist:0` — an id naming
    nothing, printed for a reader to copy. Inline code is exempt, which is why
    the renderer backticks the ids it puts in paragraphs."""
    text = _render(PLAN, tmp_path)
    adf = _wrap_body_in_expand(markdown_to_adf(f"{TEST_PLAN_MARKER}\n\n{text}"))
    rendered = json.dumps(adf, ensure_ascii=False)
    assert "regression_checklist:0" in rendered
    assert "regressionchecklist" not in rendered
    assert "covered_by_unit_test:0" in rendered
    assert "coveredbyunittest" not in rendered
